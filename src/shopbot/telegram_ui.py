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
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import __version__
from .analytics import Analytics
from .checks import binding_fingerprint, connection_snapshot, needs_kosell, preview_delivery
from .engine import Engine
from .models import ServiceError, money, now, rub, timestamp
from .updates import Updates, read_json

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
            text = "Обновление и проверка запуска: обработка заказов и настройки временно приостановлены."
            result = read_json(self.maintenance.parent / "result.json", {})
            if isinstance(result, dict) and result.get("status") == "manual_required":
                text = "Автоматическое восстановление не завершено. Нужна проверка сервера администратором."
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            else:
                await event.answer(text)
            return None
        return await handler(event, data)


class TelegramUI:
    def __init__(self, bot: Bot, engine: Engine, admins, data_dir, project_dir=None):
        self.bot, self.engine, self.db = bot, engine, engine.db
        self.data_dir = data_dir
        self.analytics = Analytics(self.db)
        self.updates = Updates(self.db, project_dir)
        self.router = Router()
        maintenance = self.updates.folder / "maintenance.json" if self.updates.folder else None
        self.router.message.outer_middleware(AdminOnly(admins, maintenance))
        self.router.callback_query.outer_middleware(AdminOnly(admins, maintenance))
        self.register()

    def register(self):
        r = self.router
        r.message.register(self.start, CommandStart())
        r.message.register(self.cancel, Command("cancel"))
        r.message.register(self.start, Command("menu"))
        r.message.register(self.setup_menu, Command("setup"))
        r.message.register(self.updates_menu, Command("version"))
        r.message.register(self.status, Command("status"))
        r.message.register(self.bind_start, Command("bind"))
        r.message.register(self.report_command, Command("report"))
        r.message.register(self.reply_command, Command("reply"))
        r.message.register(self.import_history, Command("import"))
        r.message.register(self.value, StateFilter(Form.value))
        r.message.register(self.binding_input, StateFilter(Form.binding))
        r.message.register(self.stock_input, StateFilter(Form.stock))
        r.message.register(self.reply_input, StateFilter(Form.reply))
        r.callback_query.register(self.callback)
        r.message.register(self.help)

    async def start(self, message: Message, state: FSMContext):
        await state.clear()
        self.db.set("update_offer", None)
        status = (
            "⏸ пауза"
            if self.db.get("paused")
            else "✅ работает"
            if self.db.get("live_enabled") and now() - self.db.get("last_poll", 0) < 90
            else "⚪ нет успешной сверки"
        )
        await message.answer(
            f"PlayerokHelper {__version__}\nДобро пожаловать! Выберите действие.\n"
            f"Автоматизация: {status}\n"
            f"Готовность подключения: {self.db.get('readiness_status', 'не проверена')}",
            reply_markup=keyboard(
                [
                    ("🧙 Настроить магазин", "setup:home"),
                    ("⬆️ Версия и обновления", "updates:home"),
                    ("⭐ Автонастройки", "menu:auto"),
                    ("👤 Профиль", "menu:profile"),
                    ("📈 Аналитика", "menu:analytics"),
                    ("⚡ Плагины", "menu:plugins"),
                    ("📍 Управление аккаунтом", "menu:account"),
                    ("📞 Помощь", "menu:help"),
                ]
            ),
        )

    async def cancel(self, message, state):
        await state.clear()
        self.db.set("update_offer", None)
        await message.answer("Ввод отменён. /menu")

    async def status(self, message, state=None):
        status = self.engine.diagnostic()
        await message.answer(
            f"Подключение: {self.db.get('readiness_status', 'не проверено')}\n"
            f"Пауза: {'да' if status['paused'] else 'нет'}\nЗаказов: {status['orders']}\n"
            f"Требуют проверки: {status['review']}\nНеопределённые отправки: {status['uncertain']}\n"
            f"Последняя успешная сверка: {self.engine.expiry(self.db.get('last_poll'))}"
        )

    async def setup_menu(self, message, state):
        await state.clear()
        snapshot = await connection_snapshot(
            self.db, self.engine.market, self.engine.kosell, check_kosell=needs_kosell(self.db)
        )
        bindings = self.db.all("SELECT * FROM bindings ORDER BY rowid")
        source = self.db.get("setup_source") or (
            "kosell" if any(b["module"] != "stock" for b in bindings) else "stock" if bindings else None
        )
        playerok_ok = snapshot["services"]["playerok"]["ok"]
        kosell_ok = snapshot["services"].get("kosell", {}).get("ok", False)
        visibility_ok = playerok_ok and not any(i.startswith("visibility_") for i in snapshot["issues"])
        last = self.db.get("last_preview", {})
        checked = next((b for b in bindings if b["item_id"] == last.get("item_id")), None)
        preview_ok = (
            checked
            and last.get("ok")
            and now() - last.get("at", 0) < 60
            and binding_fingerprint(checked) == last.get("binding")
        )

        def mark(ok):
            return "✅" if ok else "⬜"

        lines = [
            f"🧙 Мастер первого запуска · PlayerokHelper {__version__}",
            f"{mark(playerok_ok)} 1. Подключить Playerok",
            f"{mark(source and (source == 'stock' or kosell_ok))} 2. Источник: "
            + ({"stock": "свой склад", "kosell": "KOSell"}.get(source, "выберите ниже")),
            f"{mark(visibility_ok)} 3. Проверить скрытие и восстановление",
            f"{mark(bindings)} 4. Привязать первый лот",
            f"{mark(preview_ok)} 5. Проверить сценарий в карточке лота (актуальность 60 секунд)",
            "\nНа сервере, в каталоге проекта:",
            ".venv/bin/playerokhelper setup --connections",
            "sudo systemctl restart playerokhelper",
            "Ключи и сессия вводятся только в консоли. Не присылайте их в чат.",
            "\nДля живого теста выберите свой активный тестовый лот и выполните на сервере:",
            "sudo systemctl stop playerokhelper",
            ".venv/bin/playerokhelper verify-visibility --item UUID_ЛОТА --confirm-reversible-test",
            "sudo systemctl start playerokhelper",
            "После теста снова откройте /setup. От root команды выполняются без sudo. "
            "Для Docker и ручного запуска — инструкция в репозитории.",
        ]
        if snapshot["issues"]:
            lines.append("\nТребует внимания: " + ", ".join(snapshot["issues"]))
        rows = [
            ("📦 Использовать свой склад", "setup:source:stock"),
            ("🎮 Использовать KOSell", "setup:source:kosell"),
            ("🔄 Проверить подключение", "setup:home"),
            ("➕ Привязать лот", "bind:new"),
        ]
        rows += [
            ("🧪 " + (b["title"][:30] or b["item_id"]), "preview:lot:" + b["item_id"]) for b in bindings[:10]
        ]
        rows += [("🚨 Предупреждения", "alerts:home"), ("‹ В меню", "menu:home")]
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
                    ("‹ Назад", "menu:account"),
                ]
            ),
        )

    async def preview_menu(self, message, item_id):
        binding = self.db.one("SELECT rowid AS rid,* FROM bindings WHERE item_id=?", (item_id,))
        if not binding:
            return await message.answer("Лот не найден.")
        if binding["module"] != "extend":
            return await self.show_preview(message, item_id)
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
            f"⬆️ PlayerokHelper {__version__}",
            "Проверка новых стабильных версий — раз в 6 часов.",
            "Последняя проверка: " + self.engine.expiry(self.db.get("update_check_at")),
        ]
        if self.db.get("update_check_error"):
            lines.append(self.db.get("update_check_error"))
        rows = [("🔄 Проверить сейчас", "updates:check")]
        if release:
            lines += ["\nДоступна " + release["tag"], release["body"][:1000], release["url"]]
            if self.updates.enabled() and not self.updates.busy():
                rows.append(("⬆️ Обновить бота", "updates:offer"))
        else:
            lines.append(
                "Новая версия пока не найдена. Нажмите «Проверить сейчас» для актуального результата."
            )
        if not self.updates.enabled():
            lines.append(
                "Обновление кнопкой требует однократной установки v0.3.0+ со службой systemd. "
                "Для Docker и ручного запуска обновляйте установку обычным способом."
            )
        if self.updates.busy():
            lines.append("Обновление уже поставлено в очередь или выполняется.")
        for item in self.db.get("update_history", [])[:5]:
            label = {
                "success": "установлено",
                "rolled_back": "выполнен откат",
                "failed": "не установлено",
                "manual_required": "нужна проверка сервера",
            }.get(item.get("status"), "неизвестно")
            lines.append(f"\n{self.engine.expiry(item.get('finished_at'))}: {item.get('tag')} — {label}")
            reason = {
                "modified_project": "В проекте есть локальные изменения; они сохранены.",
                "main_branch_required": "Для обновления требуется ветка main.",
                "release_tag_changed": "Коммит релиза изменился; нужна повторная проверка версии.",
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
            (
                "🔔 Уведомления: " + ("вкл" if self.db.get("update_notifications") else "выкл"),
                "updates:toggle",
            ),
            ("‹ В меню", "menu:home"),
        ]
        await message.answer("\n".join(lines)[:4000], reply_markup=keyboard(rows))

    async def callback(self, query: CallbackQuery, state: FSMContext):
        await query.answer()
        message = query.message
        data = query.data or ""
        await state.set_state(None)
        try:
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
                    f"Установить {offer['tag']}?\nСначала подготовим версию, затем остановим бот и "
                    "создадим копию базы. При ошибке запуска вернём прежнюю версию и базу. "
                    "Во время установки заказы и настройки временно недоступны.\n"
                    "Предложение действует 10 минут.",
                    reply_markup=keyboard(
                        [
                            ("Установить " + offer["tag"], "updates:install:" + offer["nonce"]),
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
                    "Обновление поставлено в очередь. Подготовка может занять несколько минут. "
                    "После запуска придёт сообщение с результатом."
                )
            if data == "menu:home":
                return await self.start(message, state)
            if data == "setup:home":
                return await self.setup_menu(message, state)
            if data.startswith("setup:source:"):
                source = data.rsplit(":", 1)[1]
                if source not in {"stock", "kosell"}:
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
                return await message.answer("Введите целый порог от 0 до 10000. /cancel — отмена.")
            if data.startswith("preview:lot:"):
                return await self.preview_menu(message, data.split(":", 2)[2])
            if data.startswith("preview:extend:"):
                _, _, binding_row, rental_row = data.split(":")
                binding = self.db.one("SELECT * FROM bindings WHERE rowid=?", (int(binding_row),))
                rental = self.db.one("SELECT id FROM orders WHERE rowid=?", (int(rental_row),))
                if binding and binding["module"] == "extend" and rental:
                    return await self.show_preview(message, binding["item_id"], rental["id"])
                return await message.answer("Лот или аренда больше не доступны. Откройте карточку лота.")
            if data == "menu:auto":
                return await self.auto_menu(message, state)
            if data == "menu:profile":
                return await self.profile(message)
            if data == "menu:plugins":
                return await message.answer(
                    "Плагины",
                    reply_markup=keyboard(
                        [
                            ("🎮 Аренда Steam — KOSell", "plugin:kosell"),
                            ("📦 Собственный склад", "plugin:stock"),
                            ("‹ Назад", "menu:home"),
                        ]
                    ),
                )
            if data.startswith("plugin:"):
                module = data.split(":")[1]
                return await self.bindings_menu(message, module)
            if data == "menu:analytics":
                return await message.answer(
                    self.analytics.text(),
                    reply_markup=keyboard(
                        [
                            ("📊 Выгрузить Excel", "report:all"),
                            ("📦 Товары", "analytics:items"),
                            ("🗓 Фильтр периода", "edit:report_filter"),
                            (
                                "✅ Ежедневный отчёт"
                                if self.db.get("daily_report")
                                else "⬜ Ежедневный отчёт",
                                "toggle:daily_report",
                            ),
                            ("🕒 Время ежедневного отчёта", "edit:report_time"),
                            ("💰 Комиссия Playerok", "edit:fee_percent"),
                            ("Загрузить историю", "history:confirm"),
                            ("‹ Назад", "menu:home"),
                        ]
                    ),
                )
            if data == "menu:account":
                return await message.answer(
                    "Управление магазином",
                    reply_markup=keyboard(
                        [
                            ("📦 Привязанные лоты", "bindings:all"),
                            ("🧾 Последние заказы", "orders:0"),
                            ("⚠️ Неопределённые отправки", "uncertain:list"),
                            ("💳 Неопределённые списания", "expenses:list"),
                            (
                                "▶ Возобновить" if self.db.get("paused") else "⏸ Пауза и скрытие лотов",
                                "toggle:paused",
                            ),
                            ("🗃 Создать резервную копию", "backup"),
                            ("🚨 Ранние предупреждения", "alerts:home"),
                            ("‹ Назад", "menu:home"),
                        ]
                    ),
                )
            if data == "menu:help":
                return await self.help(message)
            if data.startswith("toggle:"):
                key = data.split(":")[1]
                if key not in {"daily_report", "paused", "paid_enabled", "offline_override"}:
                    return
                self.db.set(key, not self.db.get(key))
                return await message.answer("Сохранено. /menu")
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
                return await self.auto_menu(message, state)
            if data.startswith("templates"):
                return await message.answer(
                    "Выберите текст",
                    reply_markup=keyboard(
                        [(label, "text:" + key) for key, label in LABELS.items() if key != "auto_confirm"]
                        + [("‹ Назад", "menu:auto")]
                    ),
                )
            if data.startswith("text:"):
                key = data.split(":")[1]
                if key not in LABELS or key == "auto_confirm":
                    return
                await state.set_state(Form.value)
                await state.update_data(edit="template", template=key)
                return await message.answer(
                    "Пришлите новый текст (до 2000 символов). Переменные: "
                    "{buyer}, {order}, {title}, {expires}. /cancel — отмена."
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
                return await message.answer(
                    "Настройки магазина или отдельного лота",
                    reply_markup=keyboard(
                        [("Весь магазин", "scope:global")]
                        + [
                            (b["title"][:35] or b["item_id"], "scope:" + b["item_id"])
                            for b in self.db.all("SELECT * FROM bindings LIMIT 40")
                        ]
                    ),
                )
            if data.startswith("edit:"):
                key = data.split(":")[1]
                prompts = {
                    "rules": "Пришлите правила: одна строка «фраза | ответ | приоритет». Пустой список: -",
                    "work_hours": "Рабочие часы по Москве: например 9-23. Для круглосуточной работы: 0-24.",
                    "action_limit": "Максимальная стоимость одной операции в рублях (0 запрещает платные действия).",
                    "daily_budget": "Общий суточный бюджет продвижения в рублях. День считается по часовому поясу магазина.",
                    "fee_percent": "Комиссия Playerok в процентах: например 10. Значение - означает неизвестную комиссию.",
                    "report_time": "Время ежедневного отчёта: ЧЧ:ММ, часовой пояс магазина.",
                    "report_filter": "Период: ГГГГ-ММ-ДД ГГГГ-ММ-ДД (обе даты включительно). "
                    "Необязательно добавьте item=ID или module=kosell/stock/extend.",
                }
                if key not in prompts:
                    return
                await state.set_state(Form.value)
                await state.update_data(edit=key)
                return await message.answer(prompts[key] + "\n/cancel — отмена")
            if data == "promotion":
                return await message.answer(
                    f"Платные действия: {'включены' if self.db.get('paid_enabled') else 'выключены'}\n"
                    f"Одна операция: {money(self.db.get('action_limit'))}\n"
                    f"Суточный бюджет: {money(self.db.get('daily_budget'))}",
                    reply_markup=keyboard(
                        [
                            ("Включить / выключить платные действия", "toggle:paid_enabled"),
                            ("Лимит операции", "edit:action_limit"),
                            ("Суточный бюджет", "edit:daily_budget"),
                            ("Настроить лоты", "bindings:all"),
                            ("‹ Назад", "menu:auto"),
                        ]
                    ),
                )
            if data == "bindings:all":
                return await self.bindings_menu(message)
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
                return await message.answer(names[field] + ". /cancel — отмена")
            if data.startswith("stock:"):
                item_id = data[6:]
                binding = self.db.one("SELECT * FROM bindings WHERE item_id=? AND module='stock'", (item_id,))
                if not binding:
                    return
                await state.set_state(Form.stock)
                await state.update_data(item_id=item_id)
                return await message.answer(
                    "Пришлите UTF-8 TXT или текст: одна строка — один товар. "
                    "Для общей себестоимости добавьте первой строкой «cost=12.50». "
                    "Без неё себестоимость будет неизвестной. /cancel — отмена"
                )
            if data.startswith("orders:"):
                return await self.orders(message, max(0, int(data.split(":")[1])))
            if data.startswith("order:"):
                return await self.order_card(message, data[6:])
            if data.startswith("reply:"):
                order = self.engine.order(data[6:])
                if not order:
                    return
                await state.set_state(Form.reply)
                await state.update_data(chat_id=order["chat_id"])
                return await message.answer(f"Ответ в чат {order['chat_id']}. Пришлите текст. /cancel")
            if data.startswith("reply_send:"):
                context = await state.get_data()
                if "reply_text" not in context or data.split(":", 1)[1] != context.get("reply_nonce"):
                    return await message.answer("Предпросмотр устарел. Подготовьте ответ заново.")
                self.db.enqueue(
                    "playerok",
                    context["chat_id"],
                    context["reply_text"],
                    "operator_reply:" + str(uuid.uuid4()),
                )
                self.db.execute("UPDATE chats SET last_seller_at=? WHERE id=?", (now(), context["chat_id"]))
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
                if self.db.get("readiness_status") != "готово":
                    return await message.answer("Подключение не прошло проверку готовности.")
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
                    "Бот сверит новый срок и завершит выдачу, без повторного списания. /cancel"
                )
            if data == "uncertain:list":
                rows = self.db.all(
                    "SELECT * FROM outbox WHERE channel='playerok' AND state IN ('uncertain','failed') "
                    "ORDER BY id DESC LIMIT 30"
                )
                return await message.answer(
                    "Отправки на проверке. Повтор отправляет те же данные; сначала проверьте историю чата.",
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
                    "Сначала проверьте списание в Playerok.",
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
                    "или «не списано». Меняется только местный учёт. /cancel"
                )
            if data == "analytics:items":
                return await message.answer(
                    "Выберите товар для отчёта",
                    reply_markup=keyboard(
                        [
                            (b["title"][:40] or b["item_id"], "reportitem:" + b["item_id"])
                            for b in self.db.all("SELECT * FROM bindings LIMIT 40")
                        ]
                    ),
                )
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
        except (ValueError, KeyError, ArithmeticError):
            await message.answer("Некорректное значение. Проверьте ввод или используйте /cancel.")
        except ServiceError as exc:
            await message.answer(
                f"Сервис не выполнил операцию: {exc.code}. Проверьте состояние перед повтором."
            )

    async def auto_menu(self, message, state):
        scope = (await state.get_data()).get("scope")
        rows = [
            (("✅ " if self.db.feature(key, scope) else "⬜ ") + label, "feature:" + key)
            for key, label in LABELS.items()
        ]
        rows += [
            ("🚨 Ранние предупреждения", "alerts:home"),
            ("📦 Автовыдача", "menu:plugins"),
            ("✏ Тексты сообщений", "templates"),
            ("🤖 Правила автоответчика", "edit:rules"),
            ("🚀 Поднятие и выставление", "promotion"),
            ("🕒 Рабочие часы", "edit:work_hours"),
            ("💤 Ручной режим вне сети", "toggle:offline_override"),
            ("🎯 Область настроек", "scopes"),
            ("‹ Назад", "menu:home"),
        ]
        await message.answer(
            "⭐ Автонастройки\nОбласть: " + (scope or "весь магазин"), reply_markup=keyboard(rows)
        )

    async def profile(self, message):
        profile = self.db.get("profile", {})
        balance = self.db.get("kosell_balance", {})
        await message.answer(
            f"Playerok: {profile.get('username', 'не подключён')}\nID: {profile.get('id', '—')}\n"
            f"Баланс Playerok: {profile.get('balance', {}).get('value', '—')} ₽\n"
            f"KOSell RUB: {balance.get('balance_rub', '—')}\nKOSell USD: {balance.get('balance_usd', '—')}\n"
            f"Проверено: {self.engine.expiry(self.db.get('last_profile'))}",
            reply_markup=keyboard([("Статус подключения", "menu:account"), ("‹ Назад", "menu:home")]),
        )

    async def bindings_menu(self, message, module=None):
        rows = self.db.all(
            "SELECT * FROM bindings" + (" WHERE module=?" if module else ""), (module,) if module else ()
        )
        await message.answer(
            "Привязанные лоты. Сначала создайте лот с ручной выдачей на Playerok.",
            reply_markup=keyboard(
                [
                    (
                        ("⏸ " if b["paused"] else "📦 ") + (b["title"][:35] or b["item_id"]),
                        "lot:" + b["item_id"],
                    )
                    for b in rows[:40]
                ]
                + [("➕ Привязать лот", "bind:new"), ("‹ Назад", "menu:home")]
            ),
        )

    async def lot(self, message, item_id):
        b = self.db.one("SELECT * FROM bindings WHERE item_id=?", (item_id,))
        if not b:
            return
        count = self.db.one("SELECT count(*) n FROM stock WHERE item_id=? AND state='free'", (item_id,))["n"]
        rows = [
            ("🧪 Проверить сценарий", f"preview:lot:{item_id}"),
            ("▶ Снять ручную паузу" if b["paused"] else "⏸ Ручная пауза", f"lotflag:paused:{item_id}"),
            ("Автовыставление: " + str(bool(b["auto_restore"])), f"lotflag:auto_restore:{item_id}"),
            ("Автоподнятие: " + str(bool(b["auto_bump"])), f"lotflag:auto_bump:{item_id}"),
        ]
        for field, label in [
            ("max_cost", "Лимит закупки"),
            ("hours", "Длительность"),
            ("instruction", "Инструкция"),
            ("bump_interval", "Интервал поднятия"),
        ]:
            rows.append(("✏ " + label, f"lotedit:{field}:{item_id}"))
        if b["module"] == "stock":
            rows.append(("📥 Загрузить склад", "stock:" + item_id))
        rows += [("Настройки сообщений этого лота", "scope:" + item_id), ("‹ Назад", "bindings:all")]
        await message.answer(
            f"{b['title']}\nID: {item_id}\nМодуль: {b['module']}\n"
            f"Игра KOSell: {b['product_id']}\nЧасов: {b['hours']}\n"
            f"Лимит: {money(b['max_cost'])}\nСвободных позиций: {count}\n"
            f"Причина скрытия: {b['hide_reason'] or '—'}",
            reply_markup=keyboard(rows),
        )

    async def bind_start(self, message, state):
        await state.clear()
        await state.set_state(Form.binding)
        await state.update_data(step=0, binding={})
        await message.answer("Пришлите UUID готового лота Playerok. Ссылка не подходит. /cancel — отмена")

    async def binding_input(self, message, state):
        data = await state.get_data()
        step, b = data["step"], data["binding"]
        text = (message.text or "").strip()
        try:
            if step == 0:
                b["item_id"] = str(uuid.UUID(text))
                if self.db.one("SELECT item_id FROM bindings WHERE item_id=?", (b["item_id"],)):
                    return await message.answer("Лот уже привязан. Его настройки доступны в списке лотов.")
                prompt = "Тип выдачи: kosell — аренда, extend — продление, stock — ваш склад."
            elif step == 1:
                if text not in {"kosell", "extend", "stock"}:
                    raise ValueError()
                b["module"] = text
                prompt = "Название лота для меню."
            elif step == 2:
                if not 1 <= len(text) <= 150:
                    raise ValueError()
                b["title"] = text
                prompt = "ID игры KOSell (целое число). Для своего склада: 0."
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
                    "INSERT INTO bindings(item_id,module,title,product_id,hours,max_cost,instruction) "
                    "VALUES (?,?,?,?,?,?,?)",
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
                        )
                    ),
                )
                await state.clear()
                return await self.lot(message, b["item_id"])
            await state.update_data(step=step + 1, binding=b)
            await message.answer(prompt)
        except (ValueError, ArithmeticError):
            await message.answer("Не удалось принять значение. Проверьте формат и попробуйте снова. /cancel")

    async def value(self, message, state):
        context = await state.get_data()
        key = context["edit"]
        text = (message.text or "").strip()
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
            await message.answer("Сохранено. /menu")
        except (ValueError, KeyError, ArithmeticError):
            await message.answer("Некорректное значение. Попробуйте снова или /cancel.")
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
            item_id = (await state.get_data())["item_id"]
            count = self.db.add_stock(item_id, lines, cost)
            await state.clear()
            await message.answer(f"Загружено позиций: {count}. Данные хранятся в зашифрованном виде.")
        except (ValueError, UnicodeError, ArithmeticError):
            await message.answer("Проверьте UTF-8, число строк и себестоимость. /cancel")

    async def reply_command(self, message, state):
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 3 or not self.db.one("SELECT id FROM chats WHERE id=?", (parts[1],)):
            return await message.answer("/reply ID_ЧАТА текст. Чат должен быть уже известен боту.")
        await state.update_data(chat_id=parts[1])
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

    async def orders(self, message, page):
        rows = self.db.all("SELECT * FROM orders ORDER BY created_at DESC LIMIT 10 OFFSET ?", (page * 10,))
        await message.answer(
            "Заказы",
            reply_markup=keyboard(
                [
                    (f"{r['stage']} · {r['title'][:24]} · {money(r['price'])}", "order:" + r["id"])
                    for r in rows
                ]
                + [("Далее", f"orders:{page + 1}"), ("‹ В меню", "menu:account")]
            ),
        )

    async def order_card(self, message, order_id):
        row = self.engine.order(order_id)
        if not row:
            return
        rows = [("✉ Ответить", "reply:" + order_id), ("▶ Возобновить автоответы", "resume:" + order_id)]
        if row["stage"] == "delivered":
            rows.append(("✅ Отметить выполнение", "sent:" + order_id))
        if row["market_status"] != "ROLLED_BACK":
            rows.append(("↩ Возврат", "refund:" + order_id))
        if row["stage"] == "failed":
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
        events = self.db.all(
            "SELECT event FROM audit WHERE reference=? ORDER BY created_at DESC LIMIT 5", (order_id,)
        )
        await message.answer(
            f"Заказ {order_id}\n{row['title']}\nПокупатель: {row['buyer_name']}\n"
            f"Статус Playerok: {row['market_status']}\nВыдача: {row['stage']}\n"
            f"Цена: {money(row['price'])}\nЗакупка: {money(row['cost'])}\n"
            f"Ошибка: {row['error'] or '—'}\n"
            f"Последние действия: {', '.join(e['event'] for e in events) or '—'}",
            reply_markup=markup,
        )

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
            if key == "module" and value in {"kosell", "stock", "extend"}:
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
            await message.answer("/report ГГГГ-ММ-ДД ГГГГ-ММ-ДД [item=UUID] [module=kosell|stock|extend]")

    async def send_report(self, message, **filters):
        path = self.analytics.export(self.data_dir / "reports" / f"report-{uuid.uuid4().hex}.xlsx", **filters)
        await message.answer(self.analytics.text(**filters))
        await message.answer_document(BufferedInputFile(path.read_bytes(), filename="playerok-report.xlsx"))

    async def import_history(self, message, state=None):
        await message.answer("Загружаю историю только для аналитики…")
        try:
            await self.engine.poll_orders(historical=True)
            await message.answer("История загружена. Себестоимость прошлых закупок может быть неизвестна.")
        except ServiceError as exc:
            await message.answer(f"Загрузка прервана: {exc.code}. Уже полученные записи сохранены.")

    async def help(self, message, state=None):
        await message.answer(
            "/setup — мастер подключения и первого лота\n"
            "/version — версия, проверка обновлений и журнал\n"
            "Настройка: /bind — готовый лот; затем загрузите склад или укажите игру KOSell.\n"
            "/status — подключение\n/report — Excel\n/reply ID_ЧАТА текст — ответ с предпросмотром\n"
            "/cancel — отмена формы\n/menu — главное меню\n\n"
            "Покупатель: !помощь, !аренды, !код <номер>, !срок <номер>, !продлить <номер>, "
            "!инструкция, !оператор.\n\n"
            "Пауза останавливает закупки и выдачу, связанные лоты снимаются с продажи. "
            "Неопределённые списания и отправки требуют сверки в управлении аккаунтом."
        )

    async def send_admin(self, target, text, order_id=None, purpose="message"):
        try:
            if purpose == "report":
                path = self.data_dir / "reports" / text
                if path.name != text or not path.is_file():
                    raise ServiceError("report_missing")
                result = await self.bot.send_document(
                    int(target), BufferedInputFile(path.read_bytes(), filename=text)
                )
            else:
                markup = keyboard([("Открыть заказ", "order:" + order_id)]) if order_id else None
                if purpose == "update":
                    markup = keyboard([("⬆️ Обновить бота", "updates:home")])
                result = await self.bot.send_message(int(target), text[:4000], reply_markup=markup)
            return str(result.message_id)
        except TelegramRetryAfter as exc:
            raise ServiceError("telegram_rate_limit", retry_after=exc.retry_after) from None
        except TelegramNetworkError:
            raise ServiceError("telegram_network", ambiguous=True) from None
        except TelegramBadRequest:
            raise ServiceError("telegram_request_rejected") from None


def dispatcher(ui: TelegramUI):
    dp = Dispatcher()
    dp.include_router(ui.router)
    return dp
