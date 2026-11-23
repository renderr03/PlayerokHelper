from __future__ import annotations

from decimal import Decimal

from .alerts import Alerts
from .models import money, now, rub


def estimate(db, binding, item, quotes):
    result = {"at": now(), "known": False}
    fee = db.get("fee_percent")
    cost = None
    mode = binding.get("fulfillment") or binding["module"]
    if mode == "kosell":
        quote = quotes.get((binding["product_id"], binding["hours"]))
        cost = rub(quote["total_rub"]) if quote else None
    elif mode in {"stock", "own_rent"}:
        table = "stock" if mode == "stock" else "own_accounts"
        row = db.one(
            f"SELECT cost FROM {table} WHERE item_id=? AND state='free' ORDER BY id LIMIT 1",
            (binding["item_id"],),
        )
        cost = row["cost"] if row else None
    elif mode == "own_extend":
        cost = 0
    if cost is not None and fee is not None:
        price = rub(item.get("rawPrice", item["price"]))
        commission = int(Decimal(price) * Decimal(str(fee)) / 100)
        result.update(known=True, price=price, cost=cost, margin=price - commission - cost)
    return result


def record(engine, binding, item, quotes):
    try:
        result = estimate(engine.db, binding, item, quotes)
    except (KeyError, ValueError, TypeError, ArithmeticError):
        result = {"at": now(), "known": False}
    engine.db.set("margin:" + binding["item_id"], result)
    threshold = binding.get("margin_min")
    if threshold is not None and result["known"]:
        Alerts(engine).observe(
            "margin:" + binding["item_id"],
            result["margin"] < threshold,
            f"💰 Низкая ожидаемая маржа лота {binding['item_id']}: {money(result['margin'])}, порог {money(threshold)}. "
            "Расходы на продвижение учитываются отдельно; цены и закупки не изменены.",
            f"✅ Ожидаемая маржа лота {binding['item_id']} снова выше порога.",
        )
