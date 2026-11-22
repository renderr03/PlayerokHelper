from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

from .engine import Engine
from .models import ServiceError, money, now, rub


class Listings:
    def __init__(self, engine: Engine):
        self.engine, self.db = engine, engine.db
        self.lock = asyncio.Lock()

    async def availability(self, binding, products, balance):
        if self.db.get("paused") or binding["paused"]:
            return "manual_pause"
        if binding["module"] == "stock":
            count = self.db.one(
                "SELECT count(*) n FROM stock WHERE item_id=? AND state='free'", (binding["item_id"],)
            )["n"]
            return "stock_empty" if not count else None
        if products is None or balance is None:
            return "kosell_unavailable"
        product = products.get(binding["product_id"])
        if not product:
            return "product_missing"
        if binding["max_cost"] <= 0:
            return "cost_limit_not_configured"
        if binding["module"] == "kosell" and product["available_accounts"] <= 0:
            return "no_accounts"
        if binding["module"] == "kosell":
            if not product["min_hours"] <= binding["hours"] <= product["max_hours"]:
                return "invalid_duration"
            cost = rub((await self.engine.kosell.quote(binding["product_id"], binding["hours"]))["total_rub"])
        else:
            # Exact extension price depends on the chosen existing rental. Check it
            # again before debit; hourly price is only the availability estimate.
            cost = rub(product["price_per_hour_rub"]) * binding["hours"]
        if cost > binding["max_cost"]:
            return "purchase_limit_exceeded"
        if balance < cost:
            return "insufficient_rub_balance"
        return None

    async def tick(self):
        async with self.lock:
            rows = self.db.all("SELECT * FROM bindings")
            products = balance = None
            if any(b["module"] != "stock" for b in rows):
                try:
                    products = {p["id"]: p for p in await self.engine.kosell.products()}
                    balance = rub((await self.engine.kosell.balance())["balance_rub"])
                except ServiceError:
                    self.db.notify(
                        "KOSell недоступен: скрываем связанные лоты.", f"kosell_outage:{int(now() // 3600)}"
                    )
            for binding in rows:
                try:
                    reason = await self.availability(binding, products, balance)
                except ServiceError:
                    reason = "quote_unavailable"
                item_id = binding["item_id"]
                try:
                    item = await self.engine.market.item(item_id)
                    intent = self.db.get("visibility_intent:" + item_id)
                    if (
                        intent == "hide"
                        and item["status"] in self.engine.market.visibility["hide"]["hidden_statuses"]
                    ):
                        self.db.execute("UPDATE bindings SET hidden_by_bot=1 WHERE item_id=?", (item_id,))
                        binding["hidden_by_bot"] = 1
                        self.db.set("visibility_intent:" + item_id, None)
                    if binding["hidden_by_bot"] and item["status"] == "APPROVED":
                        # A restore may have completed just before a crash; do not repeat it.
                        self.db.execute("UPDATE bindings SET hidden_by_bot=0 WHERE item_id=?", (item_id,))
                        binding["hidden_by_bot"] = 0
                    if reason:
                        self.db.execute(
                            "UPDATE bindings SET healthy_count=0,hide_reason=? WHERE item_id=?",
                            (reason, item_id),
                        )
                        if item["status"] == "APPROVED":
                            # Commit intent before remote mutation. A crash is reconciled
                            # against item state on the next pass, without republishing.
                            self.db.set("visibility_intent:" + item_id, "hide")
                            await self.engine.market.set_visible(item_id, False)
                            self.db.execute("UPDATE bindings SET hidden_by_bot=1 WHERE item_id=?", (item_id,))
                            self.db.set("visibility_intent:" + item_id, None)
                            self.db.notify(f"Лот {item_id} скрыт: {reason}", f"hide:{item_id}:{reason}")
                        elif self.db.get("visibility_intent:" + item_id) == "hide":
                            hidden = self.engine.market.visibility["hide"]["hidden_statuses"]
                            if item["status"] in hidden:
                                self.db.execute(
                                    "UPDATE bindings SET hidden_by_bot=1 WHERE item_id=?", (item_id,)
                                )
                                self.db.set("visibility_intent:" + item_id, None)
                        continue
                    healthy = binding["healthy_count"] + 1
                    self.db.execute(
                        "UPDATE bindings SET healthy_count=?,hide_reason=NULL WHERE item_id=?",
                        (healthy, item_id),
                    )
                    if binding["hidden_by_bot"] and healthy >= 2:
                        await self.engine.market.set_visible(item_id, True)
                        self.db.execute("UPDATE bindings SET hidden_by_bot=0 WHERE item_id=?", (item_id,))
                        self.db.notify(f"Лот {item_id} снова доступен", f"restore:{item_id}:{int(now())}")
                        continue
                    if self.db.get("paused"):
                        continue
                    if binding["auto_restore"] and item["status"] in {"SOLD", "EXPIRED"}:
                        if now() - binding["last_listing_action"] >= 300:
                            await self.action(binding, item, "publish")
                    elif binding["auto_bump"] and item["status"] == "APPROVED":
                        if now() - binding["last_bump"] >= binding["bump_interval"]:
                            await self.action(binding, item, "bump")
                except ServiceError as exc:
                    self.db.notify(
                        f"Лот {item_id}: операция не выполнена ({exc.code}). Проверьте доступность.",
                        f"listing_error:{item_id}:{exc.code}:{int(now() // 3600)}",
                    )

    async def action(self, binding, item, kind):
        item_id = item["id"]
        if self.db.one(
            "SELECT id FROM listing_actions WHERE item_id=? AND state IN ('executing','uncertain')",
            (item_id,),
        ):
            return
        tiers = await self.engine.market.tiers(item)
        tiers = sorted((t for t in tiers if rub(t["price"]) >= 0), key=lambda t: rub(t["price"]))
        if not tiers:
            return
        tier = tiers[0]
        cost = rub(tier["price"])
        action_id = str(uuid.uuid4())
        day = datetime.now(self.engine.tz).date().isoformat()
        with self.db.transaction():
            if cost:
                spent = self.db.one(
                    "SELECT coalesce(sum(amount),0) n FROM expenses "
                    "WHERE day=? AND state IN ('reserved','paid')",
                    (day,),
                )["n"]
                if (
                    not self.db.get("paid_enabled")
                    or cost > self.db.get("action_limit")
                    or spent + cost > self.db.get("daily_budget")
                ):
                    self.db.notify(
                        f"Пропущено {kind} лота {item_id}: {money(cost)}, проверьте лимиты.",
                        f"budget:{day}:{item_id}:{kind}",
                    )
                    return
            self.db.execute(
                "INSERT INTO expenses VALUES (?,?,?,?,?,?,?,?)",
                (action_id, None, item_id, kind, cost, "reserved", day, now()),
            )
            self.db.execute(
                "INSERT INTO listing_actions VALUES (?,?,?,?,?,?,?,?)",
                (action_id, item_id, kind, "executing", cost, tier["id"], now(), None),
            )
        try:
            result = await self.engine.market.listing_action(item_id, tier["id"], kind)
        except ServiceError as exc:
            state = "uncertain" if exc.ambiguous else "failed"
            self.db.execute(
                "UPDATE listing_actions SET state=?,error=? WHERE id=?", (state, exc.code, action_id)
            )
            if not exc.ambiguous:
                self.db.execute("UPDATE expenses SET state='released' WHERE id=?", (action_id,))
            raise
        actual = result.get("statusPayment")
        previous_payment = item.get("statusPayment") or {}
        actual_cost = (
            rub(actual["value"])
            if actual and actual.get("id") != previous_payment.get("id") and actual.get("value") is not None
            else cost
        )
        with self.db.transaction():
            self.db.execute("UPDATE expenses SET state='paid',amount=? WHERE id=?", (actual_cost, action_id))
            self.db.execute("UPDATE listing_actions SET state='done' WHERE id=?", (action_id,))
            self.db.execute(
                "UPDATE bindings SET last_listing_action=?,last_bump=CASE WHEN ?='bump' "
                "THEN ? ELSE last_bump END WHERE item_id=?",
                (now(), kind, now(), item_id),
            )
        if actual_cost > cost:
            self.db.set("paid_enabled", False)
            self.db.notify(
                "Playerok списал больше указанной цены. Платная автоматика отключена.",
                "unexpected_listing_price:" + action_id,
            )
