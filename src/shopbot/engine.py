from __future__ import annotations

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from .alerts import Alerts
from .db import Database
from .models import Deal, ServiceError, money, now, rub, timestamp

TERMINAL = {"delivered", "historical", "ignored", "cancelled", "failed", "review"}
CONFIRMED = {"CONFIRMED", "CONFIRMED_AUTOMATICALLY"}


class Engine:
    def __init__(self, db: Database, market, kosell, timezone="Europe/Moscow"):
        self.db, self.market, self.kosell = db, market, kosell
        self.tz = ZoneInfo(timezone)
        self._locks: dict[str, asyncio.Lock] = {}
        self._purchase_lock = asyncio.Lock()

    def order(self, order_id):
        return self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))

    def update(self, order_id: str, **fields):
        allowed = {
            "stage",
            "rental_uid",
            "rental_number",
            "expires_at",
            "cost",
            "error",
            "request_json",
            "idem_key",
            "target_order_id",
            "delivered_at",
        }
        if not fields.keys() <= allowed:
            raise ValueError("Unsupported order fields")
        self.db.execute(
            "UPDATE orders SET " + ",".join(f"{k}=?" for k in fields) + ",updated_at=? WHERE id=?",
            (*fields.values(), now(), order_id),
        )

    def accept(self, deal: Deal, *, historical=False):
        old = self.order(deal.id)
        binding = self.db.one("SELECT * FROM bindings WHERE item_id=?", (deal.item_id,))
        if old is None:
            cutoff = self.db.get("cutoff", now())
            # Importing history must not suppress an active paid order that arrived
            # after this store's first-start cutoff. The importer never calls process().
            stage = "historical" if deal.created_at < cutoff else "new"
            if not binding and stage == "new":
                stage = "ignored"
            self.db.execute(
                "INSERT INTO orders(id,item_id,buyer_id,buyer_name,chat_id,market_status,created_at,price,"
                "title,game,category,module,stage,problem,rating,completed_at,historical,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    deal.id,
                    deal.item_id,
                    deal.buyer_id,
                    deal.buyer_name,
                    deal.chat_id,
                    deal.status,
                    deal.created_at,
                    deal.price,
                    deal.title,
                    deal.game,
                    deal.category,
                    binding["module"] if binding else None,
                    stage,
                    deal.problem,
                    deal.rating,
                    deal.completed_at,
                    int(stage == "historical"),
                    now(),
                ),
            )
            if stage == "new":
                self.db.notify(
                    f"Новый заказ {deal.id}\n{deal.title}\n{money(deal.price)}", f"order:{deal.id}", deal.id
                )
        else:
            self.db.execute(
                "UPDATE orders SET market_status=?,problem=?,rating=?,completed_at=?,updated_at=? WHERE id=?",
                (deal.status, deal.problem, deal.rating, deal.completed_at, now(), deal.id),
            )
            if not old["historical"]:
                for name, changed in (
                    ("problem", deal.problem and not old["problem"]),
                    ("review", deal.rating is not None and deal.rating != old["rating"]),
                    ("seller_confirmed", deal.status == "SENT" and old["market_status"] != "SENT"),
                    ("buyer_confirmed", deal.status in CONFIRMED and old["market_status"] not in CONFIRMED),
                ):
                    if changed:
                        self.event_text(name, self.order(deal.id), f"{name}:{deal.rating or deal.status}")
                if deal.problem and not old["problem"]:
                    self.db.notify(f"Проблема по заказу {deal.id}", f"problem:{deal.id}")
                if deal.status in CONFIRMED and old["market_status"] not in CONFIRMED:
                    self.schedule(deal.id, "review_reminder", now() + 3600)
        if deal.status == "ROLLED_BACK":
            self.db.execute(
                "UPDATE jobs SET state='cancelled' WHERE order_id=? AND state='pending'", (deal.id,)
            )
            self.db.execute(
                "UPDATE outbox SET state='cancelled' WHERE order_id=? AND state='pending'", (deal.id,)
            )
            if self.order(deal.id)["stage"] not in {"delivered", "historical"}:
                self.update(deal.id, stage="cancelled")
        if deal.problem:
            self.db.execute(
                "UPDATE jobs SET state='cancelled' WHERE order_id=? AND state='pending'", (deal.id,)
            )
        return self.order(deal.id)

    def event_text(self, name: str, order: dict, suffix: str):
        if self.db.feature(name, order["item_id"]):
            self.db.enqueue(
                "playerok",
                order["chat_id"],
                self.render(name, order),
                f"event:{order['id']}:{suffix}",
                order_id=order["id"],
                purpose="event:" + name,
            )

    def render(self, name: str, order: dict | None = None, item_id=None) -> str:
        row = order or {}
        item_id = item_id or row.get("item_id")
        overrides = self.db.get("override:" + (item_id or ""), {})
        template = overrides.get("templates", {}).get(name, self.db.get("templates").get(name, ""))
        values = {
            "buyer": row.get("buyer_name", ""),
            "order": row.get("id", ""),
            "title": row.get("title", ""),
            "expires": self.expiry(row.get("expires_at")),
        }
        # Only documented substitutions; format() attribute/index access is not accepted.
        for key, value in values.items():
            template = template.replace("{" + key + "}", str(value))
        return template[:3800]

    def expiry(self, value):
        return datetime.fromtimestamp(value, self.tz).strftime("%d.%m.%Y %H:%M %Z") if value else "уточняется"

    def schedule(self, order_id: str, kind: str, due: float):
        self.db.execute(
            "INSERT OR IGNORE INTO jobs(id,kind,order_id,due_at) VALUES (?,?,?,?)",
            (f"{order_id}:{kind}", kind, order_id, due),
        )

    def fail(self, order_id: str, code: str, *, review=False):
        self.update(order_id, stage="review" if review else "failed", error=code)
        self.db.notify(
            f"Заказ {order_id}: требуется проверка\nПричина: {code}", f"failure:{order_id}:{code}", order_id
        )

    async def process(self, order_id: str):
        lock = self._locks.setdefault(order_id, asyncio.Lock())
        async with lock:
            row = self.order(order_id)
            if not row or row["stage"] in TERMINAL or row["stage"] in {"sending", "wait_selection"}:
                return
            if self.db.get("paused") or not self.db.get("live_enabled", False):
                return
            if row["stage"] == "extending":
                self.fail(order_id, "extension_interrupted_check_provider", review=True)
                return
            if row["stage"] == "creating_delivery":
                # Queuing the encrypted delivery and the stage transition are atomic.
                self.fail(order_id, "unexpected_delivery_stage", review=True)
                return
            try:
                fresh = await self.market.deal(order_id)
                self.accept(fresh)
                row = self.order(order_id)
                if fresh.problem or fresh.status == "ROLLED_BACK":
                    return
                if row["stage"] == "new" and fresh.status != "PAID":
                    return
                binding = self.db.one("SELECT * FROM bindings WHERE item_id=?", (row["item_id"],))
                if not binding:
                    self.fail(order_id, "binding_missing")
                    return
                if binding["paused"] and row["stage"] == "new":
                    self.fail(order_id, "listing_paused")
                    return
                if row["stage"] == "new":
                    if row["module"] == "stock":
                        self.stock_delivery(row, binding)
                        return
                    if row["module"] == "extend":
                        selection = self.db.one(
                            "SELECT * FROM selections WHERE buyer_id=? AND expires_at>?",
                            (row["buyer_id"], now()),
                        )
                        if not selection:
                            self.update(order_id, stage="wait_selection")
                            self.db.enqueue(
                                "playerok",
                                row["chat_id"],
                                f"Продление оплачено. Выберите аренду: !продлить <номер>. Заказ {order_id}",
                                f"select:{order_id}",
                            )
                            return
                        target = self.owned_rental(row["buyer_id"], selection["order_id"])
                        if not target or target["expires_at"] <= now():
                            self.update(order_id, stage="wait_selection")
                            return
                        if target["product_id"] != binding["product_id"]:
                            self.update(order_id, stage="wait_selection")
                            return
                        with self.db.transaction():
                            self.update(order_id, target_order_id=target["id"], stage="extension_ready")
                            self.db.execute("DELETE FROM selections WHERE buyer_id=?", (row["buyer_id"],))
                        row = self.order(order_id)
                    else:
                        await self.prepare_rent(row, binding)
                        row = self.order(order_id)
                if row["stage"] == "rent_pending":
                    await self.purchase(row)
                    row = self.order(order_id)
                if row["stage"] == "preparing":
                    await self.prepare_delivery(row, binding)
                elif row["stage"] == "extension_ready":
                    await self.extend(row, binding)
            except ServiceError as exc:
                row = self.order(order_id)
                if exc.retry_after:
                    self.db.set("retry:" + order_id, now() + exc.retry_after)
                    return
                if exc.ambiguous and row["stage"] == "rent_pending":
                    self.update(order_id, error=exc.code)  # same key and request on the next tick
                    return
                if not exc.ambiguous and (
                    "network" in exc.code or exc.code.endswith(("_500", "_502", "_503", "_504"))
                ):
                    self.db.set("retry:" + order_id, now() + 30)
                    return
                if row["stage"] == "preparing" and (
                    exc.ambiguous or "network" in exc.code or "503" in exc.code
                ):
                    return
                self.fail(order_id, exc.code, review=exc.ambiguous)
            except (KeyError, ValueError, TypeError):
                self.fail(order_id, "provider_response_contract_changed", review=True)

    def stock_delivery(self, row, binding):
        stock = self.db.reserve_stock(row["id"], row["item_id"])
        if not stock:
            self.fail(row["id"], "stock_empty")
            return
        self.queue_delivery(row, self.db.decrypt(stock["ciphertext"]), binding["instruction"], stock["cost"])
        Alerts(self).stock(row["item_id"])

    async def prepare_rent(self, row, binding):
        if not binding["product_id"] or binding["max_cost"] <= 0:
            raise ServiceError("configure_product_and_cost_limit")
        products = {p["id"]: p for p in await self.kosell.products()}
        product = products.get(binding["product_id"])
        if not product or product["available_accounts"] <= 0:
            raise ServiceError("no_accounts")
        if not product["min_hours"] <= binding["hours"] <= product["max_hours"]:
            raise ServiceError("invalid_rental_duration")
        quote = await self.kosell.quote(binding["product_id"], binding["hours"])
        cost = rub(quote["total_rub"])
        self.check_cost(cost, binding)
        balance = await self.kosell.balance()
        if rub(balance["balance_rub"]) < cost:
            raise ServiceError("insufficient_rub_balance")
        request = {
            "body": {
                "product_id": binding["product_id"],
                "duration_hours": binding["hours"],
                "currency": "RUB",
                "verify_before_issue": True,
            },
            "quoted_cost": cost,
        }
        self.update(
            row["id"],
            stage="rent_pending",
            request_json=json.dumps(request),
            idem_key="playerok:rent:" + row["id"],
        )

    @staticmethod
    def check_cost(cost, binding):
        if cost < 0 or cost > binding["max_cost"]:
            raise ServiceError("purchase_limit_exceeded")

    async def purchase(self, row):
        request = json.loads(row["request_json"])
        async with self._purchase_lock:
            result = await self.kosell.rent(request["body"], row["idem_key"])
        # Persist provider identifiers before credentials or messaging.
        cost = rub(result["price_rub"]) if result.get("price_rub") is not None else None
        with self.db.transaction():
            self.update(
                row["id"],
                rental_uid=result["rental_uid"],
                rental_number=result.get("rental_number"),
                expires_at=timestamp(result["expires_at"]),
                cost=cost,
                stage="preparing",
                error=None,
            )
            self.db.set("delivery_required:" + row["id"], bool(result.get("delivery")))
        binding = self.db.one("SELECT * FROM bindings WHERE item_id=?", (row["item_id"],))
        if binding and cost is not None and cost > binding["max_cost"]:
            self.db.execute("UPDATE bindings SET paused=1 WHERE item_id=?", (binding["item_id"],))
            self.db.notify(
                f"KOSell изменил цену при закупке заказа {row['id']}: {money(cost)}. "
                "В API нет атомарного ограничения цены; проверьте лот.",
                f"price_changed:{row['id']}",
            )

    async def prepare_delivery(self, row, binding):
        if self.db.get("delivery_required:" + row["id"], True):
            delivery = await self.kosell.delivery(row["rental_uid"])
            if delivery["state"] == "failed":
                self.fail(row["id"], "preparation_failed_refunded=" + str(delivery["refunded"]), review=True)
                return
            if delivery["state"] != "ready":
                if timestamp(delivery["deadline"]) < now():
                    self.fail(row["id"], "preparation_deadline_exceeded", review=True)
                return
        creds = await self.kosell.credentials(row["rental_uid"])
        text = (
            f"Логин: {creds['steam_login']}\nПароль: {creds['steam_password']}\n"
            f"До: {self.expiry(row['expires_at'])}\n"
            f"Номер аренды: {row['rental_number'] or row['id']}\n"
            "Код входа: !код <номер>\nСрок: !срок <номер>\nПродление: !продлить <номер>"
        )
        self.queue_delivery(row, text, binding["instruction"], row["cost"])

    def queue_delivery(self, row, payload, instruction, cost):
        text = f"Заказ {row['id']}\n{payload}"
        if instruction:
            text += "\n\n" + instruction
        if len(text) > 3900:
            self.fail(row["id"], "delivery_text_too_long", review=True)
            return
        with self.db.transaction():
            self.db.enqueue(
                "playerok",
                row["chat_id"],
                text,
                f"delivery:{row['id']}",
                order_id=row["id"],
                purpose="delivery",
            )
            self.update(row["id"], stage="sending", cost=cost)

    def owned_rental(self, buyer_id, reference):
        return self.db.one(
            "SELECT o.*,b.product_id FROM orders o LEFT JOIN bindings b ON b.item_id=o.item_id "
            "WHERE o.buyer_id=? AND (o.id=? OR o.rental_number=?) AND o.module='kosell' "
            "AND o.stage='delivered' AND o.market_status!='ROLLED_BACK' AND o.problem=0",
            (buyer_id, reference, reference),
        )

    async def extend(self, row, binding):
        target = self.owned_rental(row["buyer_id"], row["target_order_id"])
        if not target or not target["expires_at"] or target["expires_at"] <= now():
            self.fail(row["id"], "rental_not_active")
            return
        if target["product_id"] != binding["product_id"]:
            self.fail(row["id"], "extension_product_mismatch")
            return
        lock = self._locks.setdefault("rental:" + target["rental_uid"], asyncio.Lock())
        async with lock:
            quote = await self.kosell.extension_quote(target["rental_uid"], binding["hours"])
            cost = rub(quote["total_rub"])
            self.check_cost(cost, binding)
            if rub((await self.kosell.balance())["balance_rub"]) < cost:
                raise ServiceError("insufficient_rub_balance")
            # A crash after this commit always requires manual reconciliation.
            request = {
                "uid": target["rental_uid"],
                "hours": binding["hours"],
                "currency": "RUB",
                "previous_expiry": target["expires_at"],
                "quoted_cost": cost,
            }
            self.update(row["id"], stage="extending", request_json=json.dumps(request))
            try:
                result = await self.kosell.extend(target["rental_uid"], binding["hours"])
                if not result.get("success") or result.get("currency") != "RUB":
                    self.fail(row["id"], "extension_result_requires_review", review=True)
                    return
                expiry = timestamp(result["new_expires_at"])
                actual_cost = rub(result["price_paid"])
            except ServiceError as exc:
                self.fail(row["id"], exc.code, review=exc.ambiguous)
                return
            with self.db.transaction():
                self.update(target["id"], expires_at=expiry)
                self.update(row["id"], rental_uid=target["rental_uid"], expires_at=expiry, cost=actual_cost)
                self.queue_delivery_without_transaction(
                    row, f"Аренда продлена до {self.expiry(expiry)}.", actual_cost
                )
            if actual_cost > binding["max_cost"]:
                self.db.execute("UPDATE bindings SET paused=1 WHERE item_id=?", (binding["item_id"],))
                self.db.notify(
                    f"Продление {row['id']} стоило выше лимита. Лот поставлен на ручную паузу.",
                    "extension_cost_changed:" + row["id"],
                )

    def queue_delivery_without_transaction(self, row, text, cost):
        self.db.enqueue(
            "playerok",
            row["chat_id"],
            f"Заказ {row['id']}\n{text}",
            f"delivery:{row['id']}",
            order_id=row["id"],
            purpose="delivery",
        )
        self.update(row["id"], stage="sending", cost=cost)

    async def finish_delivery(self, order_id):
        row = self.order(order_id)
        if row["stage"] == "delivered":
            return
        with self.db.transaction():
            self.update(order_id, stage="delivered", delivered_at=now())
            self.db.execute("UPDATE stock SET state='used' WHERE order_id=?", (order_id,))
            self.schedule(order_id, "confirm_reminder", now() + 3600)
        self.db.notify(f"Заказ {order_id} выдан\n{row['title']}", f"delivered:{order_id}", order_id)
        if self.db.feature("auto_confirm", row["item_id"]):
            self.schedule(order_id, "mark_sent", now())

    async def flush(self, send_telegram):
        for order in self.db.all(
            "SELECT o.id FROM orders o JOIN outbox b ON b.order_id=o.id "
            "WHERE o.stage='sending' AND b.state='sent' AND b.purpose='delivery'"
        ):
            await self.finish_delivery(order["id"])
        # Orphaned sends after restart are uncertain; never blindly replay.
        for row in self.db.all("SELECT * FROM outbox WHERE state='uncertain' AND channel='playerok'"):
            if not self.db.get("live_enabled", False):
                continue
            text = self.db.decrypt(row["ciphertext"])
            try:
                history = await self.market.messages(row["target"], row["created_at"] - 2)
            except ServiceError:
                continue
            found = next((m for m in history if m.user_id == self.market.user_id and m.text == text), None)
            if found:
                self.db.execute(
                    "UPDATE outbox SET state='sent',remote_id=? WHERE id=?", (found.id, row["id"])
                )
                if row["purpose"] == "delivery":
                    await self.finish_delivery(row["order_id"])
        for row in self.db.all(
            "SELECT * FROM outbox WHERE state='pending' AND not_before<=? ORDER BY id LIMIT 40", (now(),)
        ):
            if row["expires_at"] is not None and row["expires_at"] <= now():
                self.db.execute("UPDATE outbox SET state='cancelled' WHERE id=?", (row["id"],))
                continue
            if row["channel"] == "playerok":
                if self.db.get("paused") or not self.db.get("live_enabled", False):
                    continue
                if row["order_id"]:
                    order = self.order(row["order_id"])
                    fresh = await self.market.deal(row["order_id"])
                    self.accept(fresh)
                    if fresh.status == "ROLLED_BACK" or order["historical"]:
                        continue
                    if fresh.problem and row["purpose"] in {
                        "delivery",
                        "confirm_reminder",
                        "review_reminder",
                    }:
                        continue
                    if (row["purpose"] == "confirm_reminder" and fresh.status in CONFIRMED) or (
                        row["purpose"] == "review_reminder" and fresh.rating is not None
                    ):
                        self.db.execute("UPDATE outbox SET state='cancelled' WHERE id=?", (row["id"],))
                        continue
                if row["purpose"] == "reply":
                    chat = self.db.one("SELECT * FROM chats WHERE id=?", (row["target"],))
                    if chat and chat["operator_paused"]:
                        self.db.execute("UPDATE outbox SET state='cancelled' WHERE id=?", (row["id"],))
                        continue
            changed = self.db.execute(
                "UPDATE outbox SET state='sending',attempts=attempts+1 WHERE id=? AND state='pending'",
                (row["id"],),
            ).rowcount
            if not changed:
                continue
            try:
                text = self.db.decrypt(row["ciphertext"])
                remote_id = (
                    await self.market.send_message(row["target"], text)
                    if row["channel"] == "playerok"
                    else await send_telegram(row["target"], text, row["order_id"], row["purpose"])
                )
            except ServiceError as exc:
                state = "uncertain" if exc.ambiguous else "failed"
                if exc.retry_after:
                    state = "pending"
                self.db.execute(
                    "UPDATE outbox SET state=?,error=?,not_before=? WHERE id=?",
                    (state, exc.code, now() + exc.retry_after, row["id"]),
                )
                if row["channel"] == "playerok":
                    self.db.notify(
                        f"Отправка в чат {row['target']}: {exc.code}. Требуется проверка.",
                        f"send_error:{row['id']}",
                    )
                continue
            self.db.execute(
                "UPDATE outbox SET state='sent',remote_id=? WHERE id=?", (str(remote_id), row["id"])
            )
            if row["purpose"] == "delivery":
                await self.finish_delivery(row["order_id"])

    async def run_jobs(self):
        if self.db.get("paused") or not self.db.get("live_enabled", False):
            return
        for job in self.db.all("SELECT * FROM jobs WHERE state='pending' AND due_at<=?", (now(),)):
            order = self.order(job["order_id"])
            kind = job["kind"]
            fresh = await self.market.deal(order["id"])
            self.accept(fresh)
            order = self.order(order["id"])
            cancel = fresh.problem or fresh.status == "ROLLED_BACK"
            if kind == "confirm_reminder":
                cancel |= fresh.status in CONFIRMED or not order["delivered_at"]
            elif kind == "review_reminder":
                cancel |= fresh.rating is not None or fresh.status not in CONFIRMED
            enabled = self.db.feature("auto_confirm" if kind == "mark_sent" else kind, order["item_id"])
            if kind == "mark_sent" and self.db.get("manual_mark:" + order["id"]):
                enabled = True
            if cancel or not enabled:
                self.db.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (job["id"],))
                continue
            if kind == "mark_sent":
                if fresh.status != "PAID":
                    self.db.execute("UPDATE jobs SET state='done' WHERE id=?", (job["id"],))
                    continue
                self.db.execute("UPDATE jobs SET state='executing' WHERE id=?", (job["id"],))
                try:
                    await self.market.mark_sent(order["id"])
                except ServiceError:
                    self.db.notify(
                        f"Проверьте отметку выполнения заказа {order['id']}", f"mark:{order['id']}"
                    )
                    continue
            else:
                self.db.enqueue(
                    "playerok",
                    order["chat_id"],
                    self.render(kind, order),
                    f"job:{job['id']}",
                    order_id=order["id"],
                    purpose=kind,
                )
            self.db.execute("UPDATE jobs SET state='done' WHERE id=?", (job["id"],))

    async def poll_orders(self, *, historical=False):
        seen = set()
        async for order_id in self.market.recent_deal_ids(
            None if historical else ["PAID", "PENDING", "SENT"]
        ):
            self.accept(await self.market.deal(order_id), historical=historical)
            self.db.set("checked:" + order_id, now())
            seen.add(order_id)
        if historical:
            return
        # Confirmations and reviews can arrive after an order leaves the active list.
        for row in self.db.all(
            "SELECT * FROM orders WHERE historical=0 AND market_status!='ROLLED_BACK' "
            "AND (created_at>? OR stage NOT IN ('delivered','ignored','cancelled','failed','review'))",
            (now() - 7 * 86400,),
        ):
            delay = 15 if row["market_status"] in {"PAID", "PENDING", "SENT"} else 300
            if row["id"] not in seen and now() - self.db.get("checked:" + row["id"], 0) >= delay:
                self.accept(await self.market.deal(row["id"]))
                self.db.set("checked:" + row["id"], now())
        for row in self.db.all(
            "SELECT id FROM orders WHERE historical=0 AND stage NOT IN "
            "('delivered','ignored','cancelled','failed','review','historical')"
        ):
            if self.db.get("retry:" + row["id"], 0) <= now():
                await self.process(row["id"])

    def recover(self):
        self.db.execute("UPDATE outbox SET state='uncertain' WHERE state='sending'")
        for row in self.db.all("SELECT id FROM orders WHERE stage='extending'"):
            self.fail(row["id"], "extension_interrupted_check_provider", review=True)
        for row in self.db.all("SELECT id,item_id FROM listing_actions WHERE state='executing'"):
            self.db.execute("UPDATE listing_actions SET state='uncertain' WHERE id=?", (row["id"],))
            self.db.notify(
                f"Проверьте платное действие лота {row['item_id']}", "listing_recover:" + row["id"]
            )
        for row in self.db.all("SELECT id,order_id FROM jobs WHERE state='executing'"):
            self.db.execute("UPDATE jobs SET state='uncertain' WHERE id=?", (row["id"],))
            self.db.notify(
                f"Проверьте отметку выполнения заказа {row['order_id']}", "job_recover:" + row["id"]
            )

    def diagnostic(self):
        return {
            "orders": self.db.one("SELECT count(*) n FROM orders")["n"],
            "review": self.db.one("SELECT count(*) n FROM orders WHERE stage='review'")["n"],
            "uncertain": self.db.one("SELECT count(*) n FROM outbox WHERE state='uncertain'")["n"],
            "paused": self.db.get("paused"),
        }
