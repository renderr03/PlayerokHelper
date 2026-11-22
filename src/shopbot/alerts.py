"""Persistent edge-triggered notifications, sharing the readiness/availability snapshots."""

from __future__ import annotations

from .models import ServiceError, rub


class Alerts:
    def __init__(self, engine):
        self.engine, self.db = engine, engine.db

    def observe(self, key, bad, problem, recovered, *, failures=1, successes=1):
        if not self.db.get("alerts_enabled") or bad is None:
            return
        with self.db.transaction():
            state = self.db.get("alert:" + key, {"active": False, "bad": 0, "good": 0, "generation": 0})
            state["bad"] = state["bad"] + 1 if bad else 0
            state["good"] = state["good"] + 1 if not bad else 0
            transition = None
            if bad and not state["active"] and state["bad"] >= failures:
                state["active"] = True
                state["generation"] += 1
                transition = problem
            elif not bad and state["active"] and state["good"] >= successes:
                state["active"] = False
                transition = recovered
            if transition:
                self.db.notify(
                    transition,
                    f"alert:{key}:{state['generation']}:{int(state['active'])}",
                )
            self.db.set("alert:" + key, state)

    def stock(self, item_id):
        count = self.db.one("SELECT count(*) n FROM stock WHERE item_id=? AND state='free'", (item_id,))["n"]
        self.observe(
            "stock:" + item_id,
            count <= self.db.get("stock_low"),
            f"📦 Мало товара: {item_id}. Осталось позиций: {count}.",
            f"✅ Склад пополнен: {item_id}. Свободных позиций: {count}.",
        )

    def connections(self, services):
        for name, status in services.items():
            if not status["configured"]:
                continue
            self.observe(
                "connection:" + name,
                not status["ok"],
                f"🚨 Подключение {name} не работает: три проверки подряд. Откройте /setup.",
                f"✅ Подключение {name} восстановлено: две успешные проверки.",
                failures=3,
                successes=2,
            )

    async def availability(self, bindings, products, balance, quotes):
        if not self.db.get("alerts_enabled"):
            return
        for binding in bindings:
            if binding["paused"] or self.db.get("paused"):
                continue
            item_id = binding["item_id"]
            if binding["module"] == "stock":
                self.stock(item_id)
                continue
            if binding["module"] != "kosell" or products is None or balance is None:
                continue
            product = products.get(binding["product_id"])
            if not product or not product["min_hours"] <= binding["hours"] <= product["max_hours"]:
                continue
            try:
                key = (binding["product_id"], binding["hours"])
                if key not in quotes:
                    quotes[key] = None
                    quotes[key] = await self.engine.kosell.quote(*key)
                if quotes[key] is None:
                    continue
                cost = rub(quotes[key]["total_rub"])
                if cost <= 0:
                    continue
                count = balance // cost
                self.observe(
                    "balance:" + item_id,
                    count <= self.db.get("rental_low"),
                    f"💰 Баланса KOSell хватит примерно на {count} аренд лота {item_id}. "
                    "Баланс общий: оценки разных лотов не суммируются.",
                    f"✅ Баланс для лота {item_id} выше порога: примерно {count} аренд. "
                    "Оценка по текущей цене, товар не зарезервирован.",
                )
            except (ServiceError, KeyError, TypeError, ValueError, ArithmeticError):
                # A failed/unknown quote is not a zero balance or a recovery.
                continue
