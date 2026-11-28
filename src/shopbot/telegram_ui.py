from __future__ import annotations

import io
import json
import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import __version__
from .account_ui import AccountUI, ConnectionForm, lot_error_text
from .analytics import Analytics
from .checks import connection_snapshot, preview_delivery
from .engine import Engine
from .external_ui import ExternalUI
from .loyalty import Loyalty
from .maintenance import update_status
from .models import ServiceError, money, now, rub, timestamp
from .navigation import NavigationMessages, PanelNavigation
from .navigation import context as panel_context
from .presentation import Cards
from .presentation import markup as card_markup
from .presentation import price as card_price
from .presentation import safe as safe_html
from .rentals import Rentals
from .seller_ui import MODE_NAMES, SellerUI
from .shop_ui import ShopForm, ShopUI
from .ui_routes import parent_route, view_route
from .ui_texts import issue_text, stage_label
from .updates import Updates, read_json, release_summary
from .wallets import balance_text

LABELS = {
    "greeting": "👋 Приветствие",
    "auto_confirm": "🤝 Автоподтверждение выполнения",
    "confirm_reminder": "⏰ Напоминание о подтверждении",
    "review_reminder": "⭐ Просьба об отзыве",
    "offline": "💤 Текст вне рабочего времени",
    "ignore": "💬 Покупатель не отвечает",
    "review": "💬 Текст после отзыва",
    "problem": "💬 Текст при проблеме",
    "seller_confirmed": "💬 После выполнения продавцом",
    "buyer_confirmed": "💬 После подтверждения покупателем",
}


def keyboard(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=data)] for label, data in rows]
    )


class Form(StatesGroup):
    value = State()
    binding = State()
    stock = State()
    reply = State()


class AdminOnly(BaseMiddleware):
    def __init__(self, admins, maintenance=None):
        self.admins = set(admins)
        self.maintenance = maintenance

    async def __call__(self, handler, event, data):
        if not event.from_user or event.from_user.id not in self.admins:
            if isinstance(event, CallbackQuery):
                await event.answer("Нет доступа", show_alert=True)
            return None
        if self.maintenance and self.maintenance.exists():
            text = update_status(self.maintenance.parent.parent)
            result = read_json(self.maintenance.parent / "result.json", {})
            if isinstance(result, dict) and result.get("status") == "manual_required":
                text = "🛠 Восстановление остановлено. Откройте инструкцию по восстановлению в репозитории PlayerokHelper."
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            else:
                await event.answer(text)
            return None
        return await handler(event, data)


class TelegramUI(ShopUI, AccountUI, ExternalUI, SellerUI):
    def __init__(self, bot: Bot, engine: Engine, admins, data_dir, project_dir=None):
        self.bot, self.engine, self.db = bot, engine, engine.db
        self.data_dir = data_dir
        self.analytics = Analytics(self.db)
        self.cards = Cards(engine)
        self.updates = Updates(self.db, project_dir)
        self.router = Router()
        maintenance = self.updates.folder / "maintenance.json" if self.updates.folder else None
        self.router.message.outer_middleware(AdminOnly(admins, maintenance))
        self.router.callback_query.outer_middleware(AdminOnly(admins, maintenance))
        self.init_connections()
        self.panels = {}
        self.router.message.outer_middleware(PanelNavigation(self.panels, self.db))
        self.router.callback_query.outer_middleware(PanelNavigation(self.panels, self.db))
        self.bot.session.middleware(NavigationMessages())
        self.register()

    def register(self):
        r = self.router
        r.message.register(self.start, CommandStart())
        r.message.register(self.cancel, Command("cancel"))
        r.message.register(self.start, Command("menu"))
        r.message.register(self.setup_menu, Command("setup"))
        r.message.register(self.updates_menu, Command("version"))
        r.message.register(self.workspace, Command("inbox"))
        r.message.register(self.status, Command("status"))
        r.message.register(self.bind_start, Command("bind"))
        r.message.register(self.report_command, Command("report"))
        r.message.register(self.reply_command, Command("reply"))
        r.message.register(self.import_history, Command("import"))
        r.message.register(
            self.connection_input,
            StateFilter(
                ConnectionForm.token,
                ConnectionForm.cookie,
                ConnectionForm.user_agent,
                ConnectionForm.email,
                ConnectionForm.code,
                ConnectionForm.key,
                ConnectionForm.visibility,
            ),
        )
        r.message.register(self.shop_input, StateFilter(ShopForm.value))
        r.message.register(self.shop_upload, StateFilter(ShopForm.upload))
        r.message.register(self.value, StateFilter(Form.value))
        r.message.register(self.binding_input, StateFilter(Form.binding))
        r.message.register(self.stock_input, StateFilter(Form.stock))
        r.message.register(self.reply_input, StateFilter(Form.reply))
        r.callback_query.register(self.callback)
        r.message.register(self.help)

    async def start(self, message: Message, state: FSMContext):
        await state.clear()
        await self.expire_logins(message.chat.id)
        self.db.set("update_offer", None)
        status = (
            "⏸ Приостановлена"
            if self.db.get("paused")
            else "🟢 Работает"
            if self.db.get("live_enabled") and now() - self.db.get("last_poll", 0) < 90
            else "🟡 Ожидает связи"
            if self.db.get("live_enabled")
            else "⚪ Не запущена"
        )
        shop = (self.db.get("profile") or {}).get("username") or "аккаунт пока не подключён"
        await message.answer(
            "🎮 PlayerokHelper\n"
            f"Ваш магазин · {shop}\n\n"
            f"Автоматизация: {status}\n"
            f"🔌 Подключение: {self.readiness_label()}\n\n"
            f"Версия {__version__} · выберите нужный раздел ↓",
            reply_markup=keyboard(
                ([("➕ Добавить аккаунт Playerok", "connect:playerok")] if not self.db.get("profile") else [])
                + [
                    ("⭐ Автонастройки", "menu:auto"),
                    ("📦 Мои лоты", "bindings:all"),
                    ("👤 Профиль", "menu:profile"),
                    ("📈 Аналитика", "menu:analytics"),
                    ("⚡ Плагины", "menu:plugins"),
                    ("⚙️ Управление магазином", "menu:account"),
                    ("📞 Поддержка", "menu:help"),
                ]
            ),
        )

    async def cancel(self, message, state):
        data = await state.get_data()
        panel = panel_context.get()
        route = view_route(data.get("_nav_origin", "")) or (panel.parent if panel else "menu:home")
        await state.clear()
        self.db.set("update_offer", None)
        await self.expire_logins(message.chat.id)
        return await self.navigate(message, route, state)

    def readiness_label(self):
        value = self.db.get("readiness_status")
        if not value or value == "не проверено":
            return "➕ Добавьте аккаунт" if not self.db.get("profile") else "🔄 Подключаемся"
        if value == "готово":
            return "✅ Подключён"
        return "🔌 Откройте подключения" if "_" in value else value

    async def status(self, message, state=None):
        status = self.engine.diagnostic()
        labels = {
            "orders": "заказы",
            "chats": "сообщения",
            "listings": "управление лотами",
            "providers": "поставщики",
            "delivery": "уведомления",
            "local": "отчёты",
        }
        unavailable = []
        for name, label in labels.items():
            health = self.db.get("worker_health:" + name, {})
            if not health.get("ok", True) and health.get("failures", 0) >= 3:
                unavailable.append(label)
        background = (
            "Повторяем подключение: " + ", ".join(unavailable)
            if unavailable
            else "Автоматический контроль включён"
        )
        await message.answer(
            "📋 Состояние магазина\n\n"
            f"🔌 Подключение: {self.readiness_label()}\n"
            f"⏸ Пауза: {'да' if status['paused'] else 'нет'}\n\n🛍 Заказов: {status['orders']}\n"
            f"⏸ Приостановлено заказов: {status['review']}\n✉️ Без ответа о доставке: {status['uncertain']}\n"
            f"Внешних заказов в работе: {status['provider_pending']}\nВнешних заказов на сверке: {status['provider_review']}\n"
            f"Последняя успешная сверка: {self.engine.expiry(self.db.get('last_poll'))}\n\n"
            f"🔄 {background}",
            reply_markup=keyboard([("🔌 Подключения", "connect:home"), ("🏠 Главное меню", "menu:home")]),
        )

    async def setup_menu(self, message, state):
        await state.clear()
        snapshot = await self.refresh_connections()
        services = snapshot["services"]
        profile = self.db.get("profile") or {}
        playerok_ok = services.get("playerok", {}).get("ok") is True
        kosell = services.get("kosell", {})
        source = self.db.get("setup_source")
        count = self.db.one("SELECT count(*) n FROM bindings")["n"]
        lines = [
            "⚙️ Ваш магазин",
            "",
            "🎮 Playerok: "
            + ("✅ " + profile.get("username", "подключён") if playerok_ok else "не подключён"),
            "🎮 KOSell: "
            + (
                "✅ " + balance_text(self.db.get("kosell_balance") or {})
                if kosell.get("ok")
                else "временно нет связи"
                if kosell.get("configured")
                else "не добавлен"
            ),
            "",
            "📦 Настроено лотов: " + str(count),
            "Источник: "
            + {"kosell": "KOSell", "stock": "свой склад", "stars": "Telegram Stars", "smm": "SMM"}.get(
                source, "выберите ниже"
            ),
            "",
            "✨ Подключения и баланс обновляются автоматически.",
        ]
        if not count:
            lines += [
                "",
                "Выберите свой лот, товар поставщика, срок аренды и лимит закупки. После сохранения бот начнёт обрабатывать новые оплаченные заказы этого лота.",
            ]
        if self.db.get("visibility_test_pending"):
            lines += [
                "",
                "🛠 Восстанавливаем прежний тестовый лот отдельно. Он временно исключён из автоматизации.",
            ]
        rows = []
        if not playerok_ok:
            rows.append(("➕ Добавить Playerok", "connect:playerok"))
        if not kosell.get("configured"):
            rows.append(("🎮 Подключить KOSell", "connect:key:kosell"))
        else:
            rows.append(("💱 Кошельки KOSell · " + self.db.get("kosell_currency", "RUB"), "v6:wallet"))
        rows += [
            ("📦 Настроить лот", "bind:new"),
            ("🛍 Мои лоты", "bindings:all"),
            ("🎮 Использовать KOSell", "setup:source:kosell"),
            ("📦 Использовать свой склад", "setup:source:stock"),
            ("⭐ Telegram Stars", "setup:source:stars"),
            ("💬 SMM-услуги", "setup:source:smm"),
            ("🔌 Управлять подключениями", "connect:home"),
        ]
        if source in {"stars", "smm"}:
            rows.append(("⚙️ Настроить поставщика", "x:home:" + source))
        if self.db.get("visibility_test_pending"):
            rows.append(("🛠 Помощь с прежним лотом", "connect:visibility:recover"))
        rows.append(("🏠 Главное меню", "menu:home"))
        await message.answer("\n".join(lines), reply_markup=keyboard(rows))

    async def alerts_menu(self, message):
        await message.answer(
            "🚨 Ранние предупреждения (для всего магазина)\n"
            f"Состояние: {'включены' if self.db.get('alerts_enabled') else 'выключены'}\n"
            f"Остаток склада ≤ {self.db.get('stock_low')}\n"
            f"Баланс примерно на ≤ {self.db.get('rental_low')} аренд каждого лота KOSell\n"
            "Оценки используют общий баланс и не суммируются. Для продлений оценка не строится.\n"
            "Проверка раз в минуту. Сбой подключения: три неудачи подряд; восстановление: два успеха.\n"
            "При появлении проблемы и восстановлении приходит по одному уведомлению. "
            "Повторное уведомление — только при новом возникновении проблемы.",
            reply_markup=keyboard(
                [
                    ("Выключить" if self.db.get("alerts_enabled") else "Включить", "alerts:toggle"),
                    ("✏ Порог склада", "alerts:edit:stock_low"),
                    ("✏ Порог аренд", "alerts:edit:rental_low"),
                    ("‹ Назад", "menu:auto"),
                ]
            ),
        )

    async def preview_menu(self, message, item_id):
        binding = self.db.one("SELECT rowid AS rid,* FROM bindings WHERE item_id=?", (item_id,))
        if not binding:
            return await message.answer("Лот не найден.")
        if binding["module"] != "extend" and binding["fulfillment"] != "own_extend":
            return await self.show_preview(message, item_id)
        if binding["fulfillment"] == "own_extend":
            rentals = self.db.all(
                "SELECT o.rowid rid,o.id,o.rental_number FROM orders o JOIN own_leases l ON l.order_id=o.id "
                "WHERE o.item_id=? AND o.module='own_rent' AND o.stage='delivered' AND o.market_status!='ROLLED_BACK' "
                "AND o.problem=0 AND l.state='active' AND l.expires_at>? ORDER BY o.created_at DESC LIMIT 20",
                (binding["pool_item"], now()),
            )
        else:
            rentals = self.db.all(
                "SELECT o.rowid AS rid,o.id,o.rental_number FROM orders o "
                "JOIN bindings b ON b.item_id=o.item_id WHERE o.module='kosell' "
                "AND o.stage='delivered' AND o.market_status!='ROLLED_BACK' AND o.problem=0 "
                "AND o.expires_at>? AND b.product_id=? ORDER BY o.created_at DESC LIMIT 20",
                (now(), binding["product_id"]),
            )
        await message.answer(
            "Выберите действующую аренду своего магазина для расчёта продления. "
            "Списания и продления не будет. Показаны до 20 последних подходящих аренд."
            if rentals
            else "Подходящих действующих аренд нет. Продление проверить пока нельзя.",
            reply_markup=keyboard(
                [
                    (r["rental_number"] or r["id"], f"preview:extend:{binding['rid']}:{r['rid']}")
                    for r in rentals
                ]
                + [("‹ К лоту", "lot:" + item_id)]
            ),
        )

    async def show_preview(self, message, item_id, rental_id=None):
        result = await preview_delivery(self.engine, item_id, rental_id=rental_id)
        binding = self.db.one("SELECT * FROM bindings WHERE item_id=?", (item_id,))
        if binding:
            self.db.set(
                "last_preview",
                {
                    "item_id": item_id,
                    "at": result.checked_at,
                    "ok": not result.issues,
                    "binding": result.binding,
                },
            )
        await message.answer(
            result.text(self.engine.expiry),
            reply_markup=keyboard([("‹ К лоту", "lot:" + item_id), ("🧙 Мастер", "setup:home")]),
        )

    async def updates_menu(self, message, state=None):
        self.db.set("update_offer", None)
        if state:
            await state.clear()
        release = self.updates.available()
        lines = [
            "⬆️ Обновления PlayerokHelper",
            f"📦 Установлена версия {__version__}",
            "🕒 Проверка версий — каждые 6 часов.",
            "Последняя проверка: " + self.engine.expiry(self.db.get("update_check_at")),
        ]
        if self.db.get("update_check_error"):
            lines.append(self.db.get("update_check_error"))
        rows = [("🔄 Проверить сейчас", "updates:check")]
        if release:
            lines += ["\n✨ Доступна " + release["tag"], "", release_summary(release["body"])]
            if self.updates.enabled() and not self.updates.busy():
                rows.append(("⬆️ Обновить бота", "updates:offer"))
        else:
            lines.append(
                "Новая версия пока не найдена. Нажмите «Проверить сейчас» для актуального результата."
            )
        if "b" in __version__:
            lines.append(
                "Это бета-сборка. Обновление кнопкой отключено; переход на стабильную версию выполняется вручную после её выпуска."
            )
        elif not self.updates.enabled():
            lines.append(
                "Обновление кнопкой требует однократной установки v0.3.0+ со службой systemd. "
                "Для Docker и ручного запуска обновляйте установку обычным способом."
            )
        if self.updates.busy():
            lines.append("Обновление уже поставлено в очередь или выполняется.")
        history = self.db.get("update_history", [])[:3]
        if history:
            lines.append("\n🕘 Последние обновления")
        for item in history:
            label = {
                "success": "установлено",
                "rolled_back": "выполнен откат",
                "failed": "не установлено",
                "manual_required": "восстановление остановлено",
            }.get(item.get("status"), "результат ещё не получен")
            lines.append(f"\n{self.engine.expiry(item.get('finished_at'))}: {item.get('tag')} — {label}")
            reason = {
                "modified_project": "В проекте есть локальные изменения; они сохранены.",
                "main_branch_required": "Для обновления требуется ветка main.",
                "release_tag_changed": "Версия на GitHub изменилась. Нажмите «Проверить сейчас».",
                "release_changes_private_files": "Релиз затрагивает настройки или данные; установка остановлена.",
                "command_failed": "Не выполнена загрузка, сборка или установка; прежняя версия сохранена либо восстановлена.",
                "health_check_failed": "Новая версия не подтвердила успешный запуск.",
                "worker_still_running": "Другой процесс использует базу магазина.",
                "project_changed_during_update": "Файлы проекта изменились во время подготовки.",
                "data_directory_changed": "Каталог базы изменён. Повторите обычную установку от прежнего пользователя сервера.",
            }.get(item.get("reason"))
            if reason:
                lines.append(reason)
        rows += [
            ("📋 Статус обновления", "s:update-status"),
            ("🧰 Скачать диагностику", "s:diagnostics"),
            (
                "🔔 Уведомления: " + ("вкл" if self.db.get("update_notifications") else "выкл"),
                "updates:toggle",
            ),
            ("‹ Назад", "menu:account"),
        ]
        markup = keyboard(rows)
        if release:
            markup.inline_keyboard.insert(1, [InlineKeyboardButton(text="📖 Что нового", url=release["url"])])
        await message.answer("\n".join(lines)[:4000], reply_markup=markup)

    async def callback(self, query: CallbackQuery, state: FSMContext):
        await query.answer()
        return await self.navigate(query.message, query.data or "", state)

    async def navigate(self, message, data, state):
        panel = panel_context.get()
        route = view_route(data)
        if panel and route:
            panel.route, panel.parent = route, parent_route(route)
            panel.persist()
        if await state.get_state() in {ConnectionForm.cookie.state, ConnectionForm.user_agent.state}:
            await state.clear()
        if data.startswith("card:"):
            data = self.cards.route(data)
            if data is None:
                return await message.answer("Это событие больше не доступно. Откройте рабочее окно.")
        if not data.startswith("reply_send:"):
            # Navigating away invalidates a reply preview before another chat can
            # become the target of a delayed click from the previous screen.
            await state.update_data(reply_nonce=None, reply_text=None)
        if not data.startswith(("v6:tplapply:", "v6:rulesave:")):
            await state.update_data(shop_confirm=None)
        if not data.startswith("x:confirm:"):
            await state.update_data(external_confirm=None)
        if data == "ui:cancel":
            return await self.cancel(message, state)
        if data == "ui:back":
            context = await state.get_data()
            stack = context.get("_nav_stack", [])
            if stack:
                previous = stack.pop()
                await state.set_state(previous["state"])
                await state.set_data({**previous["data"], "_nav_stack": stack})
                prompt = previous.get("prompt")
                if prompt:
                    panel = panel_context.get()
                    if panel and view_route(prompt.get("route", "")):
                        panel.route = prompt["route"]
                        panel.parent = prompt.get("parent", parent_route(panel.route))
                    return await message.answer(
                        prompt["text"],
                        parse_mode=prompt.get("parse_mode"),
                        entities=prompt.get("entities"),
                        reply_markup=InlineKeyboardMarkup.model_validate(prompt["markup"])
                        if prompt["markup"]
                        else None,
                    )
            panel = panel_context.get()
            route = view_route(context.get("_nav_origin", "")) or (panel.parent if panel else "menu:home")
            # Older generic Back buttons can outlive the in-memory form.
            if route == "menu:home" and "правила" in (message.text or "").casefold():
                route = "auto:replies"
            await state.clear()
            return await self.navigate(message, route, state)
        if (await state.get_state() or "").startswith("ConnectionForm:") and not data.startswith("connect:"):
            await self.expire_logins(message.chat.id)
        # Visibility confirmation verifies both the active form and its nonce.
        if data != "connect:visibility:run" and not data.startswith("connect:visibility:run:"):
            await state.set_state(None)
        try:
            if await self.shop_callback(message, data, state):
                return
            if data == "connect:status":
                return await self.status(message)
            if await self.connection_callback(message, data, state):
                return
            if await self.external_callback(message, data, state):
                return
            if await self.seller_callback(message, data, state):
                return
            if data == "updates:home":
                return await self.updates_menu(message, state)
            if data == "updates:check":
                await self.updates.check(force=True)
                return await self.updates_menu(message, state)
            if data == "updates:toggle":
                self.db.set("update_notifications", not self.db.get("update_notifications"))
                return await self.updates_menu(message, state)
            if data == "updates:offer":
                offer = self.updates.offer()
                return await message.answer(
                    f"⬆️ Обновление до {offer['tag']}\n\n"
                    "📦 Подготовим новую версию.\n"
                    "🗃 Создадим резервную копию базы.\n"
                    "🔄 Перезапустим бота и проверим запуск.\n\n"
                    "При ошибке запуска вернём прежнюю версию и базу. "
                    "Во время установки заказы и настройки временно недоступны.\n\n"
                    "⏱ Предложение действует 10 минут.",
                    reply_markup=keyboard(
                        [
                            ("✅ Установить " + offer["tag"], "updates:install:" + offer["nonce"]),
                            ("Отмена", "updates:home"),
                        ]
                    ),
                )
            if data.startswith("updates:install:"):
                try:
                    self.updates.request(data.split(":", 2)[2])
                except ValueError as exc:
                    return await message.answer(str(exc))
                return await message.answer(
                    "⏳ Обновление поставлено в очередь\n\n"
                    "Подготовка может занять несколько минут.\n"
                    "После запуска придёт сообщение с результатом."
                )
            if data == "menu:home":
                return await self.start(message, state)
            if data == "setup:home":
                return await self.setup_menu(message, state)
            if data.startswith("setup:source:"):
                source = data.rsplit(":", 1)[1]
                if source not in {"stock", "kosell", "stars", "smm"}:
                    return
                self.db.set("setup_source", source)
                return await self.setup_menu(message, state)
            if data == "alerts:home":
                return await self.alerts_menu(message)
            if data == "alerts:toggle":
                self.db.set("alerts_enabled", not self.db.get("alerts_enabled"))
                return await self.alerts_menu(message)
            if data.startswith("alerts:edit:"):
                key = data.rsplit(":", 1)[1]
                if key not in {"stock_low", "rental_low"}:
                    return
                await state.set_state(Form.value)
                await state.update_data(edit=key)
                return await message.answer("Введите целый порог от 0 до 10000.")
            if data.startswith("preview:lot:"):
                return await self.preview_menu(message, data.split(":", 2)[2])
            if data.startswith("preview:extend:"):
                _, _, binding_row, rental_row = data.split(":")
                binding = self.db.one("SELECT * FROM bindings WHERE rowid=?", (int(binding_row),))
                rental = self.db.one("SELECT id FROM orders WHERE rowid=?", (int(rental_row),))
                if (
                    binding
                    and (binding["module"] == "extend" or binding["fulfillment"] == "own_extend")
                    and rental
                ):
                    return await self.show_preview(message, binding["item_id"], rental["id"])
                return await message.answer("Лот или аренда больше не доступны. Откройте карточку лота.")
            if data == "menu:auto":
                return await self.auto_menu(message, state)
            if data == "menu:profile":
                return await self.profile(message)
            if data == "menu:plugins":
                return await message.answer(
                    "⚡ Плагины магазина\n\nВыберите направление и настройте выдачу товаров.",
                    reply_markup=keyboard(
                        [
                            ("🎮 Аренда KOSell", "v6:wallet"),
                            ("🎮 Свой пул аренды", "s:own-catalog"),
                            ("Telegram Stars", "x:home:stars"),
                            ("SMM-услуги", "x:home:smm"),
                            ("🎁 Бонусные товары", "s:loyalty"),
                            ("‹ Назад", "menu:home"),
                        ]
                    ),
                )
            if data.startswith("plugin:"):
                module = data.split(":")[1]
                return await self.bindings_menu(message, module, state)
            if data == "menu:analytics":
                return await self.analytics_menu(message)
            if data == "menu:account":
                return await self.account_menu(message)
            if data == "menu:help":
                return await self.help(message)
            if data == "menu:attention":
                return await self.attention_menu(message)
            if data.startswith("toggle:"):
                key = data.split(":")[1]
                if key not in {"daily_report", "paused", "paid_enabled", "offline_override"}:
                    return
                self.db.set(key, not self.db.get(key))
                if key == "daily_report":
                    return await self.navigate(message, "analytics:settings", state)
                if key == "paused":
                    return await self.navigate(message, "menu:account", state)
                if key == "paid_enabled":
                    return await self.navigate(message, "promotion", state)
                return await self.navigate(message, "auto:schedule", state)
            if data.startswith("feature:"):
                key = data.split(":")[1]
                if key not in LABELS:
                    return
                context = await state.get_data()
                item_id = context.get("scope")
                if item_id:
                    overrides = self.db.get("override:" + item_id, {})
                    overrides.setdefault("features", {})[key] = not self.db.feature(key, item_id)
                    self.db.set("override:" + item_id, overrides)
                else:
                    features = self.db.get("features")
                    features[key] = not features[key]
                    self.db.set("features", features)
                return await self.navigate(message, "auto:event:" + key, state)
            if data in {"auto:messages", "auto:replies", "auto:schedule"}:
                return await self.automation_section(message, state, data.split(":")[1])
            if data.startswith("auto:event:"):
                return await self.event_settings(message, state, data.split(":", 2)[2])
            if data == "templates":
                return await self.automation_section(message, state, "messages")
            if data == "analytics:settings":
                return await self.report_settings(message)
            if data.startswith("text:"):
                key = data.split(":")[1]
                if key not in LABELS or key == "auto_confirm":
                    return
                await state.set_state(Form.value)
                await state.update_data(edit="template", template=key)
                return await message.answer(
                    "Пришлите новый текст (до 2000 символов). Переменные: "
                    "{buyer}, {order}, {title}, {expires}."
                )
            if data.startswith("scope:"):
                scope = data.split(":", 1)[1]
                if scope != "global" and not self.db.one(
                    "SELECT item_id FROM bindings WHERE item_id=?", (scope,)
                ):
                    return
                await state.update_data(scope=None if scope == "global" else scope)
                return await self.auto_menu(message, state)
            if data == "scopes":
                return await self.catalog(message, state, module="", role="scope", page=0)
            if data.startswith("edit:"):
                key = data.split(":")[1]
                prompts = {
                    "rules": "Пришлите правила: одна строка «фраза | ответ | приоритет». Пустой список: -",
                    "work_hours": "Рабочие часы по Москве: например 9-23. Для круглосуточной работы: 0-24.",
                    "action_limit": "Максимальная стоимость одной операции в рублях (0 запрещает платные действия).",
                    "daily_budget": "Общий суточный бюджет продвижения в рублях. День считается по часовому поясу магазина.",
                    "fee_percent": "Комиссия Playerok в процентах: например 10. Отправьте - , чтобы добавить комиссию позже.",
                    "report_time": "Время ежедневного отчёта: ЧЧ:ММ, часовой пояс магазина.",
                    "report_filter": "Период: ГГГГ-ММ-ДД ГГГГ-ММ-ДД (обе даты включительно). "
                    "Необязательно добавьте item=ID или module=kosell/stock/extend.",
                }
                if key not in prompts:
                    return
                await state.set_state(Form.value)
                await state.update_data(edit=key)
                return await message.answer(prompts[key])
            if data == "promotion":
                return await self.promotion_menu(message)
            if data == "bindings:all":
                return await self.bindings_menu(message, state=state)
            if data == "bind:new":
                return await self.bind_start(message, state)
            if data.startswith("lot:"):
                return await self.lot(message, data[4:])
            if data.startswith("lotflag:"):
                _, field, item_id = data.split(":", 2)
                if field not in {"paused", "auto_restore", "auto_bump"}:
                    return
                self.db.execute(f"UPDATE bindings SET {field}=1-{field} WHERE item_id=?", (item_id,))
                return await self.lot(message, item_id)
            if data.startswith("lotedit:"):
                _, field, item_id = data.split(":", 2)
                if field not in {"max_cost", "hours", "instruction", "bump_interval"}:
                    return
                await state.set_state(Form.value)
                await state.update_data(edit="lot", field=field, item_id=item_id)
                names = {
                    "max_cost": "Лимит закупки, руб.",
                    "hours": "Длительность, часов",
                    "instruction": "Инструкция покупателю (до 1000 символов)",
                    "bump_interval": "Интервал поднятия, минут (минимум 5)",
                }
                return await message.answer(names[field] + ".")
            if data.startswith("stock:"):
                item_id = data[6:]
                binding = self.db.one("SELECT * FROM bindings WHERE item_id=? AND module='stock'", (item_id,))
                if not binding:
                    return
                await state.set_state(Form.stock)
                await state.update_data(item_id=item_id, warehouse="stock")
                return await message.answer(
                    "Пришлите UTF-8 TXT или текст: одна строка — один товар. "
                    "Для общей себестоимости добавьте первой строкой «cost=12.50». "
                    "Без этой строки стоимость можно добавить позже."
                )
            if data.startswith("orders:"):
                return await self.orders(message, max(0, int(data.split(":")[1])))
            if data.startswith("attention-orders:"):
                return await self.orders(message, max(0, int(data.split(":")[1])), attention=True)
            if data.startswith("order:"):
                return await self.order_card(message, data[6:])
            if data.startswith("reply:"):
                order = self.engine.order(data[6:])
                if not order:
                    return
                await state.set_state(Form.reply)
                await state.update_data(chat_id=order["chat_id"])
                return await message.answer(f"Ответ в чат {order['chat_id']}. Пришлите текст.")
            if data.startswith("reply_send:"):
                context = await state.get_data()
                if "reply_text" not in context or data.split(":", 1)[1] != context.get("reply_nonce"):
                    return await message.answer("Предпросмотр устарел. Подготовьте ответ заново.")
                self.db.enqueue(
                    "playerok",
                    context["chat_id"],
                    context["reply_text"],
                    "operator_reply:" + str(uuid.uuid4()),
                    purpose="manual_reply",
                )
                await state.clear()
                return await message.answer("Ответ поставлен в очередь.")
            if data.startswith("resume:"):
                order = self.engine.order(data[7:])
                if order:
                    self.db.execute("UPDATE chats SET operator_paused=0 WHERE id=?", (order["chat_id"],))
                    return await message.answer("Автоответы в чате возобновлены.")
            if data.startswith("refund:"):
                order = self.engine.order(data[7:])
                if order:
                    return await message.answer(
                        f"Вернуть средства по заказу {order['id']}? Сумма заказа: "
                        f"{money(order['price'])}. KOSell автоматически не возвращает закупку.",
                        reply_markup=keyboard(
                            [
                                ("Подтверждаю возврат", "refund_yes:" + order["id"]),
                                ("Отмена", "order:" + order["id"]),
                            ]
                        ),
                    )
            if data.startswith("refund_yes:"):
                order_id = data[11:]
                snapshot = await connection_snapshot(
                    self.db, self.engine.market, self.engine.kosell, check_kosell=False
                )
                if not snapshot["services"]["playerok"]["ok"]:
                    return await message.answer(
                        "🔌 Нет связи с Playerok\n\nВозврат не отправлен. Восстановите подключение и откройте заказ заново.",
                        reply_markup=keyboard(
                            [("🔌 Подключения", "connect:home"), ("‹ Заказ", "order:" + order_id)]
                        ),
                    )
                self.db.audit("refund_requested", order_id)
                fresh = await self.engine.market.deal(order_id)
                if fresh.status != "ROLLED_BACK":
                    # Persist pending intent to prevent repeated callback after an uncertain refund.
                    key = "refund_pending:" + order_id
                    if self.db.get(key):
                        return await message.answer("Возврат уже отправлялся. Проверьте состояние сделки.")
                    self.db.set(key, True)
                    try:
                        await self.engine.market.refund(order_id)
                    except ServiceError as exc:
                        if not exc.ambiguous:
                            self.db.set(key, False)
                        raise
                self.engine.accept(await self.engine.market.deal(order_id))
                return await self.order_card(message, order_id)
            if data.startswith("sent:"):
                row = self.engine.order(data[5:])
                if not row or row["stage"] != "delivered" or row["problem"]:
                    return await message.answer("Сначала нужна подтверждённая выдача без открытой проблемы.")
                self.engine.schedule(row["id"], "mark_sent", now())
                # Explicit action authorizes this job regardless of automatic setting.
                self.db.set("manual_mark:" + row["id"], True)
                return await message.answer("Отметка выполнения поставлена в очередь.")
            if data.startswith("retry:"):
                row = self.engine.order(data[6:])
                if not row or row["stage"] != "failed":
                    return await message.answer("Неопределённые операции сначала нужно сверить.")
                if row["module"] in {"stars", "smm"} and self.db.one(
                    "SELECT order_id FROM provider_orders WHERE order_id=?", (row["id"],)
                ):
                    return await message.answer(
                        "Откройте внешний заказ и сверку. Повторная закупка здесь заблокирована."
                    )
                stage = "preparing" if row["rental_uid"] else "rent_pending" if row["idem_key"] else "new"
                if row["module"] == "extend" and row["target_order_id"]:
                    stage = "extension_ready"
                self.engine.update(row["id"], stage=stage, error=None)
                return await message.answer("Операция возвращена в очередь с сохранёнными данными.")
            if data.startswith("reconcile:"):
                row = self.engine.order(data[10:])
                if not row or row["module"] != "extend" or row["stage"] != "review":
                    return
                await state.set_state(Form.value)
                await state.update_data(edit="extension_reconcile", order_id=row["id"])
                return await message.answer(
                    "Проверьте в KOSell, что именно это продление выполнено. "
                    "Пришлите фактическую стоимость списания в рублях. "
                    "Бот сверит новый срок и завершит выдачу, без повторного списания."
                )
            if data == "uncertain:list":
                rows = self.db.all(
                    "SELECT * FROM outbox WHERE channel='playerok' AND state IN ('uncertain','failed') "
                    "ORDER BY id DESC LIMIT 30"
                )
                return await message.answer(
                    "✉️ Доставка сообщений\n\n"
                    + (
                        "Playerok не вернул результат отправки. Откройте историю чата перед повтором."
                        if rows
                        else "✅ Все сообщения обработаны. Отправок без результата нет."
                    ),
                    reply_markup=keyboard(
                        [(f"#{r['id']} — {r['target'][:20]}", f"resend:{r['id']}") for r in rows]
                    ),
                )
            if data.startswith("resend:"):
                row_id = int(data[7:])
                return await message.answer(
                    "Повтор может продублировать сообщение. Товар останется тем же. "
                    "Вы проверили историю чата?",
                    reply_markup=keyboard(
                        [("Да, повторить те же данные", f"resend_yes:{row_id}"), ("Отмена", "uncertain:list")]
                    ),
                )
            if data.startswith("resend_yes:"):
                row_id = int(data[11:])
                self.db.execute(
                    "UPDATE outbox SET state='pending',error=NULL WHERE id=? AND channel='playerok' "
                    "AND state IN ('uncertain','failed')",
                    (row_id,),
                )
                self.db.audit("manual_resend", str(row_id))
                return await message.answer("Те же данные поставлены в очередь повторной отправки.")
            if data == "expenses:list":
                rows = self.db.all(
                    "SELECT * FROM listing_actions WHERE state='uncertain' ORDER BY created_at DESC"
                )
                return await message.answer(
                    "💳 Учёт списаний\n\n"
                    + (
                        "Playerok не вернул результат операции. Укажите фактическое списание из истории платежей."
                        if rows
                        else "✅ Все операции учтены. Списаний без результата нет."
                    ),
                    reply_markup=keyboard(
                        [
                            (f"{r['kind']} {r['item_id'][:12]} {money(r['price'])}", "expense:" + r["id"])
                            for r in rows
                        ]
                    ),
                )
            if data.startswith("expense:"):
                await state.set_state(Form.value)
                await state.update_data(edit="expense", action_id=data[8:])
                return await message.answer(
                    "После проверки пришлите «списано 12.50» с фактической суммой "
                    "или «не списано». Меняется только местный учёт."
                )
            if data == "analytics:items":
                return await self.catalog(message, state, module="", role="report", page=0)
            if data == "report:all" or data.startswith("reportitem:"):
                return await self.send_report(
                    message, item_id=data[11:] if data.startswith("reportitem:") else None
                )
            if data == "history:confirm":
                return await message.answer(
                    "Загрузить доступную историю сделок для аналитики? "
                    "Исторические заказы не запускают закупку или выдачу.",
                    reply_markup=keyboard([("Загрузить", "history:yes"), ("Отмена", "menu:analytics")]),
                )
            if data == "history:yes":
                return await self.import_history(message)
            if data == "backup":
                path = self.db.backup(self.data_dir / "backups")
                return await message.answer(f"Резервная копия создана: {path.name}. Хранится на сервере.")
            await message.answer("↩️ Этот экран уже обновился\n\nОткройте нужный раздел из главного меню.")
        except (ValueError, KeyError, TypeError, IndexError, ArithmeticError):
            await message.answer(
                "↩️ Откройте действие заново\n\nДанные изменились или формат значения не подходит. Выберите нужный раздел или нажмите «Назад»."
            )
        except TimeoutError:
            await message.answer(
                "⏱ Сервис отвечает дольше обычного\n\nПопробуйте позже. Если отправляли платный запрос, сначала откройте его результат в заказе."
            )
        except ServiceError as exc:
            await message.answer("⚠️ Операция остановлена\n\n" + issue_text(exc.code))

    async def analytics_menu(self, message):
        return await message.answer(
            self.analytics.text(),
            reply_markup=keyboard(
                [
                    ("📊 Графики и сравнение", "v6:stats:7"),
                    ("📊 Выгрузить Excel", "report:all"),
                    ("📦 Товары", "analytics:items"),
                    ("🗓 Фильтр периода", "edit:report_filter"),
                    ("⚙️ Настройки отчёта", "analytics:settings"),
                    ("Загрузить историю", "history:confirm"),
                    ("‹ Назад", "menu:home"),
                ]
            ),
        )

    async def report_settings(self, message):
        await message.answer(
            "⚙️ Настройки отчёта\n\nЕжедневный отчёт: "
            + ("🟢 включён" if self.db.get("daily_report") else "⚪ выключен")
            + "\n🕒 Время: "
            + self.db.get("report_time"),
            reply_markup=keyboard(
                [
                    (
                        "⏸ Выключить отчёт" if self.db.get("daily_report") else "▶️ Включить отчёт",
                        "toggle:daily_report",
                    ),
                    ("🕒 Время отправки", "edit:report_time"),
                    ("💰 Комиссия Playerok", "edit:fee_percent"),
                    ("‹ Назад", "menu:analytics"),
                ]
            ),
        )

    async def account_menu(self, message):
        return await message.answer(
            "⚙️ Управление магазином\n\nПодключения, состояние бота и обслуживание.",
            reply_markup=keyboard(
                [
                    ("🔌 Подключения", "connect:home"),
                    ("📋 Статус магазина", "connect:status"),
                    (
                        "▶️ Возобновить магазин" if self.db.get("paused") else "⏸ Приостановить магазин",
                        "toggle:paused",
                    ),
                    ("⬆️ Обновления", "updates:home"),
                    ("🗃 Резервная копия", "backup"),
                    ("‹ Назад", "menu:home"),
                ]
            ),
        )

    async def promotion_menu(self, message):
        return await message.answer(
            f"Платные действия: {'включены' if self.db.get('paid_enabled') else 'выключены'}\n"
            f"Одна операция: {money(self.db.get('action_limit'))}\n"
            f"Суточный бюджет: {money(self.db.get('daily_budget'))}",
            reply_markup=keyboard(
                [
                    ("Включить / выключить платные действия", "toggle:paid_enabled"),
                    ("Лимит операции", "edit:action_limit"),
                    ("Суточный бюджет", "edit:daily_budget"),
                    ("‹ Назад", "menu:auto"),
                ]
            ),
        )

    async def attention_menu(self, message):
        status = self.engine.diagnostic()
        affected = self.db.one(
            "SELECT count(*) n FROM orders WHERE stage IN ('review','failed') OR problem=1"
        )["n"]
        deliveries = self.db.one(
            "SELECT count(*) n FROM outbox WHERE channel='playerok' AND state IN ('uncertain','failed')"
        )["n"]
        total = affected + deliveries + status["provider_review"]
        expenses = self.db.one("SELECT count(*) n FROM listing_actions WHERE state='uncertain'")["n"]
        await message.answer(
            "🔔 Центр событий\n\n"
            + (
                "✅ Нет незавершённых обращений.\n\n"
                if not total and not expenses
                else "Выберите раздел, чтобы продолжить обработку.\n\n"
            )
            + f"🧾 Приостановлено заказов: {affected}\n✉️ Без ответа о доставке: {deliveries}\n⚡ Заказы поставщиков: {status['provider_review']}\n💳 Списания без результата: {expenses}",
            reply_markup=keyboard(
                [
                    ("🧾 Открыть обращения", "attention-orders:0"),
                    ("✉️ Доставка сообщений", "uncertain:list"),
                    ("💳 Учёт списаний", "expenses:list"),
                    ("⭐ Заказы Stars", "x:orders:stars:0"),
                    ("💜 Заказы SMM", "x:orders:smm:0"),
                    ("‹ Назад", "menu:profile"),
                ]
            ),
        )

    async def auto_menu(self, message, state):
        scope = (await state.get_data()).get("scope")
        await message.answer(
            "⭐ Автонастройки\n\nВыберите раздел настроек.\n🎯 Область: " + (scope or "весь магазин"),
            reply_markup=keyboard(
                [
                    ("💬 Сообщения покупателям", "auto:messages"),
                    ("🤖 Автоответчик", "auto:replies"),
                    ("🕒 Режим работы", "auto:schedule"),
                    ("🚀 Продвижение лотов", "promotion"),
                    ("🔔 Уведомления магазина", "alerts:home"),
                    ("🎯 Область настроек", "scopes"),
                    ("‹ Назад", "menu:home"),
                ]
            ),
        )

    async def automation_section(self, message, state, section):
        if section == "messages":
            await message.answer(
                "💬 Сообщения покупателям\n\nОткройте событие: внутри — включение и текст сообщения.",
                reply_markup=keyboard(
                    [(label, "auto:event:" + key) for key, label in LABELS.items()]
                    + [("‹ Назад", "menu:auto")]
                ),
            )
        elif section == "replies":
            await message.answer(
                "🤖 Автоответчик\n\nНастройте ответы на вопросы и условия отправки.",
                reply_markup=keyboard(
                    [
                        ("💬 Ответы по ключевым словам", "edit:rules"),
                        ("🧩 Сценарии общения", "v6:rules"),
                        ("⚡ Заготовки быстрых ответов", "s:quick:edit"),
                        ("‹ Назад", "menu:auto"),
                    ]
                ),
            )
        elif section == "schedule":
            await message.answer(
                f"🕒 Режим работы\n\nЧасы магазина: {self.db.get('work_start'):02d}:00–{self.db.get('work_end'):02d}:00\nРежим вне сети: {'включён' if self.db.get('offline_override') else 'выключен'}",
                reply_markup=keyboard(
                    [
                        ("🕒 Изменить часы", "edit:work_hours"),
                        (
                            "🟢 Вернуться в сеть"
                            if self.db.get("offline_override")
                            else "💤 Включить режим вне сети",
                            "toggle:offline_override",
                        ),
                        ("‹ Назад", "menu:auto"),
                    ]
                ),
            )

    async def event_settings(self, message, state, key):
        if key not in LABELS:
            raise ValueError()
        scope = (await state.get_data()).get("scope")
        enabled = self.db.feature(key, scope)
        text = LABELS[key] + "\n\nСостояние: " + ("🟢 включено" if enabled else "⚪ выключено")
        keys = [("⏸ Выключить" if enabled else "▶️ Включить", "feature:" + key)]
        if key != "auto_confirm":
            template = (
                self.db.get("override:" + (scope or ""), {})
                .get("templates", {})
                .get(key, self.db.get("templates").get(key, ""))
            )
            text += "\n\n📝 Текст сообщения:\n" + (template or "ещё не задан")
            keys.append(("✏️ Изменить текст", "text:" + key))
        else:
            text += "\n\nПосле успешной выдачи бот отмечает выполнение заказа на Playerok."
        await message.answer(text, reply_markup=keyboard(keys + [("‹ Назад", "auto:messages")]))

    async def profile(self, message):
        profile = self.db.get("profile", {})
        balance = self.db.get("kosell_balance", {})
        await message.answer(
            "👤 Профиль магазина\n\n"
            f"🎮 Playerok: {profile.get('username', 'не подключён')}\nID: {profile.get('id', '—')}\n\n"
            f"💰 Баланс Playerok: {profile.get('balance', {}).get('value', '—')} ₽\n"
            f"🎮 KOSell · RUB: {balance.get('balance_rub', '—')} · USD: {balance.get('balance_usd', '—')}\n\n"
            f"🕒 Проверено: {self.engine.expiry(self.db.get('last_profile'))}",
            reply_markup=keyboard(
                [
                    ("💬 Чаты и заказы", "s:inbox:new:0"),
                    ("🧾 Все заказы", "orders:0"),
                    ("👥 Мои покупатели", "v6:customers:0"),
                    ("🔔 Центр событий", "menu:attention"),
                    ("‹ Назад", "menu:home"),
                ]
            ),
        )

    async def bindings_menu(self, message, module=None, state=None):
        if state:
            await state.update_data(
                catalog={"module": module or "", "status": "all", "search": "", "page": 0, "role": "lot"}
            )
        return await self.catalog(message, state)

    async def lot(self, message, item_id):
        b = self.db.one("SELECT * FROM bindings WHERE item_id=?", (item_id,))
        if not b:
            return
        if b["fulfillment"] in {"stars", "smm"}:
            return await self.external_lot(message, item_id)
        count = self.db.one("SELECT count(*) n FROM stock WHERE item_id=? AND state='free'", (item_id,))["n"]
        rid = self.db.one("SELECT rowid rid FROM bindings WHERE item_id=?", (item_id,))["rid"]
        rows = [
            ("💰 Порог маржи", f"s:margin:{rid}"),
            ("👁 Предпросмотр выдачи", f"preview:lot:{item_id}"),
            ("📋 Сохранить шаблон", f"v6:tplsave:{rid}"),
            ("💰 Рассчитать цену", f"v6:price:{rid}"),
            ("▶ Снять ручную паузу" if b["paused"] else "⏸ Ручная пауза", f"lotflag:paused:{item_id}"),
            (
                "🔄 Автовыставление: " + ("🟢 вкл" if b["auto_restore"] else "⚪ выкл"),
                f"lotflag:auto_restore:{item_id}",
            ),
            (
                "🚀 Автоподнятие: " + ("🟢 вкл" if b["auto_bump"] else "⚪ выкл"),
                f"lotflag:auto_bump:{item_id}",
            ),
        ]
        for field, label in [
            ("max_cost", "Лимит закупки"),
            ("hours", "Длительность"),
            ("instruction", "Инструкция"),
            ("bump_interval", "Интервал поднятия"),
        ]:
            if field == "max_cost" and b["fulfillment"]:
                continue
            rows.append(("✏ " + label, f"lotedit:{field}:{item_id}"))
        if b["fulfillment"] == "own_rent":
            count = self.db.one(
                "SELECT count(*) n FROM own_accounts WHERE item_id=? AND state='free'", (item_id,)
            )["n"]
            rows.append(("🎮 Аккаунты пула", f"s:pool:{rid}:0"))
        elif b["fulfillment"] == "own_extend":
            rows.append(("🔗 Пул для продления", f"s:pool-target:{rid}:0"))
        elif b["module"] == "stock":
            rows.append(("📥 Загрузить склад", "stock:" + item_id))
        rows += [("Настройки сообщений этого лота", "scope:" + item_id), ("‹ Назад", "bindings:all")]
        await message.answer(
            f"{b['title']}\nID: {item_id}\nМодуль: {MODE_NAMES.get(b['fulfillment'] or b['module'], b['module'])}\n"
            + (
                f"Игра KOSell: {b['product_id']}\nЛимит закупки: {money(b['max_cost'])}\n"
                if b["module"] in {"kosell", "extend"}
                else ""
            )
            + f"Часов: {b['hours']}\nСвободных позиций: {count}\n"
            f"Продажа: {issue_text(b['hide_reason']) if b['hide_reason'] else 'без ограничений бота'}\n"
            + (
                "Ожидаемая маржа: "
                + money(self.db.get("margin:" + item_id, {}).get("margin"))
                + " (без расходов на продвижение).\nПроверено: "
                + self.engine.expiry(self.db.get("margin:" + item_id, {}).get("at"))
            ),
            reply_markup=keyboard(rows),
        )

    async def bind_start(self, message, state):
        await state.clear()
        await state.set_state(Form.binding)
        await state.update_data(step=0, binding={})
        await self.shop_lots(message, state, refresh=True)

    async def binding_input(self, message, state):
        data = await state.get_data()
        step, b = data["step"], data["binding"]
        next_step = step + 1
        text = (message.text or "").strip()
        try:
            if step == 0:
                b["item_id"] = (await self.resolve_own_lot(text))["id"]
                if self.db.one("SELECT item_id FROM bindings WHERE item_id=?", (b["item_id"],)):
                    return await message.answer("Лот уже привязан. Его настройки доступны в списке лотов.")
                prompt = "Выберите тип выдачи кнопкой: аренда, склад, Telegram Stars или SMM."
            elif step == 1:
                if text in {"stars", "smm"}:
                    await state.clear()
                    await self.external_prompt(
                        message,
                        state,
                        "new_title",
                        "Название лота для меню, до 150 символов.",
                        external_wizard={"mode": text, "item_id": b["item_id"]},
                    )
                    if data.get("catalog_title"):
                        return await self.external_value(
                            message, state, "x:new_title", data["catalog_title"], await state.get_data()
                        )
                    return
                if text not in {"kosell", "extend", "stock", "own_rent", "own_extend"}:
                    raise ValueError()
                b["fulfillment"] = text if text.startswith("own_") else ""
                b["module"] = "stock" if text.startswith("own_") else text
                prompt = "Название лота для меню."
            elif step == 2:
                if not 1 <= len(text) <= 150:
                    raise ValueError()
                b["title"] = text
                if b.get("fulfillment"):
                    b.update(product_id=None, max_cost=0)
                    next_step = 4
                    prompt = "Длительность аренды / продления в часах."
                elif b["module"] == "stock":
                    b.update(product_id=None, hours=1, max_cost=0)
                    next_step = 6
                    prompt = "Инструкция покупателю (до 1000 символов), либо - без инструкции."
                else:
                    prompt = "Выберите игру KOSell кнопкой из каталога. Если каталог временно недоступен, можно ввести её ID."
            elif step == 3:
                product = int(text)
                if product < 0 or (b["module"] != "stock" and product == 0):
                    raise ValueError()
                b["product_id"] = product or None
                prompt = "Длительность аренды / продления в часах (для склада укажите 1)."
            elif step == 4:
                b["hours"] = int(text)
                if not 1 <= b["hours"] <= 8760:
                    raise ValueError()
                if b.get("fulfillment"):
                    next_step = 6
                    prompt = "Инструкция покупателю (до 1000 символов), либо - без инструкции."
                else:
                    prompt = "Максимальная стоимость закупки, руб. Для своего склада: 0."
            elif step == 5:
                b["max_cost"] = rub(text.replace(",", "."))
                if b["max_cost"] < 0 or (b["module"] != "stock" and b["max_cost"] == 0):
                    raise ValueError()
                prompt = "Инструкция покупателю (до 1000 символов), либо - без инструкции."
            else:
                if len(text) > 1000:
                    raise ValueError()
                b["instruction"] = "" if text == "-" else text
                self.db.execute(
                    "INSERT INTO bindings(item_id,module,title,product_id,hours,max_cost,instruction,fulfillment) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    tuple(
                        b[k]
                        for k in (
                            "item_id",
                            "module",
                            "title",
                            "product_id",
                            "hours",
                            "max_cost",
                            "instruction",
                            "fulfillment",
                        )
                    ),
                )
                await state.clear()
                return await self.lot(message, b["item_id"])
            await state.update_data(step=next_step, binding=b)
            if step == 0 and data.get("binding_mode") in {"stars", "smm"}:
                return await self.binding_input(
                    message.model_copy(update={"text": data["binding_mode"]}).as_(self.bot), state
                )
            if step == 1 and data.get("catalog_title"):
                return await self.binding_input(
                    message.model_copy(update={"text": data["catalog_title"]}).as_(self.bot), state
                )
            if step == 2 and b["module"] in {"kosell", "extend"} and not b.get("fulfillment"):
                try:
                    return await self.kosell_choices(message, state)
                except (ServiceError, TimeoutError, AttributeError):
                    pass
            if step == 0:
                from .seller_ui import MODE_NAMES, buttons

                await message.answer(
                    prompt,
                    reply_markup=buttons(
                        [(label, "s:bindtype:" + mode) for mode, label in MODE_NAMES.items()]
                    ),
                )
            else:
                choices = []
                if step == 3:
                    limits = data.get("product_limits", {})
                    lo, hi = limits.get("min_hours") or 1, limits.get("max_hours") or 8760
                    choices = [
                        (str(h) + " ч.", f"v6:hours:{h}") for h in sorted({lo, 24, 72, 168}) if lo <= h <= hi
                    ]
                await message.answer(
                    prompt,
                    reply_markup=keyboard(choices + [("‹ Назад", "ui:back"), ("✖ Отменить", "ui:cancel")]),
                )
        except ServiceError as error:
            await message.answer(lot_error_text(error))
        except (ValueError, ArithmeticError):
            await message.answer("Не удалось принять значение. Проверьте формат и попробуйте снова.")

    async def value(self, message, state):
        context = await state.get_data()
        key = context["edit"]
        text = (message.text or "").strip()
        if key.startswith("x:"):
            return await self.external_value(message, state, key, text, context)
        if key.startswith("s:"):
            return await self.seller_value(message, state, key, text, context)
        try:
            if key == "template":
                if not 1 <= len(text) <= 2000:
                    raise ValueError()
                scope = context.get("scope")
                if scope:
                    value = self.db.get("override:" + scope, {})
                    value.setdefault("templates", {})[context["template"]] = text
                    self.db.set("override:" + scope, value)
                else:
                    value = self.db.get("templates")
                    value[context["template"]] = text
                    self.db.set("templates", value)
            elif key == "rules":
                rules = []
                for line in text.splitlines() if text != "-" else []:
                    phrase, reply, priority = line.split("|")
                    if not phrase.strip() or not reply.strip() or len(reply) > 2000:
                        raise ValueError()
                    rules.append(
                        {"phrase": phrase.strip(), "reply": reply.strip(), "priority": int(priority)}
                    )
                scope = context.get("scope")
                if scope:
                    value = self.db.get("override:" + scope, {})
                    value["rules"] = rules
                    self.db.set("override:" + scope, value)
                else:
                    self.db.set("rules", rules)
            elif key in {"stock_low", "rental_low"}:
                threshold = int(text)
                if not 0 <= threshold <= 10000:
                    raise ValueError()
                self.db.set(key, threshold)
            elif key == "work_hours":
                start, end = map(int, text.split("-"))
                if not 0 <= start < 24 or not 0 <= end <= 24 or start == end:
                    raise ValueError()
                self.db.set("work_start", start)
                self.db.set("work_end", end)
            elif key in {"action_limit", "daily_budget"}:
                amount = rub(text.replace(",", "."))
                if amount < 0:
                    raise ValueError()
                self.db.set(key, amount)
            elif key == "fee_percent":
                value = None if text == "-" else float(Decimal(text.replace(",", ".")))
                if value is not None and not 0 <= value <= 100:
                    raise ValueError()
                self.db.set(key, value)
            elif key == "report_time":
                if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
                    raise ValueError()
                self.db.set(key, text)
            elif key == "report_filter":
                filters = self.parse_filters(text)
                await state.clear()
                return await self.send_report(message, **filters)
            elif key == "lot":
                field = context["field"]
                value = (
                    text
                    if field == "instruction"
                    else rub(text.replace(",", "."))
                    if field == "max_cost"
                    else int(text)
                )
                if field == "instruction" and len(text) > 1000:
                    raise ValueError()
                if field == "max_cost" and value < 0:
                    raise ValueError()
                if field == "hours" and not 1 <= value <= 8760:
                    raise ValueError()
                if field == "bump_interval":
                    if value < 5:
                        raise ValueError()
                    value *= 60
                self.db.execute(f"UPDATE bindings SET {field}=? WHERE item_id=?", (value, context["item_id"]))
            elif key == "extension_reconcile":
                row = self.engine.order(context["order_id"])
                if row["stage"] != "review":
                    raise ValueError()
                cost = rub(text.replace(",", "."))
                if cost < 0:
                    raise ValueError()
                request = json.loads(row["request_json"])
                active = next(
                    (a for a in await self.engine.kosell.active() if a["rental_uid"] == request["uid"]), None
                )
                if (
                    not active
                    or timestamp(active["expires_at"]) < request["previous_expiry"] + request["hours"] * 3600
                ):
                    return await message.answer(
                        "Ожидаемый срок продления не подтверждён. Повторного списания не будет."
                    )
                expiry = timestamp(active["expires_at"])
                with self.db.transaction():
                    self.engine.update(row["target_order_id"], expires_at=expiry)
                    self.engine.update(row["id"], rental_uid=request["uid"], expires_at=expiry, error=None)
                    self.engine.queue_delivery_without_transaction(
                        row,
                        "Продление подтверждено продавцом. Новый срок: " + self.engine.expiry(expiry),
                        cost,
                    )
                    self.db.audit("extension_manually_reconciled", row["id"])
            elif key == "expense":
                action_id = context["action_id"]
                if not self.db.one(
                    "SELECT id FROM listing_actions WHERE id=? AND state='uncertain'", (action_id,)
                ):
                    raise ValueError()
                released = text.casefold() == "не списано"
                if not released and not text.casefold().startswith("списано "):
                    raise ValueError()
                cost = 0 if released else rub(text.split(maxsplit=1)[1].replace(",", "."))
                if cost < 0:
                    raise ValueError()
                with self.db.transaction():
                    self.db.execute(
                        "UPDATE expenses SET state=?,amount=? WHERE id=?",
                        ("released" if released else "paid", cost, action_id),
                    )
                    self.db.execute("UPDATE listing_actions SET state='reconciled' WHERE id=?", (action_id,))
                    self.db.audit("listing_manually_reconciled", action_id)
            else:
                raise ValueError()
            self.db.audit("settings_changed", key)
            await state.set_state(None)
            origin = view_route(context.get("_nav_origin", ""))
            if origin:
                return await self.navigate(message, origin, state)
            await message.answer("✅ Сохранено.")
        except (ValueError, KeyError, ArithmeticError):
            await message.answer("Некорректное значение. Попробуйте снова или нажмите «Отменить».")
        except ServiceError as exc:
            await message.answer(f"Сверка недоступна: {exc.code}. Данные не изменены.")

    async def stock_input(self, message, state):
        try:
            if message.document:
                if message.document.file_size and message.document.file_size > 2_000_000:
                    raise ValueError("Файл слишком большой: максимум 2 МБ")
                buffer = io.BytesIO()
                await self.bot.download(message.document, destination=buffer)
                if buffer.tell() > 2_000_000:
                    raise ValueError("Файл слишком большой")
                text = buffer.getvalue().decode("utf-8-sig")
            else:
                text = message.text or ""
            lines = text.splitlines()
            cost = None
            if lines and lines[0].startswith("cost="):
                cost = rub(lines.pop(0)[5:].replace(",", "."))
            context = await state.get_data()
            item_id = context["item_id"]
            warehouse = context.get("warehouse", "stock")
            if warehouse == "own":
                lines = [line for line in lines if line.strip()]
                Rentals(self.engine).add(item_id, lines, cost)
                count = len(lines)
            elif warehouse == "bonus":
                lines = [line for line in lines if line.strip()]
                Loyalty(self.engine).add(lines, cost)
                count = len(lines)
            else:
                count = self.db.add_stock(item_id, lines, cost)
            await state.clear()
            await message.answer(f"Загружено позиций: {count}. Данные хранятся в зашифрованном виде.")
        except (ValueError, UnicodeError, ArithmeticError):
            await message.answer("Проверьте UTF-8, число строк и себестоимость.")

    async def reply_command(self, message, state):
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 3 or not self.db.one("SELECT id FROM chats WHERE id=?", (parts[1],)):
            return await message.answer("Выберите чат в рабочем окне и нажмите «Ответить».")
        await state.update_data(chat_id=parts[1], reply_nonce=None, reply_text=None)
        await self.reply_preview(message, state, parts[2])

    async def reply_input(self, message, state):
        await self.reply_preview(message, state, message.text or "")

    async def reply_preview(self, message, state, text):
        if not 1 <= len(text) <= 3500:
            return await message.answer("Текст от 1 до 3500 символов.")
        nonce = uuid.uuid4().hex[:12]
        await state.update_data(reply_text=text, reply_nonce=nonce)
        await state.set_state(None)
        context = await state.get_data()
        await message.answer(
            f"Ответ в чат {context['chat_id']}:\n{text}",
            reply_markup=keyboard([("Отправить", "reply_send:" + nonce), ("Отмена", "menu:home")]),
        )

    async def orders(self, message, page, *, attention=False):
        where = " WHERE stage IN ('review','failed') OR problem=1" if attention else ""
        prefix = "attention-orders:" if attention else "orders:"
        count = self.db.one("SELECT count(*) n FROM orders" + where)["n"]
        page = min(max(0, page), max(0, (count - 1) // 10))
        rows = self.db.all(
            "SELECT * FROM orders" + where + " ORDER BY created_at DESC LIMIT 10 OFFSET ?", (page * 10,)
        )
        await message.answer(
            f"🧾 Заказы · {count}\n\n"
            + (
                f"Страница {page + 1}. Выберите заказ ↓"
                if rows
                else "Новые покупки появятся здесь автоматически."
            ),
            reply_markup=keyboard(
                [
                    (
                        f"{stage_label(r['stage'])} · {r['title'][:24]} · {money(r['price'])}",
                        "order:" + r["id"],
                    )
                    for r in rows
                ]
                + ([("← Назад", f"{prefix}{page - 1}")] if page else [])
                + ([("Далее →", f"{prefix}{page + 1}")] if (page + 1) * 10 < count else [])
                + [("‹ Назад", "menu:profile")]
            ),
        )

    async def order_card(self, message, order_id):
        row = self.engine.order(order_id)
        if not row:
            return await message.answer(
                "🧾 Заказ больше не доступен\n\nОткройте актуальный список заказов.",
                reply_markup=keyboard([("🧾 Заказы", "orders:0")]),
            )
        rows = [("✉ Ответить", "reply:" + order_id), ("▶ Возобновить автоответы", "resume:" + order_id)]
        if row["module"] in {"stars", "smm"} and self.db.one(
            "SELECT order_id FROM provider_orders WHERE order_id=?", (order_id,)
        ):
            rid = self.db.one("SELECT rowid rid FROM orders WHERE id=?", (order_id,))["rid"]
            rows.append(("Внешний заказ и сверка", f"x:order:{rid}"))
        if row["stage"] == "delivered":
            rows.append(("✅ Отметить выполнение", "sent:" + order_id))
        if row["market_status"] != "ROLLED_BACK":
            rows.append(("↩ Возврат", "refund:" + order_id))
        if row["stage"] == "failed" and (
            row["module"] not in {"stars", "smm"}
            or not self.db.one("SELECT order_id FROM provider_orders WHERE order_id=?", (order_id,))
        ):
            rows.append(("Повторить безопасную обработку", "retry:" + order_id))
        if row["stage"] == "review" and row["module"] == "extend":
            rows.append(("Сверить выполненное продление", "reconcile:" + order_id))
        markup = keyboard(rows + [("‹ Заказы", "orders:0")])
        markup.inline_keyboard.insert(
            0,
            [
                InlineKeyboardButton(
                    text="Открыть чат Playerok", url="https://playerok.com/chats/" + row["chat_id"]
                )
            ],
        )
        card = self.cards.order(order_id, "details")
        market_status = {
            "PAID": "Оплачен",
            "SENT": "Ожидает подтверждения покупателя",
            "CONFIRMED": "Получен покупателем",
            "CONFIRMED_AUTOMATICALLY": "Подтверждён автоматически",
            "ROLLED_BACK": "Средства возвращены",
        }.get(row["market_status"], row["market_status"])
        stage = {
            "new": "Ожидает обработки",
            "rent_pending": "Закупка аренды",
            "preparing": "Подготовка доступа",
            "sending": "Отправка покупателю",
            "delivered": "Выдан",
            "cancelled": "Отменён",
            "review": "⏸ Обработка приостановлена",
            "failed": "⚠️ Ошибка обработки",
            "historical": "Из истории",
            "ignored": "Нет настроенной выдачи",
            "wait_selection": "Ожидается выбор покупателя",
            "extending": "Продление аренды",
            "extension_ready": "Готов к продлению",
        }.get(row["stage"], row["stage"])
        text = (
            card.text
            + f"\n\n<b>Статус:</b> {safe_html(market_status)}\n<b>Выдача:</b> {safe_html(stage)}\n<b>Закупка:</b> {card_price(row['cost'])}"
        )
        if row["error"]:
            text += "\n\n<b>Следующий шаг:</b> " + safe_html(issue_text(row["error"]), 350)
        await message.answer(text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup)

    def parse_filters(self, text):
        parts = text.split()
        if len(parts) < 2:
            raise ValueError()
        start = datetime.strptime(parts[0], "%Y-%m-%d").replace(tzinfo=self.engine.tz)
        end = datetime.strptime(parts[1], "%Y-%m-%d").replace(tzinfo=self.engine.tz) + timedelta(days=1)
        if start >= end:
            raise ValueError()
        filters = {"start": start.timestamp(), "end": end.timestamp()}
        for part in parts[2:]:
            key, value = part.split("=", 1)
            if key == "module" and value in {
                "kosell",
                "stock",
                "extend",
                "own_rent",
                "own_extend",
                "stars",
                "smm",
            }:
                filters[key] = value
            elif key == "item":
                filters["item_id"] = str(uuid.UUID(value))
            else:
                raise ValueError()
        return filters

    async def report_command(self, message, state=None):
        args = (message.text or "").split(maxsplit=1)
        try:
            await self.send_report(message, **(self.parse_filters(args[1]) if len(args) > 1 else {}))
        except ValueError:
            await message.answer("Выберите период в разделе «Аналитика».")

    async def send_report(self, message, **filters):
        path = self.analytics.export(self.data_dir / "reports" / f"report-{uuid.uuid4().hex}.xlsx", **filters)
        await message.answer(self.analytics.text(**filters))
        await message.answer_document(
            BufferedInputFile(path.read_bytes(), filename="playerok-report.xlsx"),
            caption="📊 Отчёт магазина\n\nПродажи, расходы и результаты за выбранный период.",
        )

    async def import_history(self, message, state=None):
        await message.answer("Загружаю историю только для аналитики…")
        try:
            await self.engine.poll_orders(historical=True)
            await message.answer(
                "✅ История загружена. Для полного расчёта прибыли добавьте расходы прошлых закупок."
            )
        except ServiceError as exc:
            await message.answer(
                "⚠️ Загрузка остановлена\n\n" + issue_text(exc.code) + " Уже полученные записи сохранены."
            )

    async def help(self, message, state=None):
        await message.answer(
            "📞 Поддержка PlayerokHelper\n\n📖 Инструкции по подключению и работе — в документации.\n\n💬 Нашли ошибку или хотите предложить улучшение? Откройте обращение на GitHub. Для разбора можно приложить диагностику из раздела обновлений.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📖 Документация", url="https://github.com/renderr03/PlayerokHelper#readme"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="💬 Сообщить о проблеме",
                            url="https://github.com/renderr03/PlayerokHelper/issues",
                        )
                    ],
                    [InlineKeyboardButton(text="‹ Назад", callback_data="menu:home")],
                ]
            ),
        )

    async def send_admin(self, target, text, order_id=None, purpose="message"):
        token = panel_context.set(None)
        try:
            return await self._send_admin(target, text, order_id, purpose)
        finally:
            panel_context.reset(token)

    async def _send_admin(self, target, text, order_id, purpose):
        try:
            if purpose == "report":
                path = self.data_dir / "reports" / text
                if path.name != text or not path.is_file():
                    raise ServiceError("report_missing")
                result = await self.bot.send_document(
                    int(target),
                    BufferedInputFile(path.read_bytes(), filename=text),
                    caption=self.db.decrypt(self.db.get("report_caption:" + text))
                    if self.db.get("report_caption:" + text)
                    else "📊 Отчёт магазина",
                    parse_mode="HTML",
                    reply_markup=card_markup(
                        [[("📈 Аналитика", "card:analytics"), ("⚙️ Настройки отчёта", "card:reports")]]
                    ),
                )
            elif purpose == "card:message" or purpose.startswith("card:order:"):
                card = (
                    self.cards.message(text)
                    if purpose == "card:message"
                    else self.cards.order(order_id, purpose.rsplit(":", 1)[1])
                )
                if card:
                    result = await self.bot.send_message(
                        int(target),
                        card.text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                        reply_markup=card.keyboard,
                    )
                else:
                    result = await self.bot.send_message(
                        int(target), text[:4000], reply_markup=keyboard([("Рабочее окно", "s:inbox:new:0")])
                    )
            else:
                markup = (
                    keyboard([("Открыть заказ", "order:" + order_id)])
                    if order_id
                    else keyboard([("Открыть магазин", "menu:home")])
                )
                if purpose == "legacy_recovery":
                    markup = keyboard([("🛠 Открыть восстановление", "connect:visibility:recover")])
                    pending = self.db.get("visibility_test_pending") or {}
                    try:
                        item_id = str(uuid.UUID(pending["item_id"]))
                    except (ValueError, KeyError, TypeError):
                        pass
                    else:
                        markup.inline_keyboard.insert(
                            0,
                            [
                                InlineKeyboardButton(
                                    text="🌐 Открыть лот в Playerok",
                                    url="https://playerok.com/products/" + item_id,
                                )
                            ],
                        )
                if purpose == "update":
                    markup = keyboard([("⬆️ Обновить бота", "updates:home")])
                    release = self.updates.available()
                    if release:
                        markup.inline_keyboard.append(
                            [InlineKeyboardButton(text="📖 Что нового", url=release["url"])]
                        )
                result = await self.bot.send_message(int(target), text[:4000], reply_markup=markup)
            return str(result.message_id)
        except TelegramRetryAfter as exc:
            raise ServiceError("telegram_rate_limit", retry_after=exc.retry_after) from None
        except TelegramNetworkError:
            raise ServiceError("telegram_network", ambiguous=True) from None
        except TelegramBadRequest:
            raise ServiceError("telegram_request_rejected") from None


def dispatcher(ui: TelegramUI):
    dp = Dispatcher(events_isolation=SimpleEventIsolation())
    dp.include_router(ui.router)
    return dp
