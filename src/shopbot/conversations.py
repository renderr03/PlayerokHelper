from __future__ import annotations

from datetime import datetime

from .engine import Engine
from .loyalty import Loyalty
from .models import Message, ServiceError, now


class Conversations:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.db = engine.db

    async def poll(self):
        if self.db.get("paused"):
            return
        async for chat in self.engine.market.chats():
            since = self.db.get("chat_cursor:" + chat["id"], self.db.get("cutoff"))
            messages = await self.engine.market.messages(chat["id"], since)
            for message in messages:
                await self.handle(message)
            if messages:
                self.db.set("chat_cursor:" + chat["id"], max(m.created_at for m in messages))
        self.ignore_messages()

    def cache_message(self, message):
        sender = "seller" if message.user_id == self.engine.market.user_id else "buyer"
        needs = (
            sender == "buyer"
            and not message.system
            and (not message.text.strip().startswith("!") or message.text.strip().startswith("!оператор"))
        )
        self.db.execute("INSERT OR IGNORE INTO chats(id) VALUES (?)", (message.chat_id,))
        self.db.execute(
            "INSERT OR IGNORE INTO chat_messages VALUES (?,?,?,?,?,?)",
            (
                message.id,
                message.chat_id,
                sender,
                self.db.encrypt(message.text[:4000]),
                message.created_at,
                int(needs),
            ),
        )

    async def handle(self, message: Message):
        self.cache_message(message)
        if self.db.one("SELECT id FROM events WHERE id=?", ("message:" + message.id,)):
            return
        if message.created_at < self.db.get("cutoff", now()):
            return
        self.db.execute("INSERT OR IGNORE INTO chats(id) VALUES (?)", (message.chat_id,))
        chat = self.db.one("SELECT * FROM chats WHERE id=?", (message.chat_id,))
        if message.system:
            self.done(message)
            return
        if message.user_id == self.engine.market.user_id:
            # Do not let bot-generated replies count as unanswered seller messages.
            ours = self.db.one(
                "SELECT id FROM outbox WHERE channel='playerok' AND remote_id=?", (message.id,)
            )
            if not ours:
                self.db.execute(
                    "UPDATE chats SET last_seller_at=? WHERE id=?", (message.created_at, message.chat_id)
                )
            self.done(message)
            return
        self.db.execute("UPDATE chats SET last_buyer_at=? WHERE id=?", (message.created_at, message.chat_id))
        self.db.execute(
            "UPDATE jobs SET state='cancelled' WHERE kind IN ('confirm_reminder','review_reminder') "
            "AND state='pending' AND order_id IN (SELECT id FROM orders WHERE chat_id=?)",
            (message.chat_id,),
        )
        self.db.execute(
            "UPDATE outbox SET state='cancelled' WHERE channel='playerok' AND target=? "
            "AND purpose IN ('confirm_reminder','review_reminder') AND state='pending'",
            (message.chat_id,),
        )
        order = self.db.one(
            "SELECT * FROM orders WHERE chat_id=? AND buyer_id=? ORDER BY created_at DESC LIMIT 1",
            (message.chat_id, message.user_id),
        )
        item_id = order["item_id"] if order else None
        text = message.text.strip()
        if text.startswith("!"):
            await self.command(message)
            self.done(message)
            return
        self.db.notify(f"Новое сообщение в чате {message.chat_id}\n{text[:1200]}", "incoming:" + message.id)
        if chat["operator_paused"]:
            self.done(message)
            return
        if self.db.feature("greeting", item_id) and now() - chat["greeted_at"] >= 86400:
            self.reply(message, self.engine.render("greeting", order), "greeting")
            self.db.execute("UPDATE chats SET greeted_at=? WHERE id=?", (now(), message.chat_id))
        local = datetime.now(self.engine.tz).hour
        start, end = self.db.get("work_start"), self.db.get("work_end")
        working = start <= local < end if start < end else local >= start or local < end
        if self.db.feature("offline", item_id) and (self.db.get("offline_override") or not working):
            self.cooldown_reply(message, self.engine.render("offline", order), "offline", 3600)
        rules = self.db.setting("rules", item_id) or []
        matching = [r for r in rules if r.get("phrase", "").casefold() in text.casefold() and r.get("phrase")]
        matching.sort(key=lambda r: (r.get("priority", 0), len(r["phrase"])), reverse=True)
        if matching:
            rule = matching[0]
            self.cooldown_reply(message, rule["reply"], "rule:" + rule["phrase"], 60)
        self.done(message)

    def done(self, message):
        self.db.execute("INSERT OR IGNORE INTO events VALUES (?,?)", ("message:" + message.id, now()))

    def reply(self, message, text, suffix="command", expires_at=None):
        self.db.enqueue(
            "playerok",
            message.chat_id,
            text[:3900],
            f"reply:{message.id}:{suffix}",
            purpose="reply",
            expires_at=expires_at,
        )

    def cooldown_reply(self, message, text, name, cooldown):
        key = f"reply_cooldown:{message.chat_id}:{name}"
        if self.db.get(key, 0) <= now():
            self.reply(message, text, name)
            self.db.set(key, now() + cooldown)

    async def command(self, message):
        parts = message.text.strip().split(maxsplit=1)
        command = parts[0].casefold()
        reference = parts[1].strip() if len(parts) > 1 else ""
        if command == "!оператор":
            self.db.execute("UPDATE chats SET operator_paused=1 WHERE id=?", (message.chat_id,))
            self.db.notify(
                f"Покупатель вызвал продавца. Чат {message.chat_id}. "
                f"Ответить: /reply {message.chat_id} <текст>",
                "operator:" + message.id,
            )
            # This acknowledgement must be delivered despite operator pause.
            self.db.enqueue(
                "playerok", message.chat_id, "Передали обращение продавцу.", "operator_ack:" + message.id
            )
            return
        if command == "!помощь":
            self.reply(
                message,
                "!аренды — ваши аренды\n!код <номер> — Steam Guard\n"
                "!срок <номер> — срок аренды\n!продлить <номер> — выбрать аренду для продления\n"
                "!инструкция — инструкция заказа\n!оператор — позвать продавца",
            )
            return
        if command == "!бонус":
            if not self.db.get("loyalty_enabled"):
                self.reply(message, "Бонусная программа пока выключена.")
            else:
                count = Loyalty(self.engine).count(message.user_id)
                threshold = self.db.get("loyalty_every")
                self.reply(
                    message,
                    f"Завершённых покупок в программе: {count}. Подарок за каждые {threshold} покупок; до следующего: {threshold - count % threshold}.",
                )
            return
        if command == "!аренды":
            rows = self.db.all(
                "SELECT * FROM orders WHERE buyer_id=? AND module IN ('kosell','own_rent') "
                "AND stage='delivered' AND market_status!='ROLLED_BACK' ORDER BY created_at DESC",
                (message.user_id,),
            )
            text = "\n".join(
                f"{r['rental_number'] or r['id']}: {r['title']} — до {self.engine.expiry(r['expires_at'])}"
                for r in rows
            )
            self.reply(message, text[:3800] or "У вас пока нет выданных аренд.")
            return
        if command == "!инструкция":
            row = self.db.one(
                "SELECT b.instruction FROM orders o JOIN bindings b ON b.item_id=o.item_id "
                "WHERE o.buyer_id=? AND o.chat_id=? ORDER BY o.created_at DESC LIMIT 1",
                (message.user_id, message.chat_id),
            )
            self.reply(
                message, (row or {}).get("instruction") or "Инструкция пока не добавлена. Напишите !оператор."
            )
            return
        if command not in {"!код", "!срок", "!продлить"}:
            self.reply(message, "Неизвестная команда. Список: !помощь")
            return
        rental = self.engine.owned_rental(message.user_id, reference) if reference else None
        if not rental:
            self.reply(message, "Аренда не найдена. Используйте !аренды и укажите её номер.")
            return
        if command == "!срок":
            self.reply(message, "Аренда до " + self.engine.expiry(rental["expires_at"]))
            return
        if rental["expires_at"] <= now():
            self.reply(message, "Срок аренды истёк. Для помощи напишите !оператор.")
            return
        if command == "!код" and rental["module"] == "own_rent":
            self.db.execute("UPDATE chat_messages SET needs_reply=1 WHERE id=?", (message.id,))
            self.db.notify(
                "Запрошен код для собственной аренды " + rental["id"], "own-code:" + message.id, rental["id"]
            )
            self.reply(message, "Запрос кода передан продавцу. Также доступна команда !оператор.")
            return
        if command == "!код":
            throttle = "guard:" + rental["rental_uid"]
            if self.db.get(throttle, 0) > now():
                self.reply(message, "Подождите перед следующим запросом Steam Guard.")
                return
            self.db.set(throttle, now() + 30)
            try:
                response = await self.engine.kosell.code(rental["rental_uid"])
            except ServiceError as exc:
                self.db.set(throttle, now() + max(30, exc.retry_after))
                self.reply(message, "Код пока недоступен. Попробуйте позже или напишите !оператор.")
                return
            if not response.get("success") or not response.get("code"):
                self.reply(message, "KOSell пока не выдал код. Попробуйте позже.")
                return
            self.reply(
                message,
                f"Steam Guard: {response['code']}\n"
                f"Срок действия: {response.get('expires_in') or 'ограничен'} сек.",
                expires_at=now() + (response.get("expires_in") or 30),
            )
            return
        waiting = self.db.all(
            "SELECT o.* FROM orders o JOIN bindings b ON b.item_id=o.item_id WHERE o.buyer_id=? "
            "AND o.stage='wait_selection' AND ((o.module='extend' AND ?='kosell' AND b.product_id=?) OR (o.module='own_extend' AND ?='own_rent' AND b.pool_item=?)) ORDER BY o.created_at",
            (message.user_id, rental["module"], rental["product_id"], rental["module"], rental["item_id"]),
        )
        if waiting:
            self.engine.update(waiting[0]["id"], target_order_id=rental["id"], stage="extension_ready")
            await self.engine.process(waiting[0]["id"])
            self.reply(message, "Аренда выбрана для оплаченного продления.")
        else:
            self.db.execute(
                "INSERT OR REPLACE INTO selections VALUES (?,?,?)",
                (message.user_id, rental["id"], now() + 1800),
            )
            self.reply(
                message,
                "Аренда выбрана на 30 минут. Оплатите лот продления для этой игры. "
                "Следующий подходящий заказ будет привязан к ней.",
            )

    def ignore_messages(self):
        for chat in self.db.all(
            "SELECT * FROM chats WHERE operator_paused=0 AND last_seller_at>last_buyer_at "
            "AND last_seller_at>ignore_sent_for AND last_seller_at<?",
            (now() - 1800,),
        ):
            order = self.db.one(
                "SELECT * FROM orders WHERE chat_id=? AND market_status IN ('PAID','SENT') "
                "AND problem=0 AND historical=0 ORDER BY created_at DESC LIMIT 1",
                (chat["id"],),
            )
            if order and self.db.feature("ignore", order["item_id"]):
                self.db.enqueue(
                    "playerok",
                    chat["id"],
                    self.engine.render("ignore", order),
                    f"ignore:{chat['id']}:{chat['last_seller_at']}",
                    order_id=order["id"],
                    purpose="reply",
                )
                self.db.execute("UPDATE chats SET ignore_sent_for=last_seller_at WHERE id=?", (chat["id"],))
