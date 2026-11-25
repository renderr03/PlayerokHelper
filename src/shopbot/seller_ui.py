"""Seller-facing workspace, catalog tools, own rentals and purchase rewards."""

from __future__ import annotations

import hashlib
import json
import uuid

from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from .loyalty import Loyalty
from .maintenance import diagnostics, update_status
from .models import now, rub
from .rentals import Rentals

MODE_NAMES = {
    "stock": "Склад",
    "kosell": "KOSell",
    "extend": "Продление KOSell",
    "own_rent": "Своя аренда",
    "own_extend": "Своё продление",
    "stars": "Telegram Stars",
    "smm": "SMM-услуги",
}


def buttons(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=value)] for label, value in rows]
    )


class SellerUI:
    async def seller_prompt(self, message, state, key, prompt, **context):
        from .telegram_ui import Form

        await state.set_state(Form.value)
        await state.update_data(edit="s:" + key, **context)
        await message.answer(prompt)

    async def workspace(self, message, state=None, *, page=0, view="new"):
        if state:
            await state.set_state(None)
        where = {
            "new": "EXISTS(SELECT 1 FROM chat_messages m WHERE m.chat_id=c.id AND m.needs_reply=1 AND m.created_at>c.panel_seen_at)",
            "waiting": "EXISTS(SELECT 1 FROM chat_messages m WHERE m.chat_id=c.id AND m.needs_reply=1 AND m.created_at>c.last_seller_at)",
            "problem": "EXISTS(SELECT 1 FROM orders o WHERE o.chat_id=c.id AND (o.problem=1 OR o.stage IN ('failed','review')))",
            "all": "1=1",
        }.get(view, "1=1")
        page = max(0, page)
        count = self.db.one("SELECT count(*) n FROM chats c WHERE " + where)["n"]
        rows = self.db.all(
            "SELECT c.rowid AS rid,c.* FROM chats c WHERE "
            + where
            + " ORDER BY c.last_buyer_at DESC,c.id LIMIT 20 OFFSET ?",
            (page * 20,),
        )
        keyboard = [
            ("Новые в панели", "s:inbox:new:0"),
            ("Ждут ответа", "s:inbox:waiting:0"),
            ("Проблемные заказы", "s:inbox:problem:0"),
            ("Все чаты", "s:inbox:all:0"),
        ]
        for row in rows:
            buyer = self.db.one(
                "SELECT buyer_name FROM orders WHERE chat_id=? ORDER BY created_at DESC LIMIT 1", (row["id"],)
            )
            keyboard.append(
                (
                    (buyer or {}).get("buyer_name", "Чат")[:35] + " · " + row["id"][:12],
                    "s:chat:" + str(row["rid"]),
                )
            )
        if page:
            keyboard.append(("←", f"s:inbox:{view}:{page - 1}"))
        if (page + 1) * 20 < count:
            keyboard.append(("→", f"s:inbox:{view}:{page + 1}"))
        keyboard += [("✏ Быстрые ответы", "s:quick:edit"), ("‹ В меню", "menu:home")]
        await message.answer(
            f"💬 Рабочее окно продавца · {count} чатов · страница {page + 1}\n"
            "Отметки просмотра относятся к этой панели. История сохраняется 30 дней.",
            reply_markup=buttons(keyboard),
        )

    async def chat_card(self, message, state, rid):
        chat = self.db.one("SELECT rowid AS rid,* FROM chats WHERE rowid=?", (rid,))
        if not chat:
            return await message.answer("Чат не найден.")
        history = self.db.all(
            "SELECT * FROM chat_messages WHERE chat_id=? ORDER BY created_at DESC,id DESC LIMIT 10",
            (chat["id"],),
        )
        seen = max([r["created_at"] for r in history] or [now()])
        self.db.execute("UPDATE chats SET panel_seen_at=max(panel_seen_at,?) WHERE id=?", (seen, chat["id"]))
        orders = self.db.all(
            "SELECT * FROM orders WHERE chat_id=? ORDER BY created_at DESC LIMIT 5", (chat["id"],)
        )
        lines = ["💬 Чат " + chat["id"], "Последние сообщения (до 10):"]
        for row in reversed(history):
            lines.append(
                ("Покупатель: " if row["sender"] == "buyer" else "Продавец: ")
                + self.db.decrypt(row["ciphertext"])[:220]
            )
        keys = [("✍ Ответить", "s:reply:" + str(rid)), ("⚡ Быстрый ответ", "s:quick:choose:" + str(rid))]
        keys += [(r["stage"] + " · " + r["title"][:25], "order:" + r["id"]) for r in orders]
        keys += [("‹ Чаты", "s:inbox:all:0")]
        await message.answer("\n".join(lines)[:3800], reply_markup=buttons(keys))

    async def catalog(self, message, state, *, page=None, module=None, role=None):
        context = await state.get_data() if state else {}
        cat = context.get("catalog", {"module": "", "status": "all", "search": "", "page": 0, "role": "lot"})
        if module is not None:
            cat.update(module=module, page=0)
        if page is not None:
            cat["page"] = max(0, page)
        if role:
            cat["role"] = role
        clauses, args = ["1=1"], []
        if cat["module"]:
            clauses.append("coalesce(nullif(fulfillment,''),module)=?")
            args.append(cat["module"])
        if cat["search"]:
            clauses.append("(instr(casefold(title),?)>0 OR instr(casefold(item_id),?)>0)")
            args += [cat["search"].casefold()] * 2
        if cat["status"] == "paused":
            clauses.append("paused=1")
        elif cat["status"] == "hidden":
            clauses.append("hidden_by_bot=1")
        where = " AND ".join(clauses)
        count = self.db.one("SELECT count(*) n FROM bindings WHERE " + where, tuple(args))["n"]
        cat["page"] = min(cat["page"], max(0, (count - 1) // 20))
        rows = self.db.all(
            "SELECT rowid AS rid,* FROM bindings WHERE "
            + where
            + " ORDER BY casefold(title),item_id LIMIT 20 OFFSET ?",
            (*args, cat["page"] * 20),
        )
        selected = context.get("selected_lots", [])
        if state:
            await state.update_data(catalog=cat, catalog_page=[r["item_id"] for r in rows])
        keyboard = []
        for row in rows:
            callback = (
                "scope:" if cat["role"] == "scope" else "reportitem:" if cat["role"] == "report" else "lot:"
            ) + row["item_id"]
            line = [
                InlineKeyboardButton(
                    text=("⏸ " if row["paused"] else "📦 ") + (row["title"][:32] or row["item_id"][:16]),
                    callback_data=callback,
                )
            ]
            if cat["role"] == "lot":
                line.append(
                    InlineKeyboardButton(
                        text="☑" if row["item_id"] in selected else "☐",
                        callback_data="s:select:" + str(row["rid"]),
                    )
                )
            keyboard.append(line)
        controls = [
            ("🔎 Найти", "s:search"),
            ("Все", "s:filter:all"),
            ("Пауза", "s:filter:paused"),
            ("Скрыты ботом", "s:filter:hidden"),
        ]
        if cat["page"]:
            controls.append(("←", f"s:page:{cat['page'] - 1}"))
        if (cat["page"] + 1) * 20 < count:
            controls.append(("→", f"s:page:{cat['page'] + 1}"))
        if cat["role"] == "lot":
            controls += [
                ("Выбрать страницу", "s:select-page"),
                (f"Изменить выбранные ({len(selected)})", "s:bulk"),
                ("Снять выбор", "s:clear"),
                ("➕ Привязать лот", "bind:new"),
            ]
        elif cat["role"] == "scope":
            controls.append(("Весь магазин", "scope:global"))
        controls.append(("‹ В меню", "menu:home"))
        keyboard += buttons(controls).inline_keyboard
        await message.answer(
            f"📦 Каталог · найдено {count} · страница {cat['page'] + 1}\n"
            f"Поиск: {cat['search'] or '—'} · выбранных: {len(selected)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        )

    def bulk_fingerprint(self, row, field):
        value = self.db.get("override:" + row["item_id"], {}) if field == "rules" else row.get(field)
        return hashlib.sha256(
            json.dumps([row["module"], row["fulfillment"], value], sort_keys=True).encode()
        ).hexdigest()

    async def bulk_preview(self, message, state, field, value):
        selected = (await state.get_data()).get("selected_lots", [])
        if not selected:
            raise ValueError("Сначала выберите лоты.")
        snapshot = {}
        for item in selected:
            row = self.db.one("SELECT * FROM bindings WHERE item_id=?", (item,))
            if row:
                snapshot[item] = self.bulk_fingerprint(row, field)
        token = uuid.uuid4().hex[:12]
        await state.set_state(None)
        await state.update_data(bulk={"field": field, "value": value, "snapshot": snapshot, "token": token})
        lines = [f"Изменение: {field}. Значение: {str(value)[:1000]}", f"Лотов: {len(snapshot)}"]
        lines += list(snapshot)[:20]
        lines.append(
            "Изменившиеся после предпросмотра лоты будут пропущены. Платные операции здесь не выполняются."
        )
        await message.answer(
            "\n".join(lines),
            reply_markup=buttons([("Применить", "s:apply:" + token), ("Отмена", "s:cancel-bulk")]),
        )

    async def apply_bulk(self, message, state, token):
        bulk = (await state.get_data()).get("bulk") or {}
        if bulk.get("token") != token:
            return await message.answer("Предпросмотр устарел.")
        await state.update_data(bulk=None)
        changed, skipped, outcomes = 0, [], []
        for item, fingerprint in bulk["snapshot"].items():
            row = self.db.one("SELECT * FROM bindings WHERE item_id=?", (item,))
            if not row or self.bulk_fingerprint(row, bulk["field"]) != fingerprint:
                skipped.append(item + ": изменён")
                outcomes.append(item + ": пропущен — изменён после предпросмотра")
                continue
            field, value = bulk["field"], bulk["value"]
            if field == "max_cost" and row["module"] in {"kosell", "extend"} and value <= 0:
                skipped.append(item + ": нужен положительный лимит")
                outcomes.append(item + ": пропущен — нужен положительный лимит")
                continue
            with self.db.transaction():
                if field == "rules":
                    overrides = self.db.get("override:" + item, {})
                    overrides["rules"] = value
                    self.db.set("override:" + item, overrides)
                elif field in {"instruction", "max_cost", "paused"}:
                    self.db.execute(f"UPDATE bindings SET {field}=? WHERE item_id=?", (value, item))
                else:
                    raise ValueError("Недопустимое поле")
                self.db.audit("bulk_settings_changed", item)
                changed += 1
                outcomes.append(item + ": сохранено")
        await state.update_data(selected_lots=[])
        await message.answer_document(
            BufferedInputFile("\n".join(outcomes).encode(), filename="bulk-results.txt"),
            caption="Результат по каждому выбранному лоту.",
        )
        await message.answer(
            f"Сохранено: {changed}; пропущено: {len(skipped)}.\n" + "\n".join(skipped[:20]),
            reply_markup=buttons([("К каталогу", "s:page:0")]),
        )

    async def own_pool(self, message, item_id, page=0):
        binding = self.db.one(
            "SELECT rowid AS rid,* FROM bindings WHERE item_id=? AND fulfillment='own_rent'", (item_id,)
        )
        if not binding:
            return await message.answer("Выберите лот собственной аренды.")
        rows = self.db.all(
            "SELECT * FROM own_accounts WHERE item_id=? ORDER BY id LIMIT 20 OFFSET ?",
            (item_id, max(0, page) * 20),
        )
        counts = self.db.all(
            "SELECT state,count(*) n FROM own_accounts WHERE item_id=? GROUP BY state", (item_id,)
        )
        keys = [("📥 Загрузить аккаунты", "s:own-upload:" + str(binding["rid"]))]
        for account in rows:
            label = {
                "free": "свободен",
                "reserved": "выдаётся",
                "active": "в аренде",
                "reset": "нужна смена доступа",
            }[account["state"]]
            if account["state"] == "reset":
                keys.append((f"#{account['id']} · {label}", "s:reset:" + str(account["id"])))
            else:
                keys.append((f"#{account['id']} · {label}", "s:account:" + str(account["id"])))
        if page:
            keys.append(("←", f"s:pool:{binding['rid']}:{page - 1}"))
        if (
            self.db.one("SELECT count(*) n FROM own_accounts WHERE item_id=?", (item_id,))["n"]
            > (page + 1) * 20
        ):
            keys.append(("→", f"s:pool:{binding['rid']}:{page + 1}"))
        keys.append(("‹ К лоту", "lot:" + item_id))
        await message.answer(
            "🎮 Собственный пул: "
            + binding["title"]
            + "\n"
            + ", ".join(f"{r['state']}: {r['n']}" for r in counts)
            + "\nТаймер начинается при резервировании. Доступ прекращает продавец сменой реквизитов; затем подтверждает готовность.",
            reply_markup=buttons(keys),
        )

    async def loyalty_menu(self, message):
        counts = self.db.all("SELECT state,count(*) n FROM bonus_stock GROUP BY state")
        await message.answer(
            "🎁 Бонусные товары\n"
            + ("Включено" if self.db.get("loyalty_enabled") else "Выключено")
            + f" · подарок за каждые {self.db.get('loyalty_every')} завершённых покупок.\n"
            + ", ".join(f"{r['state']}: {r['n']}" for r in counts)
            + "\nУчитываются новые покупки с момента начала программы. Возвраты и открытые проблемы исключаются. "
            "Выданный подарок отозвать нельзя. Стоимость подарков учитывается в аналитике.",
            reply_markup=buttons(
                [
                    ("Выключить" if self.db.get("loyalty_enabled") else "Включить", "s:loyalty-toggle"),
                    ("Порог / новая программа", "s:loyalty-threshold"),
                    ("Загрузить бонусы", "s:bonus-upload"),
                    ("Инструкция к подарку", "s:bonus-instruction"),
                    ("Спорные подарки", "s:bonus-review:0"),
                    ("‹ В меню", "menu:home"),
                ]
            ),
        )

    async def pool_target(self, message, rid, page):
        row = self.db.one("SELECT * FROM bindings WHERE rowid=? AND fulfillment='own_extend'", (rid,))
        if not row:
            return
        rows = self.db.all(
            "SELECT rowid AS rid,* FROM bindings WHERE fulfillment='own_rent' ORDER BY item_id LIMIT 20 OFFSET ?",
            (max(0, page) * 20,),
        )
        keys = [(r["title"][:35] or r["item_id"], f"s:link-pool:{rid}:{r['rid']}") for r in rows]
        if page:
            keys.append(("←", f"s:pool-target:{rid}:{page - 1}"))
        if len(rows) == 20:
            keys.append(("→", f"s:pool-target:{rid}:{page + 1}"))
        await message.answer(
            "Выберите пул, для которого продаётся это продление.", reply_markup=buttons(keys)
        )

    async def seller_callback(self, message, data, state):
        if not data.startswith("s:"):
            return False
        parts = data.split(":")
        action = parts[1]
        if action == "inbox":
            await self.workspace(message, state, view=parts[2], page=int(parts[3]))
        elif action == "own-catalog":
            await self.catalog(message, state, module="own_rent", role="lot", page=0)
        elif action == "chat":
            await self.chat_card(message, state, int(parts[2]))
        elif action == "reply":
            from .telegram_ui import Form

            chat = self.db.one("SELECT id FROM chats WHERE rowid=?", (int(parts[2]),))
            if chat:
                await state.update_data(chat_id=chat["id"])
                await state.set_state(Form.reply)
                await message.answer("Напишите ответ. Перед отправкой будет предпросмотр.")
        elif action == "quick":
            if parts[2] == "edit":
                await self.seller_prompt(
                    message,
                    state,
                    "quick",
                    "По одной строке: название | текст. До 30 заготовок. '-' — очистить.",
                )
            elif parts[2] == "choose":
                chat = self.db.one("SELECT id FROM chats WHERE rowid=?", (int(parts[3]),))
                if chat:
                    await state.update_data(chat_id=chat["id"])
                    keys = [
                        (name, f"s:quick:use:{index}")
                        for index, name in enumerate(self.db.get("quick_replies"))
                    ]
                    await message.answer(
                        "Выберите заготовку для предпросмотра.",
                        reply_markup=buttons(keys + [("‹ Чаты", "s:inbox:all:0")]),
                    )
            else:
                values = list(self.db.get("quick_replies").values())
                context = await state.get_data()
                index = int(parts[3])
                if "chat_id" in context and 0 <= index < len(values):
                    await self.reply_preview(message, state, values[index])
        elif action == "page":
            await self.catalog(message, state, page=int(parts[2]))
        elif action == "search":
            await self.seller_prompt(
                message, state, "search", "Введите часть названия или ID. '-' — сбросить поиск."
            )
        elif action == "filter":
            context = await state.get_data()
            cat = context.get("catalog", {"module": "", "search": "", "role": "lot", "page": 0})
            cat.update(status=parts[2], page=0)
            await state.update_data(catalog=cat)
            await self.catalog(message, state)
        elif action in {"select", "select-page", "clear"}:
            context = await state.get_data()
            selected = context.get("selected_lots", [])
            if action == "clear":
                selected = []
            elif action == "select":
                row = self.db.one("SELECT item_id FROM bindings WHERE rowid=?", (int(parts[2]),))
                if row:
                    item = row["item_id"]
                    selected = [x for x in selected if x != item] if item in selected else [*selected, item]
            else:
                selected = list(dict.fromkeys([*selected, *context.get("catalog_page", [])]))
            if len(selected) > 200:
                raise ValueError("Не более 200 лотов за одно изменение.")
            await state.update_data(selected_lots=selected)
            await self.catalog(message, state)
        elif action == "bulk":
            await message.answer(
                "Что изменить у выбранных лотов?",
                reply_markup=buttons(
                    [
                        ("Инструкция", "s:bulk-field:instruction"),
                        ("Лимит закупки", "s:bulk-field:max_cost"),
                        ("Правила автоответов", "s:bulk-field:rules"),
                        ("Поставить на паузу", "s:bulk-pause:1"),
                        ("Снять паузу", "s:bulk-pause:0"),
                        ("Отмена", "s:cancel-bulk"),
                    ]
                ),
            )
        elif action == "bulk-field":
            await state.update_data(bulk=None)
            prompt = {
                "instruction": "Новая инструкция, до 1000 символов. '-' — очистить.",
                "max_cost": "Максимальная закупочная стоимость, руб.",
                "rules": "Правила: фраза | ответ | приоритет, по одному в строке. '-' — очистить.",
            }[parts[2]]
            await self.seller_prompt(message, state, "bulk", prompt, bulk_field=parts[2])
        elif action == "bulk-pause":
            await self.bulk_preview(message, state, "paused", int(parts[2]))
        elif action == "apply":
            await self.apply_bulk(message, state, parts[2])
        elif action == "cancel-bulk":
            await state.update_data(bulk=None)
            await self.catalog(message, state)
        elif action == "pool":
            row = self.db.one("SELECT item_id FROM bindings WHERE rowid=?", (int(parts[2]),))
            if row:
                await self.own_pool(message, row["item_id"], int(parts[3]))
        elif action in {"own-upload", "bonus-upload"}:
            from .telegram_ui import Form

            item = (
                self.db.one("SELECT item_id FROM bindings WHERE rowid=?", (int(parts[2]),))
                if action == "own-upload"
                else None
            )
            if action == "own-upload" and not item:
                raise ValueError("Лот не найден")
            await state.update_data(
                warehouse="own" if item else "bonus", item_id=item["item_id"] if item else None
            )
            await state.set_state(Form.stock)
            await message.answer(
                "Пришлите TXT UTF-8 или текст: одна позиция в строке. Первая строка cost=12.50 задаёт себестоимость одной выдачи. "
                "Реквизиты хранятся зашифрованно."
            )
        elif action == "account":
            account = self.db.one("SELECT * FROM own_accounts WHERE id=?", (int(parts[2]),))
            if account:
                keys = []
                if account["order_id"]:
                    keys.append(("Открыть заказ", "order:" + account["order_id"]))
                if account["state"] == "free":
                    keys += [
                        ("Заменить реквизиты", "s:reset:" + str(account["id"])),
                        ("Остановить выдачу", "s:retire:" + str(account["id"])),
                    ]
                await message.answer(
                    f"Аккаунт #{account['id']} · {account['state']}\nАренда до {self.engine.expiry(account['expires_at'])}. "
                    "Реквизиты в карточке не показываются.",
                    reply_markup=buttons(keys),
                )
        elif action == "retire":
            self.db.execute(
                "UPDATE own_accounts SET state='reset' WHERE id=? AND state='free'", (int(parts[2]),)
            )
            await message.answer(
                "Свободный аккаунт выведен из выдачи до смены доступа и подтверждения готовности."
            )
        elif action == "reset":
            await self.seller_prompt(
                message,
                state,
                "reset",
                "Сначала смените доступ к аккаунту, затем пришлите новые реквизиты. "
                "Понадобится подтверждение готовности к повторной выдаче.",
                account_id=int(parts[2]),
            )
        elif action == "reset-confirm":
            context = await state.get_data()
            if context.get("reset_token") != parts[2]:
                raise ValueError("Подтверждение устарело")
            Rentals(self.engine).reset(
                context["account_id"],
                self.db.decrypt(context["reset_ciphertext"]),
                expected=context.get("reset_snapshot"),
            )
            await state.clear()
            await message.answer("Смена доступа подтверждена. Аккаунт доступен для следующей аренды.")
        elif action == "pool-target":
            await self.pool_target(message, int(parts[2]), int(parts[3]))
        elif action == "link-pool":
            pool = self.db.one(
                "SELECT item_id FROM bindings WHERE rowid=? AND fulfillment='own_rent'", (int(parts[3]),)
            )
            if pool:
                self.db.execute(
                    "UPDATE bindings SET pool_item=? WHERE rowid=? AND fulfillment='own_extend'",
                    (pool["item_id"], int(parts[2])),
                )
                await message.answer("Пул для продления сохранён.")
        elif action == "loyalty":
            await state.update_data(bonus_review=None)
            await self.loyalty_menu(message)
        elif action == "loyalty-toggle":
            if not self.db.get("loyalty_campaign"):
                Loyalty(self.engine).configure(self.db.get("loyalty_every"))
            else:
                self.db.set("loyalty_enabled", not self.db.get("loyalty_enabled"))
            await self.loyalty_menu(message)
        elif action == "loyalty-threshold":
            await self.seller_prompt(
                message,
                state,
                "loyalty",
                "Введите число покупок для подарка (1–10000). "
                "Начнётся новая программа: учитываются только новые заказы после сохранения.",
            )
        elif action == "bonus-instruction":
            await self.seller_prompt(
                message, state, "bonus_instruction", "Инструкция к подарку, до 1000 символов; '-' — убрать."
            )
        elif action == "bonus-review":
            page = max(0, int(parts[2]))
            claims = self.db.all(
                "SELECT c.id,c.order_id FROM bonus_claims c JOIN outbox b ON b.purpose='bonus:'||c.id "
                "WHERE c.state IN ('queued','review') AND b.state IN ('uncertain','failed') "
                "AND b.dedup='bonus:'||c.id||':'||c.generation ORDER BY c.id LIMIT 20 OFFSET ?",
                (page * 20,),
            )
            keys = [
                (f"Подарок #{r['id']} · заказ {r['order_id'][:20]}", "s:bonus-claim:" + str(r["id"]))
                for r in claims
            ]
            if page:
                keys.append(("←", f"s:bonus-review:{page - 1}"))
            if len(claims) == 20:
                keys.append(("→", f"s:bonus-review:{page + 1}"))
            await message.answer(
                "Сверьте историю чата перед подтверждением результата.",
                reply_markup=buttons(keys + [("‹ Бонусы", "s:loyalty")]),
            )
        elif action == "bonus-claim":
            claim = self.db.one("SELECT * FROM bonus_claims WHERE id=?", (int(parts[2]),))
            if claim:
                await message.answer(
                    "Подарок может быть уже получен. Проверьте чат заказа " + claim["order_id"] + ".",
                    reply_markup=buttons(
                        [
                            ("Получение подтверждено", f"s:bonus-settle:{claim['id']}:1"),
                            ("Передача не состоялась", f"s:bonus-settle:{claim['id']}:0"),
                            ("Открыть заказ", "order:" + claim["order_id"]),
                        ]
                    ),
                )
        elif action == "bonus-settle":
            token = uuid.uuid4().hex[:12]
            claim = self.db.one("SELECT generation FROM bonus_claims WHERE id=?", (int(parts[2]),))
            if not claim:
                raise ValueError("Подарок не найден")
            await state.update_data(
                bonus_review={
                    "token": token,
                    "id": int(parts[2]),
                    "received": parts[3] == "1",
                    "generation": claim["generation"],
                }
            )
            await message.answer(
                "Подтвердите результат после проверки чата. При подтверждении отсутствия доставки товар вернётся в бонусный склад.",
                reply_markup=buttons(
                    [("Подтверждаю результат сверки", "s:bonus-confirm:" + token), ("Отмена", "s:loyalty")]
                ),
            )
        elif action == "bonus-confirm":
            review = (await state.get_data()).get("bonus_review") or {}
            if review.get("token") != parts[2]:
                raise ValueError("Сверка устарела")
            Loyalty(self.engine).settle_review(
                review["id"], review["received"], generation=review["generation"]
            )
            await state.clear()
            await self.loyalty_menu(message)
        elif action == "margin":
            row = self.db.one("SELECT * FROM bindings WHERE rowid=?", (int(parts[2]),))
            if row:
                await self.seller_prompt(
                    message,
                    state,
                    "margin",
                    "Порог ожидаемой маржи в рублях. '-' — выключить уведомления. "
                    "Расходы на продвижение учитываются отдельно. Цены и закупки не изменяются.",
                    margin_item=row["item_id"],
                )
        elif action == "diagnostics":
            directory = self.updates.directory or self.data_dir
            await message.answer_document(
                BufferedInputFile(diagnostics(self.db, directory), filename="PlayerokHelper-diagnostics.zip")
            )
        elif action == "update-status":
            await message.answer(
                update_status(self.updates.directory)
                if self.updates.directory
                else "Для этой установки статус недоступен."
            )
        elif action == "bindtype":
            from .telegram_ui import Form

            await state.set_state(Form.binding)
            await self.binding_input(message.model_copy(update={"text": parts[2]}), state)
        else:
            await message.answer("Раздел недоступен.")
        return True

    async def seller_value(self, message, state, key, text, context):
        try:
            if key == "s:search":
                cat = context.get("catalog", {"module": "", "status": "all", "role": "lot"})
                cat.update(search="" if text == "-" else text[:150], page=0)
                await state.set_state(None)
                await state.update_data(catalog=cat)
                return await self.catalog(message, state)
            if key == "s:quick":
                replies = {}
                for line in [] if text == "-" else text.splitlines():
                    name, reply = line.split("|", 1)
                    if not 1 <= len(name.strip()) <= 40 or not 1 <= len(reply.strip()) <= 2000:
                        raise ValueError("Название до 40, текст до 2000 символов")
                    replies[name.strip()] = reply.strip()
                if len(replies) > 30:
                    raise ValueError("До 30 заготовок")
                self.db.set("quick_replies", replies)
            elif key == "s:bulk":
                field = context["bulk_field"]
                if field == "instruction":
                    if len(text) > 1000:
                        raise ValueError("Инструкция до 1000 символов")
                    value = "" if text == "-" else text
                elif field == "max_cost":
                    value = rub(text.replace(",", "."))
                    if value < 0:
                        raise ValueError("Лимит неотрицательный")
                else:
                    value = []
                    for line in [] if text == "-" else text.splitlines():
                        phrase, reply, priority = line.split("|")
                        if not phrase.strip() or not 1 <= len(reply.strip()) <= 2000:
                            raise ValueError("Проверьте правило")
                        value.append(
                            {"phrase": phrase.strip(), "reply": reply.strip(), "priority": int(priority)}
                        )
                    if len(value) > 100:
                        raise ValueError("До 100 правил")
                return await self.bulk_preview(message, state, field, value)
            elif key == "s:reset":
                if not 1 <= len(text) <= 2500:
                    raise ValueError("Реквизиты до 2500 символов")
                token = uuid.uuid4().hex[:12]
                await state.set_state(None)
                account = self.db.one(
                    "SELECT ciphertext,order_id,state FROM own_accounts WHERE id=?", (context["account_id"],)
                )
                if not account:
                    raise ValueError("Аккаунт не найден")
                await state.update_data(
                    reset_token=token, reset_ciphertext=self.db.encrypt(text), reset_snapshot=account
                )
                return await message.answer(
                    "Подтвердите: доступ изменён, предыдущая аренда отключена, аккаунт готов к новой выдаче.",
                    reply_markup=buttons(
                        [("Подтверждаю готовность", "s:reset-confirm:" + token), ("Отмена", "menu:home")]
                    ),
                )
            elif key == "s:loyalty":
                Loyalty(self.engine).configure(int(text))
            elif key == "s:bonus_instruction":
                if len(text) > 1000:
                    raise ValueError("Инструкция до 1000 символов")
                self.db.set("bonus_instruction", "" if text == "-" else text)
            elif key == "s:margin":
                value = None if text == "-" else rub(text.replace(",", "."))
                if value is not None and value < 0:
                    raise ValueError("Неотрицательный порог")
                self.db.execute(
                    "UPDATE bindings SET margin_min=? WHERE item_id=?", (value, context["margin_item"])
                )
            else:
                raise ValueError("Неизвестное поле")
            self.db.audit("seller_setting_changed", key)
            await state.clear()
            await message.answer("Сохранено.")
        except (ValueError, KeyError, ArithmeticError):
            await message.answer("Проверьте формат и ограничения.")
