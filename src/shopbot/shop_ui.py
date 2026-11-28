"""Button-based shop tools. Catalogs and calculators never perform purchases."""

from __future__ import annotations

import asyncio
import io
import json
from datetime import datetime, timedelta
from decimal import Decimal

from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile

from . import shop_tools as tools
from .models import ServiceError, money, now
from .navigation import buttons
from .scenarios import ACTIONS, CONDITIONS, PERIODS, choose


class ShopForm(StatesGroup):
    value = State()
    upload = State()


BACK = [("‹ Инструменты магазина", "v6:home"), ("✖ Отменить", "ui:cancel")]


class ShopUI:
    async def shop_prompt(self, message, state, key, text, choices=None, **values):
        await state.set_state(ShopForm.value)
        await state.update_data(shop_key=key, **values)
        await message.answer(text, reply_markup=buttons((choices or []) + [("‹ Назад", "ui:back"), *BACK]))

    def shop_binding(self, rid):
        row = self.db.one("SELECT rowid rid,* FROM bindings WHERE rowid=?", (int(rid),))
        if not row:
            raise ValueError("Лот не найден")
        return row

    async def shop_lots(self, message, state, page=0, refresh=False):
        from .telegram_ui import Form

        context = await state.get_data()
        rows = context.get("market_catalog")
        if refresh or rows is None:
            await message.answer("⏳ Загружаю ваши товары…")
            rows = []
            try:
                proxy = self.managed("market")
                async with proxy.request():
                    market = proxy.current
                    async with asyncio.timeout(20):
                        async for item in market.own_items():
                            if (item.get("user") or {}).get("id") == market.user_id:
                                rows.append(
                                    {
                                        "id": item["id"],
                                        "name": str(item.get("name") or "Товар")[:150],
                                        "status": item.get("status"),
                                    }
                                )
                            if len(rows) >= 2000:
                                break
            except (ServiceError, TimeoutError, AttributeError):
                rows = []
            await state.update_data(market_catalog=rows, market_catalog_at=now())
        query = context.get("market_query", "").casefold()
        indexed = [(i, r) for i, r in enumerate(rows) if query in r["name"].casefold()]
        page = max(0, min(page, max(0, (len(indexed) - 1) // 15)))
        await state.set_state(Form.binding)
        await state.update_data(step=0, binding={}, catalog_title=None)
        keys = [(r["name"][:38], f"v6:picklot:{i}") for i, r in indexed[page * 15 : (page + 1) * 15]]
        keys += [
            ("🔎 Найти товар", "v6:lotsearch"),
            ("🔄 Загрузить заново", "v6:lots:refresh"),
            ("🔗 Ввести ссылку", "v6:lotmanual"),
        ]
        if page:
            keys.append(("←", f"v6:lots:{page - 1}"))
        if (page + 1) * 15 < len(indexed):
            keys.append(("→", f"v6:lots:{page + 1}"))
        keys += BACK
        await message.answer(
            f"🛍 Ваши товары · {len(indexed)} · страница {page + 1}\nВыберите товар. Если список недоступен, используйте обычную ссылку.",
            reply_markup=buttons(keys),
        )

    async def kosell_choices(self, message, state, page=0):
        from .telegram_ui import Form

        context = await state.get_data()
        rows = context.get("kosell_catalog")
        if rows is None or now() - context.get("kosell_catalog_at", 0) > 300:
            async with asyncio.timeout(15):
                products = await self.engine.kosell.products()
            rows = [
                {
                    k: r.get(k)
                    for k in (
                        "id",
                        "name",
                        "min_hours",
                        "max_hours",
                        "available_accounts",
                        "price_per_hour_rub",
                        "price_per_hour_usd",
                    )
                }
                for r in products
            ]
            await state.update_data(kosell_catalog=rows, kosell_catalog_at=now())
        query = context.get("kosell_query", "").casefold()
        indexed = [(i, r) for i, r in enumerate(rows) if query in str(r["name"]).casefold()]
        page = max(0, min(page, max(0, (len(indexed) - 1) // 15)))
        await state.set_state(Form.binding)
        await state.update_data(step=3)
        keys = [
            (str(r["name"])[:35] + f" · {r['available_accounts']}", f"v6:pickgame:{i}")
            for i, r in indexed[page * 15 : (page + 1) * 15]
        ]
        if page:
            keys.append(("←", f"v6:games:{page - 1}"))
        if (page + 1) * 15 < len(indexed):
            keys.append(("→", f"v6:games:{page + 1}"))
        keys += [("🔎 Найти игру", "v6:gamesearch"), ("‹ Назад", "ui:back"), *BACK]
        await message.answer(
            f"🎮 Каталог KOSell · {len(indexed)} игр\nВыберите игру для текущего лота.",
            reply_markup=buttons(keys),
        )

    async def shop_home(self, message, state):
        # Legacy tool hub buttons keep working through the canonical main menu.
        return await self.start(message, state)

    async def template_menu(self, message, state, page=0):
        rows = self.db.all("SELECT id,name FROM shop_templates ORDER BY created_at DESC")
        page = max(0, min(page, max(0, (len(rows) - 1) // 15)))
        keys = [(r["name"], "v6:template:" + r["id"]) for r in rows[page * 15 : (page + 1) * 15]]
        if page:
            keys.append(("←", f"v6:templates:{page - 1}"))
        if (page + 1) * 15 < len(rows):
            keys.append(("→", f"v6:templates:{page + 1}"))
        keys += [("➕ Сохранить настройки лота", "v6:tplsource:0"), ("📥 Импорт JSON", "v6:tplimport"), *BACK]
        await message.answer(
            "📋 Шаблоны\n\nСрок, лимиты и инструкция. Токены, реквизиты склада и платные действия не копируются.",
            reply_markup=buttons(keys),
        )

    async def binding_picker(self, message, prefix, page):
        count = self.db.one("SELECT count(*) n FROM bindings")["n"]
        page = max(0, min(int(page), max(0, (count - 1) // 15)))
        rows = self.db.all(
            "SELECT rowid rid,title FROM bindings ORDER BY rowid LIMIT 15 OFFSET ?", (page * 15,)
        )
        keys = [(r["title"][:40] or "Лот", f"v6:{prefix}:{r['rid']}") for r in rows]
        parent = "tplsource" if prefix == "tplsave" else "prices"
        if page:
            keys.append(("←", f"v6:{parent}:{page - 1}"))
        if (page + 1) * 15 < count:
            keys.append(("→", f"v6:{parent}:{page + 1}"))
        await message.answer("📦 Выберите настроенный лот", reply_markup=buttons(keys + BACK))

    async def template_targets(self, message, state, tid, page=0):
        context = await state.get_data()
        selected = context.get("tpl_selected", [])
        value = tools.get_template(self.db, tid)
        rows = self.db.all("SELECT rowid rid,* FROM bindings ORDER BY rowid")
        rows = [r for r in rows if tools.compatible(self.db, value, r["item_id"])]
        page = max(0, min(page, max(0, (len(rows) - 1) // 15)))
        keys = [
            (
                ("☑ " if r["item_id"] in selected else "☐ ") + r["title"][:35],
                f"v6:tplselect:{tid}:{r['rid']}:{page}",
            )
            for r in rows[page * 15 : (page + 1) * 15]
        ]
        if page:
            keys.append(("←", f"v6:targets:{tid}:{page - 1}"))
        if (page + 1) * 15 < len(rows):
            keys.append(("→", f"v6:targets:{tid}:{page + 1}"))
        keys += [
            (f"Предпросмотр ({len(selected)})", f"v6:tplpreview:{tid}"),
            ("Снять выбор", f"v6:tplclear:{tid}"),
            *BACK,
        ]
        await message.answer(
            f"📋 {value['name']}\nВыберите совместимые лоты: {len(rows)}. Выбрано {len(selected)}.",
            reply_markup=buttons(keys),
        )

    async def customer_card(self, message, rid):
        row = self.db.one("SELECT buyer_id,chat_id FROM orders WHERE rowid=?", (int(rid),))
        if not row:
            raise ValueError("Покупатель не найден")
        data = tools.customer(self.db, row["buyer_id"])
        lines = [
            "👤 " + data["name"],
            f"Завершено покупок: {data['completed']}",
            "Подтверждённая выручка: " + money(data["spent"]),
            "Метка: " + tools.TAGS.get(data["tag"], "нет"),
            "\nЗаметка продавца: " + (data["note"] or "—"),
            "\nПоследние заказы:",
        ]
        lines += [
            r["title"][:40]
            + " · "
            + r["market_status"]
            + (" · до " + self.engine.expiry(r["expires_at"]) if r["expires_at"] else "")
            for r in data["rows"][:8]
        ]
        trace = self.db.get("scenario_last:" + row["chat_id"])
        if trace:
            lines.append(
                "\nПоследний сценарий: #"
                + str(trace["rule"])
                + " · "
                + ACTIONS.get(trace["action"], "действие")
            )
        keys = (
            [("✏ Заметка", f"v6:note:{rid}"), ("🧪 Проверить сценарий", f"v6:ruletest:{rid}")]
            + [(label, f"v6:tag:{rid}:{tag}") for tag, label in tools.TAGS.items()]
            + [("Снять метку", f"v6:tag:{rid}:none")]
        )
        chat = self.db.one("SELECT rowid rid FROM chats WHERE id=?", (row["chat_id"],))
        if chat:
            keys.append(("💬 Переписка", "s:chat:" + str(chat["rid"])))
        await message.answer("\n".join(lines)[:3800], reply_markup=buttons(keys + BACK))

    async def calendar_view(self, message, days, page):
        local = datetime.now(self.engine.tz)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = tools.calendar(self.db, start.timestamp(), (start + timedelta(days=days)).timestamp())
        page = max(0, min(page, max(0, (len(rows) - 1) // 15)))
        counts = {
            r["state"]: r["n"]
            for r in self.db.all("SELECT state,count(*) n FROM own_accounts GROUP BY state")
        }
        lines = [
            f"📅 Окончания аренды · {days} дн. · {self.engine.tz.key}",
            f"Свой пул: готово {counts.get('free', 0)}, занято {counts.get('active', 0) + counts.get('reserved', 0)}, подготовка {counts.get('reset', 0)}",
            "Освобождение по сроку не означает готовность к выдаче.",
            "",
        ]
        lines += [
            self.engine.expiry(r["expires_at"])
            + " · "
            + r["buyer_name"][:30]
            + "\n"
            + r["title"][:65]
            + (" · срок истёк" if r["expires_at"] < now() else "")
            for r in rows[page * 15 : (page + 1) * 15]
        ]
        keys = [("Сегодня", "v6:calendar:1:0"), ("Неделя", "v6:calendar:7:0")]
        if page:
            keys.append(("←", f"v6:calendar:{days}:{page - 1}"))
        if (page + 1) * 15 < len(rows):
            keys.append(("→", f"v6:calendar:{days}:{page + 1}"))
        await message.answer("\n".join(lines) or "Аренд нет", reply_markup=buttons(keys + BACK))

    async def stats_view(self, message, days):
        data = tools.dashboard(self.engine, days)
        s, prev = data["summary"], data["previous"]
        lines = [
            f"📊 Итоги за {days} дн.",
            "Подтверждённая выручка: " + money(s["gross"]),
            "Предыдущий период: " + money(prev["gross"]),
            "Возвраты: " + money(s["refunds"]),
            "Известная себестоимость: " + money(s["cost"]),
            "Предварительная прибыль: " + money(s["profit"]),
            f"Повторных покупателей: {data['repeat']} из {data['buyers']}",
            "\nЛидеры продаж:",
        ]
        lines += [r["title"][:45] + " · " + money(r["total"]) for r in data["top"]]
        lines += [
            "\nРасходы неполные; прибыль предварительная."
            if not s["complete"]
            else "\nПо известным расходам магазина.",
            "Активные часы: "
            + (
                ", ".join(
                    f"{h:02d}:00 ({n})"
                    for h, n in sorted(enumerate(data["hours"]), key=lambda x: -x[1])[:3]
                    if n
                )
                or "нет продаж"
            ),
        ]
        await message.answer(
            "\n".join(lines),
            reply_markup=buttons(
                [
                    ("Сутки", "v6:stats:1"),
                    ("Неделя", "v6:stats:7"),
                    ("Месяц", "v6:stats:30"),
                    ("📉 График PNG", f"v6:chart:{days}"),
                    *BACK,
                ]
            ),
        )

    async def shop_callback(self, message, data, state):
        if not data.startswith("v6:"):
            return False
        p = data.split(":")
        action = p[1]
        context = await state.get_data()
        if action not in {"tplapply", "rulesave"}:
            await state.update_data(shop_confirm=None)
        try:
            if action == "home":
                await self.shop_home(message, state)
            elif action == "lots":
                await self.shop_lots(message, state, int(p[2]) if p[2].isdigit() else 0, p[2] == "refresh")
            elif action == "lotmanual":
                from .telegram_ui import Form

                await state.set_state(Form.binding)
                await state.update_data(step=0, binding={}, catalog_title=None)
                await message.answer("🔗 Пришлите ссылку на свой товар Playerok.", reply_markup=buttons(BACK))
            elif action in {"lotsearch", "gamesearch"}:
                await self.shop_prompt(message, state, action, "🔎 Что найти? Введите часть названия.")
            elif action == "picklot":
                rows = context.get("market_catalog", [])
                if now() - context.get("market_catalog_at", 0) > 300:
                    raise ValueError("Список устарел. Загрузите товары заново.")
                i = int(p[2])
                if not 0 <= i < len(rows):
                    raise ValueError("Выбор устарел")
                from .telegram_ui import Form

                await state.set_state(Form.binding)
                await state.update_data(step=0, binding={}, catalog_title=rows[i]["name"])
                await self.binding_input(
                    message.model_copy(update={"text": rows[i]["id"]}).as_(self.bot), state
                )
            elif action == "games":
                await self.kosell_choices(message, state, int(p[2]))
            elif action == "pickgame":
                rows = context.get("kosell_catalog", [])
                if now() - context.get("kosell_catalog_at", 0) > 300 or context.get("step") != 3:
                    raise ValueError("Выбор устарел")
                i = int(p[2])
                if not 0 <= i < len(rows):
                    raise ValueError("Выбор устарел")
                await state.update_data(product_limits=rows[i])
                from .telegram_ui import Form

                await state.set_state(Form.binding)
                await self.binding_input(
                    message.model_copy(update={"text": str(rows[i]["id"])}).as_(self.bot), state
                )
            elif action == "hours":
                if context.get("step") != 4 or not 1 <= int(p[2]) <= 8760:
                    raise ValueError("Выбор устарел")
                from .telegram_ui import Form

                await state.set_state(Form.binding)
                await self.binding_input(message.model_copy(update={"text": p[2]}).as_(self.bot), state)
            elif action == "templates":
                await self.template_menu(message, state, int(p[2]) if len(p) > 2 else 0)
            elif action in {"tplsource", "prices"}:
                await self.binding_picker(message, "tplsave" if action == "tplsource" else "price", int(p[2]))
            elif action == "tplsave":
                row = self.shop_binding(p[2])
                await self.shop_prompt(
                    message,
                    state,
                    "tplname",
                    "Название шаблона (до 80 символов).",
                    template_source=row["item_id"],
                )
            elif action == "tplimport":
                await state.set_state(ShopForm.upload)
                await message.answer(
                    "📥 Пришлите JSON-файл шаблона до 128 КБ или вставьте JSON текстом. Экспорт содержит только настройки; проверьте свободный текст инструкции перед передачей другим людям.",
                    reply_markup=buttons(BACK),
                )
            elif action == "template":
                value = tools.get_template(self.db, p[2])
                await message.answer(
                    f"📋 {value['name']}\nТип: {value['mode']}\nСрок: {value['hours']} ч.\nЛимит: {money(value['max_cost'])}\nИнструкция: {value['instruction'][:800]}",
                    reply_markup=buttons(
                        [
                            ("Применить к лотам", "v6:targets:" + p[2] + ":0"),
                            ("📤 Экспорт JSON", "v6:tplexport:" + p[2]),
                            ("🗑 Удалить шаблон", "v6:tpldelete:" + p[2]),
                            *BACK,
                        ]
                    ),
                )
            elif action == "tpldelete":
                self.db.execute("DELETE FROM shop_templates WHERE id=?", (p[2],))
                await self.template_menu(message, state)
            elif action == "tplexport":
                value = tools.get_template(self.db, p[2])
                await message.answer_document(
                    BufferedInputFile(
                        json.dumps(value, ensure_ascii=False, indent=2).encode(),
                        filename="shop-template.json",
                    ),
                    caption="📋 Настройки шаблона без ключей и складских реквизитов.",
                    reply_markup=buttons(BACK),
                )
            elif action in {"targets", "tplclear", "tplselect"}:
                tid = p[2]
                if action == "tplclear":
                    await state.update_data(tpl_selected=[])
                if action == "tplselect":
                    row = self.shop_binding(p[3])
                    selected = context.get("tpl_selected", [])
                    if row["item_id"] in selected:
                        selected.remove(row["item_id"])
                    else:
                        selected.append(row["item_id"])
                    if len(selected) > 200:
                        raise ValueError("Не более 200 лотов")
                    await state.update_data(tpl_selected=selected)
                await self.template_targets(
                    message,
                    state,
                    tid,
                    int(p[4]) if action == "tplselect" else int(p[3]) if action == "targets" else 0,
                )
            elif action == "tplpreview":
                preview = tools.preview_template(self.db, p[2], context.get("tpl_selected", []))
                await state.update_data(template_preview=preview, shop_confirm=preview["nonce"])
                lines = [
                    "📋 Подтвердите изменения",
                    f"Лотов: {len(preview['items'])}",
                    f"Срок: {preview['value']['hours']} ч. · лимит {money(preview['value']['max_cost'])}",
                    "Инструкция будет заменена. Пауза, поставщик и платное поднятие не меняются.",
                ]
                for item in list(preview["items"])[:15]:
                    row = self.db.one("SELECT hours,max_cost,title FROM bindings WHERE item_id=?", (item,))
                    lines.append(
                        row["title"][:35]
                        + f" · {row['hours']} → {preview['value']['hours']} ч.; "
                        + money(row["max_cost"])
                        + " → "
                        + money(preview["value"]["max_cost"])
                    )
                await message.answer(
                    "\n".join(lines)[:3800],
                    reply_markup=buttons([("✅ Применить", "v6:tplapply:" + preview["nonce"]), *BACK]),
                )
            elif action == "tplapply":
                if context.get("shop_confirm") != p[2]:
                    raise ValueError("Подтверждение устарело")
                await state.update_data(shop_confirm=None, template_preview=None)
                result = tools.apply_template(self.db, context["template_preview"])
                await message.answer_document(
                    BufferedInputFile(
                        "\n".join(i + ": " + r for i, r in result).encode(), filename="template-result.txt"
                    ),
                    caption=f"📋 Обработано лотов: {len(result)}",
                    reply_markup=buttons(BACK),
                )
            elif action == "customers":
                page = max(0, int(p[2]))
                tag = context.get("customer_filter", "")
                where = " AND coalesce(n.tag,'')=?" if tag else ""
                rows = self.db.all(
                    "SELECT min(o.rowid) rid,o.buyer_name,o.buyer_id,max(o.created_at) recent FROM orders o LEFT JOIN customer_notes n ON n.buyer_id=o.buyer_id WHERE 1=1"
                    + where
                    + " GROUP BY o.buyer_id ORDER BY recent DESC LIMIT 16 OFFSET ?",
                    ((tag,) if tag else ()) + (page * 15,),
                )
                keys = [(r["buyer_name"][:40], f"v6:customer:{r['rid']}") for r in rows[:15]]
                keys += [("Все", "v6:customerfilter:none")] + [
                    (v, "v6:customerfilter:" + k) for k, v in tools.TAGS.items()
                ]
                if page:
                    keys.append(("←", f"v6:customers:{page - 1}"))
                if len(rows) > 15:
                    keys.append(("→", f"v6:customers:{page + 1}"))
                await message.answer(
                    "👤 Покупатели · " + tools.TAGS.get(tag, "все"), reply_markup=buttons(keys + BACK)
                )
            elif action == "customerfilter":
                if p[2] not in {"none", *tools.TAGS}:
                    raise ValueError("Нет такой метки")
                await state.update_data(customer_filter="" if p[2] == "none" else p[2])
                return await self.shop_callback(message, "v6:customers:0", state)
            elif action == "customer":
                await self.customer_card(message, p[2])
            elif action == "tag":
                row = self.db.one("SELECT buyer_id FROM orders WHERE rowid=?", (int(p[2]),))
                if not row:
                    raise ValueError("Покупатель не найден")
                tools.set_customer(self.db, row["buyer_id"], tag="" if p[3] == "none" else p[3])
                await self.customer_card(message, p[2])
            elif action == "note":
                await self.shop_prompt(
                    message,
                    state,
                    "note",
                    "Заметка только для продавца. До 2000 символов; «-» очищает.",
                    customer_rid=int(p[2]),
                )
            elif action == "calendar":
                await self.calendar_view(message, int(p[2]) if p[2] in {"1", "7"} else 7, int(p[3]))
            elif action in {"stats", "chart"}:
                days = int(p[2]) if p[2] in {"1", "7", "30"} else 7
                if action == "stats":
                    await self.stats_view(message, days)
                else:
                    blob = await asyncio.to_thread(tools.chart_png, tools.dashboard(self.engine, days))
                    await message.answer_document(
                        BufferedInputFile(blob, filename=f"sales-{days}d.png"),
                        caption="📊 Подтверждённая выручка по дням, RUB. Расходы и предварительная прибыль — в итогах.",
                        reply_markup=buttons([("‹ Итоги", f"v6:stats:{days}"), *BACK]),
                    )
            elif action == "price":
                row = self.shop_binding(p[2])
                cost = None
                source = "введённая себестоимость"
                if tools.mode(row) == "kosell":
                    async with asyncio.timeout(15):
                        quote = await self.engine.kosell.quote(row["product_id"], row["hours"])
                    cost = quote["total_rub"]
                    source = "расчёт KOSell"
                elif tools.mode(row) in {"stock", "own_rent"}:
                    table = "stock" if tools.mode(row) == "stock" else "own_accounts"
                    stock = self.db.one(
                        f"SELECT cost FROM {table} WHERE item_id=? AND state='free' ORDER BY id LIMIT 1",
                        (row["item_id"],),
                    )
                    cost = Decimal(stock["cost"]) / 100 if stock and stock["cost"] is not None else None
                    source = "себестоимость свободной позиции"
                await state.update_data(
                    price_data={
                        "cost": str(cost) if cost is not None else None,
                        "source": source,
                        "at": now(),
                    }
                )
                if cost is None:
                    await self.shop_prompt(
                        message,
                        state,
                        "price_cost",
                        "Известная себестоимость в рублях? Этот ввод будет отмечен как ручная оценка.",
                    )
                else:
                    await self.shop_prompt(
                        message,
                        state,
                        "price_margin",
                        f"💰 Себестоимость: {cost} ₽\nЖелаемая маржа от продажной цены, %?",
                        [("20%", "v6:input:20"), ("30%", "v6:input:30")],
                    )
            elif action == "input":
                if context.get("shop_key") not in {
                    "price_margin",
                    "rule_delay",
                    "rule_cooldown",
                    "rule_priority",
                }:
                    raise ValueError("Выбор устарел")
                await self.shop_input(message.model_copy(update={"text": p[2]}).as_(self.bot), state)
            elif action == "external":
                await message.answer(
                    "⭐ Stars и SMM\nПакеты и количество выбираются в форме. Получатель и цена подтверждаются перед закупкой; частичное выполнение сохраняется отдельным статусом.",
                    reply_markup=buttons(
                        [("⭐ Telegram Stars", "x:home:stars"), ("💬 SMM", "x:home:smm"), *BACK]
                    ),
                )
            elif action == "quantity":
                if (
                    context.get("edit") != "x:new_quantity"
                    or context.get("external_wizard", {}).get("mode") != "smm"
                ):
                    raise ValueError("Выбор устарел")
                quantity = int(p[2])
                if not 1 <= quantity <= 1000000:
                    raise ValueError()
                await self.external_value(message, state, "x:new_quantity", p[2], context)
            elif action == "pack":
                if (
                    context.get("edit") != "x:new_quantity"
                    or context.get("external_wizard", {}).get("mode") != "stars"
                ):
                    raise ValueError("Выбор пакета устарел")
                if p[2] not in {"50", "100", "250", "500", "1000"}:
                    raise ValueError("Неизвестный пакет")
                await self.external_value(message, state, "x:new_quantity", p[2], context)
            elif action == "wallet":
                from .wallets import balance_text

                async with asyncio.timeout(15):
                    balance = await self.engine.kosell.balance()
                self.db.set("kosell_balance", balance)
                await message.answer(
                    "💱 Кошельки KOSell\n\n"
                    + balance_text(balance)
                    + "\nОплата новых операций: "
                    + self.db.get("kosell_currency", "RUB")
                    + "\nКошельки не складываются. Лимит закупки задаётся в рублях по расчёту поставщика.",
                    reply_markup=buttons(
                        [("Оплачивать RUB", "v6:pay:RUB"), ("Оплачивать USD", "v6:pay:USD"), *BACK]
                    ),
                )
            elif action == "pay":
                if p[2] not in {"RUB", "USD"}:
                    raise ValueError("Неверная валюта")
                self.db.set("kosell_currency", p[2])
                self.db.set("last_readiness", 0)
                await message.answer(
                    "✅ Для новых закупок и продлений выбран кошелёк "
                    + p[2]
                    + ". Уже отправленные операции сохраняют прежнюю валюту.",
                    reply_markup=buttons(BACK),
                )
            elif action == "rules":
                rows = self.db.all("SELECT id,name,enabled FROM automation_rules ORDER BY priority DESC,id")
                page = max(0, min(int(p[2]) if len(p) > 2 else 0, max(0, (len(rows) - 1) // 15)))
                keys = [
                    (("🟢 " if r["enabled"] else "⚪ ") + r["name"][:35], f"v6:rule:{r['id']}")
                    for r in rows[page * 15 : (page + 1) * 15]
                ]
                if page:
                    keys.append(("←", f"v6:rules:{page - 1}"))
                if (page + 1) * 15 < len(rows):
                    keys.append(("→", f"v6:rules:{page + 1}"))
                await message.answer(
                    "🧩 Сценарии общения\nОдно сообщение — одно правило с наивысшим приоритетом. Оплата определяется только по данным заказа.",
                    reply_markup=buttons(
                        keys
                        + [
                            ("➕ Создать сценарий", "v6:rulenew"),
                            ("🧪 Проверить на примере", "v6:ruletest"),
                            *BACK,
                        ]
                    ),
                )
            elif action == "rulenew":
                await self.shop_prompt(
                    message, state, "rule_name", "Название сценария (до 80 символов).", rule_draft={}
                )
            elif action == "rule":
                r = self.db.one("SELECT * FROM automation_rules WHERE id=?", (int(p[2]),))
                if not r:
                    raise ValueError("Сценарий не найден")
                await message.answer(
                    f"🧩 {r['name']}\nФраза: {r['phrase']}\nУсловие: {CONDITIONS[r['condition']]}\nВремя: {PERIODS[r['period']]}\nДействие: {ACTIONS[r['action']]}\nОтвет: {r['reply']}\nПриоритет {r['priority']} · задержка {r['delay']} с · повтор через {r['cooldown']} с",
                    reply_markup=buttons(
                        [
                            ("Выключить" if r["enabled"] else "Включить", f"v6:ruletoggle:{r['id']}"),
                            ("✏ Изменить", f"v6:ruleedit:{r['id']}"),
                            ("🗑 Удалить", f"v6:ruledelete:{r['id']}"),
                            *BACK,
                        ]
                    ),
                )
            elif action == "ruleedit":
                r = self.db.one("SELECT * FROM automation_rules WHERE id=?", (int(p[2]),))
                if not r:
                    raise ValueError()
                await self.shop_prompt(
                    message,
                    state,
                    "rule_name",
                    "Новое название сценария (до 80 символов).",
                    rule_draft={"id": r["id"]},
                )
            elif action == "ruledelete":
                rid = int(p[2])
                self.db.execute(
                    "UPDATE outbox SET state='cancelled' WHERE state='pending' AND dedup LIKE ?",
                    (f"scenario:{rid}:%",),
                )
                self.db.execute("DELETE FROM automation_rules WHERE id=?", (rid,))
                await self.shop_callback(message, "v6:rules", state)
            elif action == "ruletoggle":
                self.db.execute("UPDATE automation_rules SET enabled=1-enabled WHERE id=?", (int(p[2]),))
                rule = self.db.one("SELECT enabled FROM automation_rules WHERE id=?", (int(p[2]),))
                if rule and not rule["enabled"]:
                    self.db.execute(
                        "UPDATE outbox SET state='cancelled' WHERE state='pending' AND dedup LIKE ?",
                        (f"scenario:{int(p[2])}:%",),
                    )
                return await self.shop_callback(message, "v6:rule:" + p[2], state)
            elif action in {"rulecondition", "ruleperiod", "ruleaction"}:
                r = context.get("rule_draft", {})
                if not r.get("phrase"):
                    raise ValueError("Форма устарела")
                if action == "rulecondition":
                    if p[2] not in CONDITIONS:
                        raise ValueError("Неверное условие")
                    r["condition"] = p[2]
                    await state.update_data(rule_draft=r)
                    await message.answer(
                        "Когда отвечать?",
                        reply_markup=buttons([(v, "v6:ruleperiod:" + k) for k, v in PERIODS.items()] + BACK),
                    )
                elif action == "ruleperiod":
                    if p[2] not in PERIODS:
                        raise ValueError("Неверный период")
                    r["period"] = p[2]
                    await state.update_data(rule_draft=r)
                    await message.answer(
                        "Выберите действие",
                        reply_markup=buttons([(v, "v6:ruleaction:" + k) for k, v in ACTIONS.items()] + BACK),
                    )
                else:
                    if p[2] not in ACTIONS:
                        raise ValueError("Неверное действие")
                    r["action"] = p[2]
                    await state.update_data(rule_draft=r)
                    await self.shop_prompt(
                        message,
                        state,
                        "rule_reply",
                        "Текст ответа (до 2000 символов). Для инструкции или передачи продавцу отправьте «-».",
                    )
            elif action == "rulesave":
                if context.get("shop_confirm") != p[2] or context.get("rule_expires", 0) < now():
                    raise ValueError("Подтверждение устарело")
                await state.update_data(shop_confirm=None)
                r = context["rule_draft"]
                columns = (
                    "name",
                    "phrase",
                    "condition",
                    "period",
                    "action",
                    "reply",
                    "priority",
                    "delay",
                    "cooldown",
                )
                if r.get("id"):
                    with self.db.transaction():
                        self.db.execute(
                            "UPDATE automation_rules SET "
                            + ",".join(k + "=?" for k in columns)
                            + ",enabled=0 WHERE id=?",
                            tuple(r[k] for k in columns) + (r["id"],),
                        )
                        self.db.execute(
                            "UPDATE outbox SET state='cancelled' WHERE state='pending' AND dedup LIKE ?",
                            (f"scenario:{r['id']}:%",),
                        )
                else:
                    if self.db.one("SELECT count(*) n FROM automation_rules")["n"] >= 100:
                        raise ValueError()
                    self.db.execute(
                        "INSERT INTO automation_rules("
                        + ",".join(columns)
                        + ") VALUES ("
                        + ",".join("?" for _ in columns)
                        + ")",
                        tuple(r[k] for k in columns),
                    )
                await self.shop_callback(message, "v6:rules", state)
            elif action == "ruletest":
                await self.shop_prompt(
                    message,
                    state,
                    "ruletest",
                    "Отправьте пример вопроса. Используются данные выбранного заказа."
                    if len(p) > 2
                    else "Отправьте пример вопроса. Проверка до покупки: без заказа, без отправки покупателю.",
                    preview_order_rid=int(p[2]) if len(p) > 2 else None,
                )
            else:
                raise ValueError("Раздел больше не доступен")
        except (ValueError, KeyError, TypeError, IndexError, ArithmeticError):
            # Never echo arbitrary input, raw JSON or provider bodies.
            await message.answer(
                "⚠️ Не удалось принять данные. Проверьте значения или начните этот шаг заново.",
                reply_markup=buttons(BACK),
            )
        except (ServiceError, TimeoutError):
            await message.answer(
                "⏳ Сервис пока не ответил. Сохранённые настройки не изменены; попробуйте позже.",
                reply_markup=buttons(BACK),
            )
        return True

    async def shop_upload(self, message, state):
        try:
            if message.document:
                if not message.document.file_size or message.document.file_size > 131072:
                    raise ValueError()
                output = io.BytesIO()
                await self.bot.download(message.document, destination=output)
                if output.tell() > 131072:
                    raise ValueError()
                text = output.getvalue().decode("utf-8")
            else:
                text = message.text or ""
            if len(text.encode()) > 131072:
                raise ValueError()
            tid = tools.import_template(self.db, json.loads(text))
            await state.clear()
            await self.shop_callback(message, "v6:template:" + tid, state)
        except (ValueError, KeyError, TypeError, ArithmeticError, UnicodeError):
            await message.answer(
                "⚠️ Файл не принят. Нужен экспортированный JSON-шаблон до 128 КБ, без посторонних полей.",
                reply_markup=buttons(BACK),
            )

    async def shop_input(self, message, state):
        context = await state.get_data()
        key = context.get("shop_key")
        text = (message.text or "").strip()
        try:
            if key in {"lotsearch", "gamesearch"}:
                await state.update_data(
                    **{"market_query" if key == "lotsearch" else "kosell_query": text[:100]}
                )
                return await (
                    self.shop_lots(message, state)
                    if key == "lotsearch"
                    else self.kosell_choices(message, state)
                )
            if key == "tplname":
                tid = tools.save_template(self.db, context["template_source"], text)
                await state.set_state(None)
                return await self.shop_callback(message, "v6:template:" + tid, state)
            if key == "note":
                row = self.db.one("SELECT buyer_id FROM orders WHERE rowid=?", (context["customer_rid"],))
                if not row:
                    raise ValueError()
                tools.set_customer(self.db, row["buyer_id"], note="" if text == "-" else text)
                await state.set_state(None)
                return await self.customer_card(message, context["customer_rid"])
            if key.startswith("price_"):
                d = context["price_data"]
                v = Decimal(text.replace(",", "."))
                if not v.is_finite() or v < 0 or v > Decimal("1e12"):
                    raise ValueError()
                field = key.removeprefix("price_")
                if field in {"fee", "margin"} and v >= 100:
                    raise ValueError()
                d[field] = str(v)
                await state.update_data(price_data=d)
                fields = ["cost", "margin", "fee", "floor", "ceiling"]
                prompts = {
                    "margin": "Желаемая маржа от продажной цены, %?",
                    "fee": "Учитываемая комиссия, %? Укажите известную ставку; 0 означает без учёта комиссии.",
                    "floor": "Нижняя граница цены, руб.?",
                    "ceiling": "Верхняя граница цены, руб.?",
                }
                index = fields.index(field)
                if index < 4:
                    return await self.shop_prompt(
                        message, state, "price_" + fields[index + 1], prompts[fields[index + 1]]
                    )
                if now() - d["at"] > 300:
                    raise ValueError("Расчёт устарел")
                price = tools.price_suggestion(d["cost"], d["fee"], d["margin"], d["floor"], d["ceiling"])
                await state.set_state(None)
                return await message.answer(
                    f"💰 Рекомендуемая цена: {price} ₽\nИсточник: {d['source']}\nСебестоимость {d['cost']} ₽ · комиссия {d['fee']}% · маржа {d['margin']}%\nГраницы {d['floor']}–{d['ceiling']} ₽.\n\nЭто рекомендация по известным данным. Цена на Playerok не изменялась. Автоматическое изменение цен в этой версии недоступно; рекомендацию можно использовать при настройке цены на площадке.",
                    reply_markup=buttons(BACK),
                )
            if key == "ruletest":
                selected = (
                    self.db.one("SELECT * FROM orders WHERE rowid=?", (context.get("preview_order_rid"),))
                    if context.get("preview_order_rid")
                    else None
                )
                r = choose(self.engine, text, selected)
                await state.set_state(None)
                return await message.answer(
                    ("🧪 По текущему заказу: " if selected else "🧪 До покупки: ")
                    + (
                        "сработает «" + r["name"] + "» · " + ACTIONS[r["action"]]
                        if r
                        else "подходящего сценария нет"
                    )
                    + ". Сообщения не отправлены.",
                    reply_markup=buttons(BACK),
                )
            if key.startswith("rule_"):
                r = context.get("rule_draft", {})
                field = key.removeprefix("rule_")
                if field in {"name", "phrase", "reply"}:
                    if not 1 <= len(text) <= (2000 if field == "reply" else 80 if field == "name" else 200):
                        raise ValueError()
                    r[field] = "" if field == "reply" and text == "-" else text
                    if field == "reply" and r.get("action") == "reply" and not r[field]:
                        raise ValueError()
                else:
                    value = int(text)
                    limits = {"priority": (0, 1000), "delay": (0, 3600), "cooldown": (1, 86400)}
                    if not limits[field][0] <= value <= limits[field][1]:
                        raise ValueError()
                    r[field] = value
                await state.update_data(rule_draft=r)
                if field == "name":
                    return await self.shop_prompt(
                        message, state, "rule_phrase", "Какую фразу искать в сообщении покупателя?"
                    )
                if field == "phrase":
                    await state.set_state(None)
                    return await message.answer(
                        "При каком состоянии заказа?",
                        reply_markup=buttons(
                            [(v, "v6:rulecondition:" + k) for k, v in CONDITIONS.items()] + BACK
                        ),
                    )
                next_field = {"reply": "priority", "priority": "delay", "delay": "cooldown"}.get(field)
                if next_field:
                    return await self.shop_prompt(
                        message,
                        state,
                        "rule_" + next_field,
                        {
                            "priority": "Приоритет 0–1000: больше — раньше.",
                            "delay": "Задержка ответа в секундах, 0–3600.",
                            "cooldown": "Не повторять ответ чаще, чем раз в N секунд (1–86400).",
                        }[next_field],
                        [
                            (
                                "Без задержки" if next_field == "delay" else "60",
                                "v6:input:" + ("0" if next_field == "delay" else "60"),
                            )
                        ],
                    )
                import uuid

                nonce = uuid.uuid4().hex[:12]
                await state.set_state(None)
                await state.update_data(shop_confirm=nonce, rule_expires=now() + 300)
                return await message.answer(
                    f"🧩 {r['name']}\nФраза: {r['phrase']}\n{CONDITIONS[r['condition']]} · {PERIODS[r['period']]}\n{ACTIONS[r['action']]}\nПриоритет {r['priority']}, задержка {r['delay']} с, повтор через {r['cooldown']} с.\nСохраним выключенным. Затем можно включить кнопкой.",
                    reply_markup=buttons([("✅ Сохранить", "v6:rulesave:" + nonce), *BACK]),
                )
        except (ValueError, KeyError, TypeError, ArithmeticError):
            await message.answer(
                "⚠️ Проверьте формат и допустимые границы. Данные не сохранены.",
                reply_markup=buttons([("‹ Назад", "ui:back"), *BACK]),
            )
        except (ServiceError, TimeoutError):
            await message.answer("Сервис пока недоступен. Повторите позже.", reply_markup=buttons(BACK))
