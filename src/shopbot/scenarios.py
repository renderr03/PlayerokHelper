"""Bounded message rules; no arbitrary code, payment or credential actions."""

from __future__ import annotations

from datetime import datetime

from .models import now

CONDITIONS = {
    "any": "Любой заказ",
    "paid": "Оплачен",
    "delivered": "Выдан",
    "no_order": "До покупки",
    "problem": "Проблема",
}
PERIODS = {"any": "В любое время", "working": "В рабочее время", "outside": "Вне рабочего времени"}
ACTIONS = {"reply": "Ответить текстом", "instruction": "Инструкция лота", "operator": "Передать продавцу"}


def match(rule, text, order, working):
    if not rule["enabled"] or rule["phrase"].casefold() not in text.casefold():
        return False
    condition = rule["condition"]
    if condition == "paid" and (
        not order or order["market_status"] not in {"PAID", "SENT"} or order["problem"]
    ):
        return False
    if condition == "delivered" and (
        not order
        or order["stage"] != "delivered"
        or order["market_status"] == "ROLLED_BACK"
        or order["problem"]
    ):
        return False
    if condition == "no_order" and order:
        return False
    if condition == "problem" and (not order or not order["problem"]):
        return False
    return not (rule["period"] == "working" and not working or rule["period"] == "outside" and working)


def choose(engine, text, order):
    hour = datetime.now(engine.tz).hour
    start, end = engine.db.get("work_start"), engine.db.get("work_end")
    working = not engine.db.get("offline_override") and (
        start <= hour < end if start < end else hour >= start or hour < end
    )
    for rule in engine.db.all("SELECT * FROM automation_rules ORDER BY priority DESC,length(phrase) DESC,id"):
        if match(rule, text, order, working):
            return rule
    return None


def execute(engine, message, order):
    db = engine.db
    rule = choose(engine, message.text, order)
    if not rule:
        return False
    key = f"scenario_cooldown:{message.chat_id}:{rule['id']}"
    if db.get(key, 0) > now():
        return True  # matching rule owns this message; no fallback duplicates
    text = rule["reply"]
    if rule["action"] == "instruction":
        binding = (
            db.one("SELECT instruction FROM bindings WHERE item_id=?", (order["item_id"],)) if order else None
        )
        if not binding or not binding["instruction"]:
            return False
        text = binding["instruction"]
    elif rule["action"] == "operator":
        text = "Обращение передано продавцу."
    with db.transaction():
        db.enqueue(
            "playerok",
            message.chat_id,
            text,
            f"scenario:{rule['id']}:{message.id}",
            purpose="reply",
            order_id=order["id"] if order else None,
            expires_at=now() + rule["delay"] + 300,
        )
        db.execute(
            "UPDATE outbox SET not_before=? WHERE dedup=?",
            (now() + rule["delay"], f"scenario:{rule['id']}:{message.id}"),
        )
        if rule["action"] == "operator":
            # The acknowledgement itself must survive operator pause.
            db.execute(
                "UPDATE outbox SET purpose='operator_ack' WHERE dedup=?",
                (f"scenario:{rule['id']}:{message.id}",),
            )
            db.execute("UPDATE chats SET operator_paused=1 WHERE id=?", (message.chat_id,))
            db.notify(
                "💬 Покупатель ожидает продавца.",
                "scenario-operator:" + message.id,
                order["id"] if order else None,
            )
        db.set(key, now() + rule["cooldown"])
        db.set(
            "scenario_last:" + message.chat_id,
            {"rule": rule["id"], "condition": rule["condition"], "action": rule["action"], "at": now()},
        )
    return True
