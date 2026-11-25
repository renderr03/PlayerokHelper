"""Telegram setup and reconciliation for Stars and SMM orders."""

from __future__ import annotations

import asyncio
import re
import uuid

from .external import STATE_NAMES, encoded
from .models import ServiceError, money, now, rub
from .providers import PRESETS, amount, currency
from .seller_ui import buttons


class ExternalUI:
    async def external_prompt(self, message, state, key, prompt, **context):
        from .telegram_ui import Form

        await state.set_state(Form.value)
        await state.update_data(edit="x:" + key, **context)
        await message.answer(prompt)

    async def external_home(self, message, mode):
        kind = "split" if mode == "stars" else "smm"
        rows = self.db.all("SELECT * FROM providers ORDER BY id")
        keys = [
            (r["title"] + (" · включён" if r["enabled"] else " · выключен"), f"x:provider:{r['id']}")
            for r in rows
            if PRESETS[r["preset"]][0] == kind
        ]
        keys += [
            ("Подключить поставщика", "x:help:" + mode),
            ("Лоты", "x:catalog:" + mode),
            ("Привязать лот", "x:new:" + mode),
            ("Заказы и сверка", "x:orders:" + mode + ":0"),
            ("‹ Плагины", "menu:plugins"),
        ]
        await message.answer(
            ("Telegram Stars" if mode == "stars" else "SMM-услуги")
            + "\nПолучатель подтверждается покупателем. Закупка начинается только после проверки оплаты и лимитов.",
            reply_markup=buttons(keys),
        )

    async def provider_card(self, message, pid):
        provider = self.engine.external.provider(pid)
        settings = self.engine.external.settings(provider)
        balances = self.db.get("provider_balance:" + str(pid), {})
        keys = [
            ("Проверить подключение", f"x:check:{pid}"),
            ("Остановить" if provider["enabled"] else "Запустить", f"x:toggle:{pid}"),
            (
                "Подтверждение получателя: " + ("вкл" if settings["confirm"] else "выкл"),
                f"x:flag:{pid}:confirm",
            ),
            ("Автовозврат: " + ("вкл" if settings["auto_refund"] else "выкл"), f"x:flag:{pid}:auto_refund"),
            ("Уведомления: " + ("вкл" if settings["notify"] else "выкл"), f"x:flag:{pid}:notify"),
            ("Курс для учёта в рублях", f"x:rate:{pid}"),
            ("Порог низкого баланса", f"x:low:{pid}"),
        ]
        if provider["preset"] != "split":
            keys.append(("Каталог услуг", f"x:services:{pid}:0"))
        mode = "stars" if provider["preset"] == "split" else "smm"
        keys.append(("‹ Назад", "x:home:" + mode))
        rates = ", ".join(
            f"{k}: {v['value']} ₽ (до {self.engine.expiry(v['at'] + 86400)})"
            for k, v in settings["rates"].items()
        )
        await message.answer(
            provider["title"]
            + "\n"
            + ("Включён" if provider["enabled"] else "Выключен")
            + (
                "\nНовые закупки заблокированы: проверьте подключение и заказы на сверке."
                if provider["enabled"] and not self.engine.external.enabled(provider)
                else ""
            )
            + "\nБаланс: "
            + (", ".join(f"{v} {k}" for k, v in balances.items()) or "не проверен")
            + "\nПроверено: "
            + self.engine.expiry(provider["checked_at"])
            + "\nКурс учёта: "
            + (rates or "не задан")
            + "\nАвтовозврат действует только при подтверждённой отмене SMM без выполнения и без затрат. Частичные и неопределённые заказы требуют сверки.",
            reply_markup=buttons(keys),
        )

    async def external_lot(self, message, item_id):
        binding = self.engine.external.binding(item_id)
        provider = self.engine.external.provider(binding["provider_id"])
        rid = self.db.one("SELECT rowid rid FROM bindings WHERE item_id=?", (item_id,))["rid"]
        keys = [
            ("Снять паузу" if binding["paused"] else "Пауза", "lotflag:paused:" + item_id),
            ("Проверить сценарий", "preview:lot:" + item_id),
        ]
        for field, label in [
            ("quantity", "Количество"),
            ("max_native", "Лимит в валюте поставщика"),
            ("max_cost", "Лимит в рублях"),
            ("instruction", "Инструкция"),
        ]:
            keys.append((label, f"x:lotedit:{rid}:{field}"))
        if binding["fulfillment"] == "smm":
            keys += [
                ("Разрешённые сайты", f"x:lotedit:{rid}:domains"),
                ("ID услуги", f"x:lotedit:{rid}:service_id"),
            ]
        keys += [
            ("Подключение", f"x:provider:{provider['id']}"),
            ("Тексты этого лота", "scope:" + item_id),
            ("‹ Лоты", "x:catalog:" + binding["fulfillment"]),
        ]
        await message.answer(
            f"{binding['title']}\nID: {item_id}\nПоставщик: {provider['title']}\n"
            f"Количество: {binding['quantity']}\nУслуга: {binding['service_id'] or 'Stars'}\n"
            f"Лимит: {binding['max_native']} {binding['currency']} / {money(binding['max_cost'])}\n"
            f"Сайты: {', '.join(binding['domains']) or 'username Telegram'}\n"
            f"Пауза: {'да' if binding['paused'] else 'нет'}\n"
            f"Причина скрытия: {binding['hide_reason'] or '—'}",
            reply_markup=buttons(keys),
        )

    async def service_catalog(self, message, state, pid, page=0):
        context = await state.get_data()
        query = context.get("service_query", "").casefold()
        catalog = self.db.get("provider_catalog:" + str(pid), [])
        rows = [r for r in catalog if query in (r["name"] + " " + r["id"]).casefold()]
        page = max(0, min(page, max(0, (len(rows) - 1) // 20)))
        wizard = context.get("external_wizard") or {}
        selecting = wizard.get("provider_id") == pid and wizard.get("mode") == "smm"
        keys = (
            [
                (r["id"] + " · " + r["name"][:35], f"x:service:{pid}:{r['id']}")
                for r in rows[page * 20 : (page + 1) * 20]
            ]
            if selecting
            else []
        )
        lines = [
            f"Каталог {len(rows)} услуг · страница {page + 1}",
            "Поддерживаются услуги типа Default; цена указана за 1000 единиц.",
        ]
        if not selecting:
            lines += [
                f"{r['id']} · {r['name'][:60]} · {r['rate']} / 1000 · {r['min']}–{r['max']}"
                for r in rows[page * 20 : (page + 1) * 20]
            ]
        keys.append(("Поиск", f"x:service-search:{pid}"))
        if page:
            keys.append(("←", f"x:services:{pid}:{page - 1}"))
        if (page + 1) * 20 < len(rows):
            keys.append(("→", f"x:services:{pid}:{page + 1}"))
        keys.append(("‹ Подключение", f"x:provider:{pid}"))
        await message.answer("\n".join(lines)[:3800], reply_markup=buttons(keys))

    async def external_orders(self, message, mode, page):
        rows = self.db.all(
            "SELECT o.rowid rid,o.id,o.title,p.state FROM orders o JOIN provider_orders p ON p.order_id=o.id "
            "WHERE o.module=? ORDER BY p.created_at DESC LIMIT 20 OFFSET ?",
            (mode, max(0, page) * 20),
        )
        keys = [
            (r["title"][:25] + " · " + STATE_NAMES.get(r["state"], r["state"]), f"x:order:{r['rid']}")
            for r in rows
        ]
        if page:
            keys.append(("←", f"x:orders:{mode}:{page - 1}"))
        if len(rows) == 20:
            keys.append(("→", f"x:orders:{mode}:{page + 1}"))
        keys.append(("‹ Назад", "x:home:" + mode))
        await message.answer("Внешние заказы и сверка", reply_markup=buttons(keys))

    async def external_order(self, message, rid):
        row = self.db.one(
            "SELECT p.*,o.rowid rid,o.module FROM provider_orders p JOIN orders o ON o.id=p.order_id WHERE o.rowid=?",
            (rid,),
        )
        if not row:
            return
        keys = []
        if row["state"] == "review" and not row["remote_id"]:
            keys.append(("Привязать найденный заказ", f"x:attach:{rid}"))
        if row["remote_id"] and row["state"] in {"review", "partial"}:
            keys.append(("Повторить проверку статуса", f"x:rescan:{rid}"))
        if row["state"] in {"review", "partial"} and row["rub_rate"]:
            keys.append(("Сверить затраты и закрыть", f"x:settle:{rid}"))
        provider = self.engine.external.provider(row["provider_id"])
        if provider["preset"] != "split" and row["remote_id"]:
            if row["state"] == "processing":
                keys.append(("Запросить отмену у поставщика", f"x:action:{rid}:cancel"))
            if row["state"] == "completed":
                keys.append(("Запросить восстановление", f"x:action:{rid}:refill"))
        keys += [
            ("Карточка Playerok", "order:" + row["order_id"]),
            ("‹ Заказы", f"x:orders:{row['module']}:0"),
        ]
        target = self.db.decrypt(row["destination"]) if row["destination"] else "ожидается"
        await message.answer(
            f"Заказ {row['order_id']}\n{provider['title']} · {STATE_NAMES.get(row['state'], row['state'])}\n"
            f"Получатель: {target}\nНомер поставщика: {row['remote_id'] or 'не получен'}\n"
            f"{'Затраты' if row['cost_known'] else 'Предварительная оценка'}: {row['native_amount'] or '—'} {row['currency'] or ''} / {money(row['cost_rub'])}\n"
            f"Сверенный возврат поставщика: {row['supplier_refund']} {row['currency'] or ''}\n"
            f"Причина проверки: {row['error'] or '—'}\n"
            f"Осталось выполнить: {row['remains'] if row['remains'] is not None else 'поставщик не сообщает'}\n"
            "Возврат поставщика и возврат покупателю на Playerok учитываются отдельно.",
            reply_markup=buttons(keys),
        )

    async def external_callback(self, message, data, state):
        if not data.startswith("x:"):
            return False
        parts = data.split(":")
        action = parts[1]
        if action == "home":
            await state.clear()
            await self.external_home(message, parts[2])
        elif action == "help":
            mode = parts[2]
            await self.connection_callback(
                message, "connect:key:split" if mode == "stars" else "connect:smm", state
            )
        elif action == "provider":
            await self.provider_card(message, int(parts[2]))
        elif action == "check":
            try:
                balances, catalog = await self.engine.external.check(int(parts[2]))
                await message.answer(
                    "Чтение API проверено. Баланс: "
                    + ", ".join(f"{v} {k}" for k, v in balances.items())
                    + (f". Услуг Default: {len(catalog)}." if catalog else ".")
                )
            except (ServiceError, KeyError, TypeError, ValueError, ArithmeticError):
                await message.answer(
                    "Не удалось проверить баланс или каталог. Проверьте ключ, активность аккаунта и совместимость API."
                )
            await self.provider_card(message, int(parts[2]))
        elif action == "toggle":
            pid = int(parts[2])
            provider = self.engine.external.provider(pid)
            if provider["enabled"]:
                self.db.execute("UPDATE providers SET enabled=0 WHERE id=?", (pid,))
                await self.provider_card(message, pid)
            else:
                if (
                    provider["checked_revision"] != provider["revision"]
                    or now() - (provider["checked_at"] or 0) > 300
                ):
                    raise ValueError("Сначала нажмите «Проверить подключение»")
                token = uuid.uuid4().hex[:12]
                await state.update_data(
                    external_confirm={
                        "token": token,
                        "kind": "launch",
                        "provider": pid,
                        "revision": provider["revision"],
                    }
                )
                await message.answer(
                    "Включить реальные закупки с баланса поставщика? Проверьте лимиты лотов. "
                    "Для SMM поддерживается формат API v2 с услугами Default. Проверка подключения читает баланс и каталог, но не выполняет тестовую покупку. "
                    "При неопределённом результате закупка останавливается до сверки.",
                    reply_markup=buttons(
                        [("Включить", "x:confirm:" + token), ("Отмена", f"x:provider:{pid}")]
                    ),
                )
        elif action == "flag":
            pid = int(parts[2])
            key = parts[3]
            if key not in {"confirm", "auto_refund", "notify"}:
                raise ValueError("Настройка недоступна")
            provider = self.engine.external.provider(pid)
            settings = self.engine.external.settings(provider)
            settings[key] = not settings[key]
            self.db.execute("UPDATE providers SET settings=? WHERE id=?", (encoded(settings), pid))
            await self.provider_card(message, pid)
        elif action in {"rate", "low"}:
            await self.external_prompt(
                message,
                state,
                action,
                "Введите валюту и значение через пробел, например TON 250 или USD 90. "
                + (
                    "Курс: рублей за единицу, действует 24 часа."
                    if action == "rate"
                    else "Порог предупреждения в валюте баланса."
                ),
                provider_id=int(parts[2]),
            )
        elif action == "catalog":
            await self.catalog(message, state, module=parts[2], role="lot", page=0)
        elif action == "new":
            await state.clear()
            await self.external_prompt(
                message,
                state,
                "new_id",
                "Пришлите UUID готового лота Playerok с ручной выдачей.",
                external_wizard={"mode": parts[2]},
            )
        elif action == "choose":
            pid = int(parts[2])
            provider = self.engine.external.provider(pid)
            context = await state.get_data()
            wizard = context.get("external_wizard") or {}
            if wizard.get("mode") not in {"stars", "smm"} or (provider["preset"] == "split") != (
                wizard["mode"] == "stars"
            ):
                raise ValueError("Откройте создание лота заново")
            wizard["provider_id"] = pid
            await state.update_data(external_wizard=wizard, service_query="")
            if wizard["mode"] == "smm":
                await self.service_catalog(message, state, pid)
            else:
                await self.external_prompt(
                    message, state, "new_quantity", "Количество Stars в одной покупке."
                )
        elif action == "services":
            await self.service_catalog(message, state, int(parts[2]), int(parts[3]))
        elif action == "service-search":
            await self.external_prompt(
                message,
                state,
                "service_search",
                "Часть названия или ID; '-' — сбросить.",
                provider_id=int(parts[2]),
            )
        elif action == "service":
            context = await state.get_data()
            wizard = context.get("external_wizard") or {}
            if wizard.get("provider_id") != int(parts[2]):
                raise ValueError("Откройте выбор услуги заново")
            if not any(s["id"] == parts[3] for s in self.db.get("provider_catalog:" + parts[2], [])):
                raise ValueError("Услуга не найдена")
            wizard["service_id"] = parts[3]
            await state.update_data(external_wizard=wizard)
            await self.external_prompt(message, state, "new_quantity", "Количество единиц в одном заказе.")
        elif action == "method":
            context = await state.get_data()
            wizard = context.get("external_wizard") or {}
            if wizard.get("mode") != "stars" or parts[2] not in {"balance", "usdt_balance"}:
                raise ValueError("Начните привязку заново")
            wizard.update(payment_method=parts[2], currency="TON" if parts[2] == "balance" else "USD")
            await state.update_data(external_wizard=wizard)
            await self.external_prompt(
                message, state, "new_native", "Предельная стоимость закупки в " + wizard["currency"] + "."
            )
        elif action == "save":
            context = await state.get_data()
            wizard = context.get("external_wizard") or {}
            if wizard.get("token") != parts[2]:
                raise ValueError("Предпросмотр устарел")
            with self.db.transaction():
                self.db.execute(
                    "INSERT INTO bindings(item_id,title,module,fulfillment,max_cost,instruction,paused) VALUES (?,?,'stock',?,?,?,1)",
                    (
                        wizard["item_id"],
                        wizard["title"],
                        wizard["mode"],
                        wizard["max_cost"],
                        wizard["instruction"],
                    ),
                )
                self.db.execute(
                    "INSERT INTO external_bindings VALUES (?,?,?,?,?,?,?,?)",
                    (
                        wizard["item_id"],
                        wizard["provider_id"],
                        wizard.get("service_id", ""),
                        wizard["quantity"],
                        wizard.get("payment_method", "balance"),
                        encoded(wizard.get("domains", [])),
                        wizard["max_native"],
                        wizard["currency"],
                    ),
                )
            await state.clear()
            await message.answer("Лот сохранён на паузе. Проверьте сценарий, затем снимите паузу в карточке.")
            await self.external_lot(message, wizard["item_id"])
        elif action == "lotedit":
            row = self.db.one("SELECT item_id FROM bindings WHERE rowid=?", (int(parts[2]),))
            if not row:
                raise ValueError("Лот не найден")
            field = parts[3]
            prompts = {
                "quantity": "Количество в одном заказе.",
                "max_native": "Лимит закупки в валюте поставщика.",
                "max_cost": "Лимит закупки, руб.",
                "instruction": "Инструкция до 1000 символов; '-' — убрать.",
                "domains": "Разрешённые домены через запятую, без https:// и путей.",
                "service_id": "ID услуги Default из проверенного каталога.",
            }
            await self.external_prompt(
                message, state, "lot", prompts[field], external_item=row["item_id"], external_field=field
            )
        elif action == "orders":
            await self.external_orders(message, parts[2], int(parts[3]))
        elif action == "order":
            await self.external_order(message, int(parts[2]))
        elif action in {"attach", "settle"}:
            row = self.db.one(
                "SELECT p.* FROM provider_orders p JOIN orders o ON o.id=p.order_id WHERE o.rowid=?",
                (int(parts[2]),),
            )
            if not row:
                raise ValueError("Заказ не найден")
            prompt = (
                "Укажите найденный номер заказа в кабинете поставщика. Проверьте совпадение получателя, услуги и количества. Новая закупка не создаётся."
                if action == "attach"
                else "После сверки кабинета укажите чистые затраты и подтверждённый возврат поставщика через | в валюте "
                + str(row["currency"])
                + ". Например: 2.50 | 1.00. Чистые затраты уже учитывают возврат; сумма возврата сохраняется отдельно для справки. Это не возврат покупателю."
            )
            await self.external_prompt(message, state, action, prompt, external_order=row["order_id"])
        elif action == "rescan":
            row = self.db.one(
                "SELECT p.* FROM provider_orders p JOIN orders o ON o.id=p.order_id WHERE o.rowid=?",
                (int(parts[2]),),
            )
            if row and row["remote_id"] and row["state"] in {"partial", "review"}:
                async with self.engine._locks.setdefault(row["order_id"], asyncio.Lock()):
                    self.db.execute(
                        "UPDATE provider_orders SET state='processing',next_poll=0 WHERE order_id=? AND state IN ('partial','review')",
                        (row["order_id"],),
                    )
                await message.answer(
                    "Запрошена повторная сверка существующего заказа. Новая закупка не создаётся."
                )
        elif action == "action":
            row = self.db.one(
                "SELECT p.* FROM provider_orders p JOIN orders o ON o.id=p.order_id WHERE o.rowid=?",
                (int(parts[2]),),
            )
            if not row or parts[3] not in {"cancel", "refill"}:
                raise ValueError("Запрос недоступен")
            token = uuid.uuid4().hex[:12]
            await state.update_data(
                external_confirm={
                    "token": token,
                    "kind": "action",
                    "order": row["order_id"],
                    "action": parts[3],
                    "expected": row["updated_at"],
                }
            )
            await message.answer(
                "Отправить поставщику запрос "
                + ("отмены" if parts[3] == "cancel" else "восстановления")
                + "? "
                "Доступность зависит от услуги. Принятие запроса не подтверждает возврат или выполнение. Повтор после неизвестного ответа заблокирован.",
                reply_markup=buttons([("Отправить запрос", "x:confirm:" + token)]),
            )
        elif action == "confirm":
            context = await state.get_data()
            confirmation = context.get("external_confirm") or {}
            if confirmation.get("token") != parts[2]:
                raise ValueError("Подтверждение устарело")
            await state.update_data(external_confirm=None)
            if confirmation["kind"] == "launch":
                provider = self.engine.external.provider(confirmation["provider"])
                if (
                    provider["revision"] != confirmation["revision"]
                    or provider["checked_revision"] != provider["revision"]
                    or now() - (provider["checked_at"] or 0) > 300
                ):
                    raise ValueError("Повторите проверку подключения")
                self.db.execute("UPDATE providers SET enabled=1 WHERE id=?", (provider["id"],))
                self.db.audit("provider_enabled", str(provider["id"]))
                await self.provider_card(message, provider["id"])
            else:
                oid = confirmation["order"]
                async with self.engine._locks.setdefault(oid, asyncio.Lock()):
                    if confirmation["kind"] == "attach":
                        self.engine.external.attach(oid, confirmation["remote"], confirmation["expected"])
                    elif confirmation["kind"] == "settle":
                        self.engine.external.settle(
                            oid, confirmation["net"], confirmation["refunded"], confirmation["expected"]
                        )
                    else:
                        row = self.db.one("SELECT updated_at FROM provider_orders WHERE order_id=?", (oid,))
                        if not row or row["updated_at"] != confirmation["expected"]:
                            raise ValueError("Состояние изменилось, откройте действие заново")
                        await self.engine.external.action(oid, confirmation["action"])
                await message.answer(
                    "Результат сохранён. Для закупки или повторной отправки запрос не создавался."
                    if confirmation["kind"] != "action"
                    else "Запрос поставщику отправлен. Проверьте результат в его кабинете."
                )
        return True

    def external_domains(self, text):
        rows = list(dict.fromkeys(s.strip().lower() for s in text.split(",") if s.strip()))
        if not 1 <= len(rows) <= 20 or any(
            not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}", r) for r in rows
        ):
            raise ValueError("Нужны домены через запятую, например t.me, vk.com; до 20 доменов")
        return rows

    async def external_value(self, message, state, key, text, context):
        key = key[2:]
        try:
            if key in {"rate", "low"}:
                unit, value = text.split()
                unit = currency(unit)
                value = amount(value.replace(",", "."))
                if key == "rate" and (unit == "RUB" or value <= 0):
                    raise ValueError("Нужен положительный курс иностранной валюты")
                pid = context["provider_id"]
                provider = self.engine.external.provider(pid)
                settings = self.engine.external.settings(provider)
                if key == "rate":
                    settings["rates"][unit] = {"value": str(value), "at": now()}
                else:
                    settings["low"][unit] = str(value)
                self.db.execute("UPDATE providers SET settings=? WHERE id=?", (encoded(settings), pid))
                if key == "rate":
                    for row in self.db.all(
                        "SELECT item_id FROM external_bindings WHERE provider_id=?", (pid,)
                    ):
                        self.db.set("external_margin:" + row["item_id"], {})
                await state.clear()
                return await self.provider_card(message, pid)
            if key == "service_search":
                await state.set_state(None)
                await state.update_data(service_query="" if text == "-" else text[:100])
                return await self.service_catalog(message, state, context["provider_id"])
            if key == "lot":
                item = context["external_item"]
                field = context["external_field"]
                binding = self.engine.external.binding(item)
                if field == "quantity":
                    value = int(text)
                    if not 1 <= value <= 1000000:
                        raise ValueError("Количество от 1 до 1000000")
                elif field == "max_native":
                    value = str(amount(text.replace(",", ".")))
                    if amount(value) <= 0:
                        raise ValueError("Лимит должен быть положительным")
                elif field == "max_cost":
                    value = rub(text.replace(",", "."))
                    if not 0 < value <= 10**11:
                        raise ValueError("Некорректный лимит в рублях")
                elif field == "domains":
                    value = encoded(self.external_domains(text))
                elif field == "instruction":
                    if len(text) > 1000:
                        raise ValueError("Инструкция до 1000 символов")
                    value = "" if text == "-" else text
                elif field == "service_id":
                    if not any(
                        s["id"] == text
                        for s in self.db.get("provider_catalog:" + str(binding["provider_id"]), [])
                    ):
                        raise ValueError("Услуга не найдена в каталоге")
                    value = text
                else:
                    raise ValueError("Настройка недоступна")
                table = "bindings" if field in {"max_cost", "instruction"} else "external_bindings"
                self.db.execute(f"UPDATE {table} SET {field}=? WHERE item_id=?", (value, item))
                self.db.audit("external_lot_changed", item)
                self.db.set("external_margin:" + item, {})
                await state.clear()
                return await self.external_lot(message, item)
            if key in {"attach", "settle"}:
                row = self.db.one(
                    "SELECT * FROM provider_orders WHERE order_id=?", (context["external_order"],)
                )
                if not row:
                    raise ValueError("Заказ не найден")
                token = uuid.uuid4().hex[:12]
                confirmation = {
                    "token": token,
                    "kind": key,
                    "order": row["order_id"],
                    "expected": row["updated_at"],
                }
                if key == "attach":
                    provider = self.engine.external.provider(row["provider_id"])
                    remote = str(uuid.UUID(text)) if provider["preset"] == "split" else str(int(text))
                    confirmation["remote"] = remote
                    preview = "Привязать найденный заказ " + remote + " к " + row["order_id"] + "?"
                else:
                    net, refunded = (str(amount(v.strip().replace(",", "."))) for v in text.split("|"))
                    confirmation.update(net=net, refunded=refunded)
                    preview = f"Закрыть сверку {row['order_id']}? Чистые затраты {net} {row['currency']}, подтверждённый возврат поставщика {refunded}. Возврат покупателю не выполняется."
                await state.set_state(None)
                await state.update_data(external_confirm=confirmation)
                return await message.answer(
                    preview, reply_markup=buttons([("Подтверждаю сверку", "x:confirm:" + token)])
                )
            wizard = context.get("external_wizard") or {}
            if key == "new_id":
                wizard["item_id"] = str(uuid.UUID(text))
                if self.db.one("SELECT item_id FROM bindings WHERE item_id=?", (wizard["item_id"],)):
                    raise ValueError("Лот уже привязан")
                await state.update_data(external_wizard=wizard)
                return await self.external_prompt(
                    message, state, "new_title", "Название лота для меню, до 150 символов."
                )
            if key == "new_title":
                if not 1 <= len(text) <= 150:
                    raise ValueError("Название от 1 до 150 символов")
                wizard["title"] = text
                await state.update_data(external_wizard=wizard)
                await state.set_state(None)
                rows = [
                    r
                    for r in self.db.all("SELECT * FROM providers ORDER BY id")
                    if (r["preset"] == "split") == (wizard["mode"] == "stars")
                ]
                return await message.answer(
                    "Выберите настроенного поставщика. Если список пуст, сначала подключите его в меню плагина.",
                    reply_markup=buttons([(r["title"], f"x:choose:{r['id']}") for r in rows]),
                )
            if key == "new_quantity":
                wizard["quantity"] = int(text)
                if not 1 <= wizard["quantity"] <= 1000000:
                    raise ValueError("Количество от 1 до 1000000")
                await state.update_data(external_wizard=wizard)
                if wizard["mode"] == "stars":
                    await state.set_state(None)
                    return await message.answer(
                        "С какого баланса Split оплачивать?",
                        reply_markup=buttons(
                            [("TON", "x:method:balance"), ("USDT", "x:method:usdt_balance")]
                        ),
                    )
                catalog = self.db.get("provider_catalog:" + str(wizard["provider_id"]), [])
                service = next((s for s in catalog if s["id"] == wizard["service_id"]), None)
                if not service or not service["min"] <= wizard["quantity"] <= service["max"]:
                    raise ValueError("Количество вне лимитов услуги")
                balances = self.db.get("provider_balance:" + str(wizard["provider_id"]), {})
                if len(balances) != 1:
                    raise ValueError("Сначала проверьте баланс поставщика")
                wizard["currency"] = next(iter(balances))
                await state.update_data(external_wizard=wizard)
                return await self.external_prompt(
                    message, state, "new_native", "Предел закупки в " + wizard["currency"] + "."
                )
            if key == "new_native":
                wizard["max_native"] = str(amount(text.replace(",", ".")))
                if amount(wizard["max_native"]) <= 0:
                    raise ValueError("Нужен положительный лимит")
                await state.update_data(external_wizard=wizard)
                return await self.external_prompt(
                    message,
                    state,
                    "new_rub",
                    "Предельная стоимость закупки в рублях для контроля по заданному курсу.",
                )
            if key == "new_rub":
                wizard["max_cost"] = rub(text.replace(",", "."))
                if not 0 < wizard["max_cost"] <= 10**11:
                    raise ValueError("Нужен положительный лимит")
                await state.update_data(external_wizard=wizard)
                return await self.external_prompt(
                    message,
                    state,
                    "new_domains" if wizard["mode"] == "smm" else "new_instruction",
                    "Разрешённые домены через запятую: например t.me, vk.com."
                    if wizard["mode"] == "smm"
                    else "Инструкция покупателю до 1000 символов; '-' — без инструкции.",
                )
            if key == "new_domains":
                wizard["domains"] = self.external_domains(text)
                await state.update_data(external_wizard=wizard)
                return await self.external_prompt(
                    message, state, "new_instruction", "Инструкция до 1000 символов; '-' — без инструкции."
                )
            if key == "new_instruction":
                if len(text) > 1000:
                    raise ValueError("Инструкция до 1000 символов")
                wizard["instruction"] = "" if text == "-" else text
                wizard["token"] = uuid.uuid4().hex[:12]
                await state.set_state(None)
                await state.update_data(external_wizard=wizard)
                return await message.answer(
                    f"{wizard['title']}\nЛот: {wizard['item_id']}\nКоличество: {wizard['quantity']}\n"
                    f"Лимиты: {wizard['max_native']} {wizard['currency']} / {money(wizard['max_cost'])}\n"
                    "Будет сохранён на паузе до проверки.",
                    reply_markup=buttons(
                        [("Сохранить лот", "x:save:" + wizard["token"]), ("Отменить", "ui:cancel")]
                    ),
                )
            raise ValueError("Откройте настройку заново")
        except (ValueError, KeyError, TypeError, ArithmeticError, ServiceError):
            await message.answer(
                "Значение не принято. Проверьте формат, валюту, лимиты услуги и текущую настройку."
            )
