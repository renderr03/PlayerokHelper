"""Escaped Telegram HTML cards and compact contextual actions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from html import escape
from urllib.parse import quote

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .analytics import Analytics


def clip(value, units):
    text = str(value or "")
    raw = text.encode("utf-16-le")
    return (
        text if len(raw) <= units * 2 else raw[: (units - 1) * 2].decode("utf-16-le", errors="ignore") + "…"
    )


def safe(value, units=250):
    return escape(clip(value, units))


def link(label, path):
    return '<a href="https://playerok.com/' + escape(path, quote=True) + '">' + safe(label) + "</a>"


def price(value):
    if value is None:
        return "неизвестно"
    return f"{Decimal(value) / 100:,.2f}".replace(",", " ") + " ₽"


def markup(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in rows
            if row
        ]
    )


@dataclass
class Card:
    text: str
    keyboard: InlineKeyboardMarkup


class Cards:
    def __init__(self, engine):
        self.engine, self.db = engine, engine.db

    def seller(self):
        return safe((self.db.get("profile") or {}).get("username") or "вашего магазина", 80)

    def buyer(self, order, fallback="не указан"):
        name = (order or {}).get("buyer_name") or fallback
        if order and re.fullmatch(r"[A-Za-z0-9_]{1,80}", str(name)):
            return link(name, "profile/" + quote(str(name), safe=""))
        return safe(name, 80)

    def chat_keys(self, chat_id, order=None):
        chat = self.db.one("SELECT rowid rid FROM chats WHERE id=?", (chat_id,))
        rows = []
        if chat:
            rid = str(chat["rid"])
            rows.append(
                [
                    ("✉️ Ответ", "card:reply:" + rid),
                    ("📋 Шаблоны", "card:quick:" + rid),
                    ("💬 Весь чат", "card:chat:" + rid),
                ]
            )
        if order:
            rows += self.order_keys(order)
        if not rows:
            rows.append([("💬 Рабочее окно", "card:inbox")])
        return rows

    def order_keys(self, order):
        rid = str(order["rid"])
        actions = []
        if order["stage"] == "delivered" and not order["problem"] and order["market_status"] == "PAID":
            actions.append(("✅ Подтвердить", "card:sent:" + rid))
        if order["market_status"] != "ROLLED_BACK":
            actions.append(("↩️ Возврат", "card:refund:" + rid))
        result = [actions] if actions else []
        row = [("🧾 Заказ", "card:order:" + rid)]
        if self.db.one("SELECT item_id FROM bindings WHERE item_id=?", (order["item_id"],)):
            row.append(("📦 Лот", "card:lot:" + rid))
        result.append(row)
        return result

    def order(self, order_id, kind):
        order = self.db.one("SELECT rowid rid,* FROM orders WHERE id=?", (order_id,))
        if not order:
            return None
        chat_id = order["chat_id"]
        # Card actions need a stable local chat reference even before chat polling.
        self.db.execute("INSERT OR IGNORE INTO chats(id) VALUES (?)", (chat_id,))
        if kind == "new":
            local = datetime.fromtimestamp(order["created_at"], self.engine.tz)
            number = self.db.get("order_number:" + order_id)
            if number is None:
                start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                end = (
                    local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                ).timestamp()
                number = self.db.one(
                    "SELECT count(*) n FROM orders WHERE historical=0 AND module IS NOT NULL AND created_at>=? AND created_at<? AND (created_at<? OR (created_at=? AND rowid<=?))",
                    (start, end, order["created_at"], order["created_at"], order["rid"]),
                )["n"]
                self.db.set("order_number:" + order_id, number)
            day = (
                "за сегодня"
                if local.date() == datetime.now(self.engine.tz).date()
                else "за " + local.strftime("%d.%m")
            )
            title = f"🛍 Заказ №{number} {day}"
        elif kind == "refund":
            title = "↩️ Возврат средств по заказу"
        elif kind == "details":
            title = "🧾 Заказ"
        else:
            title = "📦 Заказ выдан"
        item = order["item_id"]
        product = (
            link(order["title"], "products/" + quote(item, safe=""))
            if re.fullmatch(r"[0-9a-fA-F-]{36}", item)
            else safe(order["title"])
        )
        text = (
            f"<b>{title}</b>\nдля <b>{self.seller()}</b>\n\n"
            f"<b>Товар:</b> {product}\n<b>Сумма:</b> {price(order['price'])}\n"
            f"<b>Покупатель:</b> {self.buyer(order)}\n\n"
            f"<b>Ссылка на чат:</b> {link('Открыть чат', 'chats/' + quote(chat_id, safe=''))}\n"
            f"<b>Заказ:</b> {safe(order_id, 80)}"
        )
        return Card(text, markup(self.chat_keys(chat_id, order)))

    def message(self, payload):
        data = json.loads(payload)
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("chat_id"), str)
            or not isinstance(data.get("text"), str)
        ):
            raise ValueError("Invalid chat card")
        chat_id = data["chat_id"]
        order = self.db.one(
            "SELECT rowid rid,* FROM orders WHERE chat_id=? AND buyer_id=? ORDER BY created_at DESC LIMIT 1",
            (chat_id, data.get("buyer_id", "")),
        )
        identity = order or ({"buyer_name": data["buyer_name"]} if data.get("buyer_name") else None)
        buyer = self.buyer(identity, data.get("buyer_id") or "не указан")
        system = data.get("source") == "playerok"
        header = "📣 Новое сообщение от Playerok" if system else "✉️ Новое сообщение от клиента"
        text = (
            f"<b>{header}</b>\nдля <b>{self.seller()}</b>\n\n"
            + ("" if system else f"<b>Покупатель:</b> {buyer}\n\n")
            + safe(data["text"], 1200)
            + "\n\n"
            + f"<b>Ссылка на чат:</b> {link('Открыть чат', 'chats/' + quote(chat_id, safe=''))}"
        )

        return Card(text, markup(self.chat_keys(chat_id, order)))

    def daily(self, start, end):
        analytics = Analytics(self.db)
        rows = analytics.orders(start, end)
        summary = analytics.summary(rows, start, end)
        begin = datetime.fromtimestamp(start, self.engine.tz)
        finish = datetime.fromtimestamp(end, self.engine.tz)
        zone = "МСК" if self.engine.tz.key == "Europe/Moscow" else self.engine.tz.key
        known = summary["cost"] + summary["promotion"] + summary["bonus_cost"] + (summary["commission"] or 0)
        profit = "Прибыль" if summary["complete"] else "Предварительная прибыль"
        reviews = sum(row["rating"] is not None for row in rows)
        return (
            f"<b>📊 Итоги за день</b>\nАккаунт: <b>{self.seller()}</b>\n"
            f"{begin:%d.%m %H:%M} — {finish:%d.%m %H:%M} {safe(zone, 40)}\n\n"
            f"🛍 Заказов за период: <b>{summary['orders']}</b>\n"
            f"💰 Выручка: <b>{price(summary['gross'])}</b>\n"
            f"💸 Известные расходы: {price(known)}\n"
            f"🚀 Из них продвижение: {price(summary['promotion'])}\n"
            f"📈 {profit}: <b>{price(summary['profit'])}</b>\n"
            f"⭐ Отзывы по этим заказам: {reviews}\n\n"
            + (
                "Все расходы учтены."
                if summary["complete"]
                else "Расчёт предварительный: проверьте комиссию, неизвестные расходы и незавершённые закупки."
            )
            + "\nВыручка — по подтверждённым заказам.\nПодробности и настройка отчёта — в «Аналитике»."
        )

    def route(self, data):
        if data in {"card:analytics", "card:reports"}:
            return "menu:analytics"
        if data == "card:inbox":
            return "s:inbox:new:0"
        parts = data.split(":")
        if len(parts) != 3 or not parts[2].isdigit():
            return None
        action, rid = parts[1:]
        if action in {"reply", "quick", "chat"}:
            if not self.db.one("SELECT id FROM chats WHERE rowid=?", (int(rid),)):
                return None
            return {"reply": "s:reply:", "quick": "s:quick:choose:", "chat": "s:chat:"}[action] + rid
        order = self.db.one("SELECT id,item_id FROM orders WHERE rowid=?", (int(rid),))
        if not order:
            return None
        if action == "lot":
            return "lot:" + order["item_id"]
        if action in {"order", "sent", "refund"}:
            return action + ":" + order["id"]
        return None
