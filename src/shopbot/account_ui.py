"""Button-driven connection wizard. Secrets are accepted only in admin private chats."""

from __future__ import annotations

import asyncio
import json
import re
import uuid

from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton

from . import __version__
from .access import probe_access, probe_text
from .checks import connected_service, connection_snapshot, needs_kosell, save_connections
from .connections import EmailLogin, ManagedClient, session_input
from .external import save_provider
from .item_reference import item_reference
from .kosell import KOSell
from .models import ServiceError, now, rub
from .navigation import buttons
from .playerok import Playerok
from .providers import PRESETS
from .wallets import balance_text

CHECK_TIMEOUT = 15

DOCS = (
    "https://github.com/renderr03/PlayerokHelper/blob/"
    + ("beta/v0.5.0" if "b" in __version__ else "main")
    + "/docs/CONNECTIONS.md"
)


class ConnectionForm(StatesGroup):
    token = State()
    cookie = State()
    user_agent = State()
    email = State()
    code = State()
    key = State()
    visibility = State()


def controls(back="connect:home"):
    return buttons([("‹ Назад", back), ("✖ Отменить", "ui:cancel")])


def connection_error_text(error, *, playerok=True):
    """Show only known failure categories, never arbitrary server text or keys."""
    if not playerok:
        if error.code == "kosell_not_configured":
            return "Сначала добавьте API-ключ KOSell через кнопку «Заменить ключ»."
        if error.code == "kosell_http_401":
            return "KOSell не принял API-ключ. Проверьте его в кабинете поставщика."
        if error.code == "kosell_rate_limit":
            return "KOSell ограничил частоту запросов. Подождите и повторите проверку."
        return "Не удалось подключиться к поставщику. Откройте его кабинет и убедитесь, что ключ активен."
    messages = {
        "playerok_auth_required": "Сессия Playerok больше не действует. Войдите на сайте и получите новый токен.",
        "playerok_http_401": "Сессия Playerok больше не действует (HTTP 401). Войдите на сайте и получите новый токен.",
        "playerok_security_check": "Сервер получил страницу проверки или защиты Playerok (HTTP 403). Подключиться к аккаунту пока не удалось. Откройте проверку доступа ниже и инструкцию. Автоматическое прохождение этой страницы не выполняется.",
        "playerok_http_403": "Playerok отклонил запрос с сервера (HTTP 403). Это не доказывает, что токен введён неверно. Проверьте доступ к сайту с сервера; может потребоваться полный Cookie и User-Agent вашей сессии.",
        "playerok_access_denied": "Playerok запретил доступ к профилю этой сессии. Проверьте доступ к аккаунту на сайте.",
        "playerok_query_rejected": "Playerok отклонил формат запроса профиля. Это ошибка совместимости API, а не доказательство неверного токена. Проверьте обновление бота.",
        "playerok_graphql_error": "API Playerok не вернул профиль. Причина не определена; токен пока не сохранён. Проверьте обновление бота и повторите попытку.",
        "playerok_invalid_response": "Вместо ответа API Playerok получен другой формат. Проверьте доступ к площадке с сервера; токен пока не сохранён.",
        "playerok_network": "Сервер бота не смог связаться с Playerok. Проверьте сеть и доступ к площадке с сервера, затем повторите попытку.",
        "playerok_invalid_profile": "Playerok вернул неполные данные профиля. Подключение не сохранено; проверьте доступ к аккаунту на сайте.",
    }
    if error.code == "playerok_rate_limit":
        return (
            "Playerok ограничил частоту запросов. Повторите через "
            + str(max(1, min(3600, error.retry_after or 30)))
            + " с."
        )
    if error.code in messages:
        return messages[error.code]
    match = re.fullmatch(r"playerok_http_([45][0-9]{2})", error.code)
    if match:
        return "Playerok вернул HTTP " + match[1] + ". Подключение сейчас недоступно. Попробуйте позже."
    if error.code.startswith("playerok_login_"):
        return "Не удалось войти по почте. Введите актуальный код. Если сайт требует проверку браузера или второй фактор, войдите на сайте и используйте токен."
    return "Не удалось подключиться к Playerok. Войдите в аккаунт на сайте и повторите попытку."


def lot_error_text(error):
    messages = {
        "playerok_item_not_found": "Товар по этой ссылке не найден. Проверьте ссылку или выберите другой свой лот.",
        "playerok_item_invalid": "Playerok вернул неполные данные товара. Попробуйте другую ссылку или повторите позже.",
        "lot_not_owned": "Этот товар принадлежит другому продавцу. Пришлите ссылку на свой лот.",
        "lot_not_active": "Этот товар сейчас не продаётся. Для проверки выберите свой активный опубликованный лот.",
    }
    return messages.get(error.code, "Не удалось получить товар. " + connection_error_text(error))


class AccountUI:
    def init_connections(self):
        self.logins = {}
        self.connection_lock = asyncio.Lock()
        self.connection_config = None
        self.playerok_factory = Playerok
        self.access_probe = probe_access
        self.login_factory = EmailLogin
        self.kosell_factory = KOSell

    async def expire_logins(self, chat_id=None):
        for key, login in list(self.logins.items()):
            if key == chat_id or login["expires"] < now():
                del self.logins[key]
                await login["client"].close()

    async def expire_logins_all(self):
        for chat_id in list(self.logins):
            await self.expire_logins(chat_id)

    async def connection_menu(self, message, state):
        await state.clear()
        await self.expire_logins(message.chat.id)
        snapshot = await self.refresh_connections()
        profile = self.db.get("profile") or {}
        name = profile.get("username") or "не добавлен"
        supplier = snapshot["services"].get("kosell", {})
        supplier_status = (
            "✅ подключён"
            if supplier.get("ok")
            else "нет связи"
            if supplier.get("configured")
            else "не добавлен"
        )
        await message.answer(
            f"🔌 Подключения магазина\n\n🎮 Playerok: {name}\n🎮 KOSell: {supplier_status}\n\n"
            "Доступ проверяется автоматически. Для своего склада поставщик не нужен.",
            reply_markup=buttons(
                [
                    (
                        "➕ Добавить аккаунт Playerok" if not profile else "🔑 Переподключить Playerok",
                        "connect:playerok",
                    ),
                    ("🎮 Подключить KOSell", "connect:key:kosell"),
                    ("⭐ Подключить Split", "connect:key:split"),
                    ("Подключить SMM-панель", "connect:smm"),
                    ("⚙️ Настроить товары", "setup:home"),
                    ("‹ Главное меню", "menu:home"),
                ]
            ),
        )

    async def connection_callback(self, message, data, state):
        if not data.startswith("connect:"):
            return False
        if message.chat.type != "private":
            await message.answer("Подключайте аккаунты в личном чате с ботом.")
            return True
        if data == "connect:home":
            await self.connection_menu(message, state)
        elif data == "connect:check:kosell":
            await self.check_kosell(message)
        elif data == "connect:visibility:recover":
            if not self.db.get("visibility_test_pending"):
                await message.answer(
                    "✅ Незавершённых тестов нет. Подключения проверяются автоматически.",
                    reply_markup=self.visibility_controls(),
                )
                return True
            await message.answer("⏳ Проверяю восстановление лота…")
            try:
                async with self.managed("market").exclusive() as market:
                    async with asyncio.timeout(30):
                        await self.restore_visibility(market, retry=True)
                text = "✅ Лот снова в продаже. Новый тест скрытия не запускался."
            except (ServiceError, TimeoutError, ValueError, KeyError, TypeError) as error:
                self.record_visibility_error(error)
                text = self.visibility_failure_text()
            await message.answer(text, reply_markup=self.visibility_controls())
        elif data == "connect:playerok":
            await state.clear()
            await self.expire_logins(message.chat.id)
            await message.answer(
                "Как добавить аккаунт Playerok?",
                reply_markup=buttons(
                    [
                        ("✉️ По почте", "connect:email"),
                        ("🔑 По токену", "connect:token"),
                        ("🌐 Полная сессия браузера", "connect:session"),
                        ("🔎 Проверить доступ сервера", "connect:probe"),
                        ("‹ Назад", "connect:home"),
                    ]
                ),
            )
        elif data == "connect:email":
            await state.clear()
            await self.expire_logins(message.chat.id)
            await state.set_state(ConnectionForm.email)
            await message.answer(
                "Отправьте email аккаунта Playerok. На него придёт код входа.\n"
                "Пароль не нужен. Код и сессия дают доступ к магазину; вводите их только в этом личном чате.",
                reply_markup=controls("connect:playerok"),
            )
        elif data == "connect:token":
            await state.clear()
            await self.expire_logins(message.chat.id)
            await state.set_state(ConnectionForm.token)
            markup = controls("connect:playerok")
            markup.inline_keyboard.insert(0, [InlineKeyboardButton(text="Как получить токен", url=DOCS)])
            await message.answer(
                "Отправьте токен аккаунта Playerok. Можно также отправить Cookie "
                "или JSON с cookie и user_agent из браузера.\n"
                "Сообщение с токеном будет удалено по возможности, сессия сохранится зашифрованно на сервере.",
                reply_markup=markup,
            )
        elif data == "connect:session":
            await state.clear()
            await self.expire_logins(message.chat.id)
            await state.set_state(ConnectionForm.cookie)
            markup = controls("connect:playerok")
            markup.inline_keyboard.insert(
                0,
                [
                    InlineKeyboardButton(
                        text="Где взять Cookie и User-Agent", url=DOCS + "#полная-сессия-браузера"
                    )
                ],
            )
            await message.answer(
                "Полная сессия · шаг 1 из 2\n\nПришлите значение заголовка Cookie из запроса к playerok.com после обычного входа. На следующем шаге нужен User-Agent того же браузера.\nЭто даёт доступ к вашему магазину. Используйте только личный чат этого бота; сообщение удаляется по возможности.",
                reply_markup=markup,
            )
        elif data == "connect:probe":
            await state.clear()
            await self.expire_logins(message.chat.id)
            async with self.connection_lock:
                snapshot = self.db.get("connection_probe") or {}
                if (
                    not snapshot
                    or snapshot.get("transport") != "curl_cffi"
                    or now() - snapshot.get("at", 0) >= 60
                ):
                    await message.answer("Проверяю доступ к сайту и API без авторизации…")
                    checks = await self.access_probe()
                    snapshot = {"at": now(), "checks": checks, "transport": "curl_cffi"}
                    self.db.set("connection_probe", snapshot)
                await message.answer(
                    probe_text(snapshot["checks"])
                    + "\n\nПроверено: "
                    + self.engine.expiry(snapshot["at"])
                    + "\nПовторная проверка — не чаще раза в минуту.",
                    reply_markup=buttons(
                        [
                            ("🔄 Проверить ещё раз", "connect:probe"),
                            ("🌐 Ввести полную сессию", "connect:session"),
                            ("‹ Способы подключения", "connect:playerok"),
                        ]
                    ),
                )
        elif data == "connect:smm":
            await state.clear()
            await message.answer(
                "Выберите SMM-панель:",
                reply_markup=buttons(
                    [
                        (PRESETS[p][2], "connect:key:" + p)
                        for p in ("vingboost", "twiboost", "vexboost", "jap")
                    ]
                    + [("‹ Назад", "connect:home")]
                ),
            )
        elif data.startswith("connect:key:"):
            preset = data.split(":")[2]
            if preset not in {*PRESETS, "kosell"}:
                raise ValueError("Поставщик больше не доступен")
            await state.clear()
            await state.set_state(ConnectionForm.key)
            await state.update_data(preset=preset)
            await message.answer(
                "Отправьте API-ключ "
                + ("KOSell" if preset == "kosell" else PRESETS[preset][2])
                + ". Его можно получить в личном кабинете поставщика.\n"
                "Не отправляйте пароль или seed-фразу. Сообщение с ключом будет удалено по возможности.",
                reply_markup=controls(),
            )
        elif (
            data == "connect:visibility"
            or data == "connect:visibility:run"
            or data.startswith("connect:visibility:run:")
        ):
            await state.clear()
            await message.answer(
                "✨ Тест скрытия больше не нужен\n\nДоступ проверяется автоматически при подключении. "
                "Настройка не снимает товары с продажи.",
                reply_markup=buttons(
                    [("⚙️ Настроить товары", "setup:home"), ("🏠 Главное меню", "menu:home")]
                ),
            )
        else:
            raise ValueError("Экран подключения больше не доступен")
        return True

    async def connection_input(self, message, state):
        if message.chat.type != "private":
            return await message.answer("Подключайте аккаунты в личном чате с ботом.")
        form = await state.get_state()
        text = (message.text or "").strip()
        context = await state.get_data()
        # Never echo credentials, even after a failed validation or deletion.
        if form in {
            ConnectionForm.token.state,
            ConnectionForm.cookie.state,
            ConnectionForm.user_agent.state,
            ConnectionForm.email.state,
            ConnectionForm.code.state,
            ConnectionForm.key.state,
        }:
            try:
                await message.delete()
            except TelegramAPIError:
                pass
        async with self.connection_lock:
            try:
                await self.expire_logins()
                if form == ConnectionForm.email.state:
                    if not re.fullmatch(r"[^\s@]{1,64}@[^\s@.]+(?:\.[^\s@.]+)+", text) or len(text) > 254:
                        raise ValueError("Проверьте адрес почты.")
                    retry = self.db.get("login_retry:" + str(message.chat.id), 0)
                    if retry > now():
                        raise ValueError("Подождите минуту перед повторным запросом кода.")
                    self.db.set("login_retry:" + str(message.chat.id), now() + 60)
                    await self.expire_logins(message.chat.id)
                    login = self.login_factory()
                    try:
                        await login.send(text)
                    except BaseException:
                        await login.close()
                        raise
                    self.logins[message.chat.id] = {
                        "client": login,
                        "email": self.db.encrypt(text),
                        "expires": now() + 600,
                        "attempts": 0,
                    }
                    await state.set_state(ConnectionForm.code)
                    await message.answer(
                        "Код запрошен. Отправьте 6 цифр из письма Playerok. Проверьте также папку «Спам».",
                        reply_markup=controls("connect:email"),
                    )
                elif form == ConnectionForm.code.state:
                    login = self.logins.get(message.chat.id)
                    if not login:
                        await state.clear()
                        return await message.answer(
                            "Время ввода кода истекло. Запросите новый код.",
                            reply_markup=buttons(
                                [("Запросить код", "connect:email"), ("Отменить", "ui:cancel")]
                            ),
                        )
                    if not re.fullmatch(r"\d{6}", text):
                        raise ValueError("Отправьте 6 цифр из письма.")
                    login["attempts"] += 1
                    try:
                        session = await login["client"].confirm(self.db.decrypt(login["email"]), text)
                        await self.apply_playerok(session)
                    finally:
                        if login["attempts"] >= 5:
                            await self.expire_logins(message.chat.id)
                    await self.expire_logins(message.chat.id)
                    await state.clear()
                    await self.connected(message)
                elif form == ConnectionForm.token.state:
                    await self.apply_playerok(session_input(text))
                    await state.clear()
                    await self.connected(message)
                elif form == ConnectionForm.cookie.state:
                    session = session_input(text)
                    await state.update_data(
                        connection_cookie=self.db.encrypt(session["cookie"]), connection_expires=now() + 600
                    )
                    await state.set_state(ConnectionForm.user_agent)
                    await message.answer(
                        "Полная сессия · шаг 2 из 2\n\nПришлите User-Agent из того же запроса браузера. Заголовок начинается с User-Agent, его значение часто начинается с Mozilla/.\nСессия может зависеть от сети/IP: Cookie с телефона не гарантирует работу на VPS.",
                        reply_markup=controls("connect:session"),
                    )
                elif form == ConnectionForm.user_agent.state:
                    if context.get("connection_expires", 0) < now() or not context.get("connection_cookie"):
                        await state.clear()
                        return await message.answer(
                            "Время ввода сессии истекло. Начните заново.",
                            reply_markup=buttons(
                                [("Ввести сессию", "connect:session"), ("Отменить", "ui:cancel")]
                            ),
                        )
                    agent = re.sub(r"(?i)^user-agent\s*:\s*", "", text)
                    session = session_input(
                        json.dumps(
                            {"cookie": self.db.decrypt(context["connection_cookie"]), "user_agent": agent}
                        )
                    )
                    await self.apply_playerok(session)
                    await state.clear()
                    await self.connected(message)
                elif form == ConnectionForm.key.state:
                    preset = context["preset"]
                    if preset == "kosell":
                        await message.answer("⏳ Проверяю API-ключ KOSell…")
                        await self.apply_kosell(text)
                    else:
                        save_provider(self.db, preset, text)
                    await state.clear()
                    if preset == "kosell":
                        balance = self.db.get("kosell_balance") or {}
                        await message.answer(
                            "✅ KOSell подключён\n\n💰 Кошельки:\n"
                            + balance_text(balance)
                            + "\n\nПроверка выполнена автоматически. Выберите лот и товар для выдачи.",
                            reply_markup=buttons(
                                [
                                    ("📦 Настроить первый лот", "bind:new"),
                                    ("💱 Выбрать кошелёк", "v6:wallet"),
                                    ("⚙️ Мой магазин", "setup:home"),
                                    ("🏠 Главное меню", "menu:home"),
                                ]
                            ),
                        )
                    else:
                        provider_id = self.db.one("SELECT id FROM providers WHERE preset=?", (preset,))["id"]
                        summary = "Ключ сохранён. Выберите товар и настройте поставщика."
                        if preset in PRESETS:
                            await message.answer("⏳ Подключаю " + PRESETS[preset][2] + "…")
                            try:
                                async with asyncio.timeout(CHECK_TIMEOUT):
                                    balances, catalog = await self.engine.external.check(provider_id)
                                summary = (
                                    "✅ "
                                    + PRESETS[preset][2]
                                    + " подключён\n\n💰 Баланс: "
                                    + ", ".join(f"{v} {k}" for k, v in balances.items())
                                    + (
                                        "\n⭐ Telegram Stars"
                                        if preset == "split"
                                        else f"\nПоддерживаемых услуг: {len(catalog)}."
                                    )
                                    + "\n\nНастройте лот, лимиты и включите поставщика. Закупки пока выключены."
                                )
                            except (
                                ServiceError,
                                TimeoutError,
                                KeyError,
                                ValueError,
                                TypeError,
                                ArithmeticError,
                            ):
                                summary = (
                                    "⚠️ Ключ "
                                    + PRESETS[preset][2]
                                    + " сохранён. Подключиться сейчас не удалось. Откройте кабинет поставщика: ключ должен быть активен. Закупки выключены; повторная загрузка доступна в настройках поставщика."
                                )
                        await message.answer(
                            summary,
                            reply_markup=buttons(
                                [
                                    ("⚙️ Настроить поставщика", "x:provider:" + str(provider_id)),
                                    ("‹ Подключения", "connect:home"),
                                ]
                            ),
                        )
                elif form == ConnectionForm.visibility.state:
                    await state.clear()
                    await message.answer(
                        "Тест скрытия больше не нужен. Подключение проверяется автоматически.",
                        reply_markup=buttons([("⚙️ Настроить товары", "setup:home")]),
                    )
            except ServiceError as exc:
                if form == ConnectionForm.visibility.state:
                    return await message.answer(lot_error_text(exc), reply_markup=controls("setup:home"))
                if form == ConnectionForm.code.state and message.chat.id not in self.logins:
                    await state.clear()
                    return await message.answer(
                        "Попытки ввода кода закончились. Запросите новый код.",
                        reply_markup=buttons(
                            [
                                ("Запросить код", "connect:email"),
                                ("По токену", "connect:token"),
                                ("Отменить", "ui:cancel"),
                            ]
                        ),
                    )
                markup = controls("connect:playerok" if form != ConnectionForm.key.state else "connect:home")
                if form != ConnectionForm.key.state:
                    markup.inline_keyboard[0:0] = [
                        [
                            InlineKeyboardButton(
                                text="🔎 Проверить доступ сервера", callback_data="connect:probe"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="🌐 Полная сессия браузера", callback_data="connect:session"
                            )
                        ],
                        [InlineKeyboardButton(text="Инструкция по подключению", url=DOCS)],
                    ]
                error_text = connection_error_text(exc, playerok=form != ConnectionForm.key.state)
                if form == ConnectionForm.user_agent.state and exc.code in {
                    "playerok_http_403",
                    "playerok_security_check",
                }:
                    error_text = "Проверка введённой сессии тоже завершилась HTTP 403. Cookie и User-Agent могут не подходить этой сети, либо доступ ограничивает площадка. Нажмите «Проверить доступ сервера»; повторный ввод того же токена пока не даст новой информации."
                await message.answer(error_text, reply_markup=markup)
            except TimeoutError:
                await message.answer(
                    "⏱ Сервис не ответил вовремя. Подключение не сохранено. Попробуйте позже.",
                    reply_markup=controls(),
                )
            except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                reason = {
                    "Подождите минуту перед повторным запросом кода.": "Подождите минуту перед повторным запросом кода.",
                    "another_store": "В этой базе уже есть данные другого магазина. Переподключите прежний аккаунт Playerok.",
                    "unsettled_orders": "Ключ KOSell пока нельзя заменить: есть незавершённые заказы, действующие аренды или спорные списания. Сначала завершите их сверку.",
                    "Сначала завершите сверку текущих заказов этого поставщика": "У поставщика есть незавершённые заказы. Сначала завершите их сверку.",
                }.get(str(exc))
                fallback = {
                    ConnectionForm.cookie.state: "Cookie не принят. Нужен заголовок Cookie только от playerok.com с непустым token.",
                    ConnectionForm.user_agent.state: "User-Agent не принят. Скопируйте его значение из того же запроса браузера; другие заголовки добавлять не нужно.",
                    ConnectionForm.email.state: "Проверьте адрес почты: например name@example.com.",
                    ConnectionForm.code.state: "Отправьте шесть цифр из письма Playerok.",
                    ConnectionForm.token.state: "Токен не распознан. Пришлите его значение, Cookie или JSON с cookie и user_agent.",
                    ConnectionForm.key.state: "Ключ не принят. Скопируйте API-ключ из кабинета поставщика без пробелов.",
                    ConnectionForm.visibility.state: "Ссылка не распознана. Нужна ссылка на товар вида https://playerok.com/products/... или полный ID товара. Ссылка на профиль не подходит. Попробуйте ещё раз.",
                }
                await message.answer(
                    reason or fallback.get(form, "Проверьте данные и повторите ввод."),
                    reply_markup=controls(
                        "setup:home"
                        if form == ConnectionForm.visibility.state
                        else "connect:playerok"
                        if form != ConnectionForm.key.state
                        else "connect:home"
                    ),
                )

    async def connected(self, message):
        profile = self.db.get("profile") or {}
        has_kosell = bool(self.db.get("kosell_token") or getattr(self.engine.kosell, "configured", False))
        rows = []
        if not has_kosell:
            rows += [
                ("🎮 Подключить KOSell", "connect:key:kosell"),
                ("📦 Использовать свой склад", "setup:source:stock"),
            ]
        else:
            rows.append(("📦 Настроить товары", "setup:home"))
        rows.append(("🏠 Главное меню", "menu:home"))
        await message.answer(
            "✅ Playerok подключён\n\n👤 "
            + str(profile.get("username") or profile["id"])
            + "\n\nАккаунт подключён. "
            + ("Настройте товары для выдачи." if has_kosell else "Добавьте KOSell или выберите свой склад."),
            reply_markup=buttons(rows),
        )

    async def refresh_connections(self):
        snapshot = self.db.get("connection_snapshot")
        if not isinstance(snapshot, dict) or now() - snapshot.get("at", 0) >= 60:
            snapshot = await connection_snapshot(
                self.db,
                self.engine.market,
                self.engine.kosell,
                check_kosell=needs_kosell(self.db) or bool(self.db.get("kosell_token")),
            )
            save_connections(self.db, snapshot)
        return snapshot

    def managed(self, name):
        client = getattr(self.engine, name)
        if not isinstance(client, ManagedClient):
            client = ManagedClient(client)
            setattr(self.engine, name, client)
        return client

    async def apply_playerok(self, session):
        candidate = self.playerok_factory(
            session, self.connection_config.visibility_file if self.connection_config else None
        )
        saved = False
        try:
            async with asyncio.timeout(CHECK_TIMEOUT):
                profile = await candidate.profile()
            if not profile.get("id") or not profile.get("username") or rub(profile["balance"]["value"]) < 0:
                raise ServiceError("playerok_invalid_profile")
            proxy = self.managed("market")
            async with proxy.exclusive():
                old_id = (self.db.get("profile") or {}).get("id") or proxy.user_id
                has_store = self.db.one("SELECT id FROM orders LIMIT 1") or self.db.one(
                    "SELECT item_id FROM bindings LIMIT 1"
                )
                pending = self.db.get("visibility_test_pending") or {}
                if (old_id and old_id != profile["id"] and has_store) or pending.get(
                    "account_id", profile["id"]
                ) != profile["id"]:
                    raise ValueError("another_store")
                with self.db.transaction():
                    self.db.set("playerok_session", self.db.encrypt(json.dumps(session)))
                    self.db.set("profile", {k: profile[k] for k in ("id", "username", "balance")})
                    self.db.set("last_profile", now())
                    self.db.set("last_readiness", 0)
                    connected_service(self.db, "playerok", profile)
                    if old_id != profile["id"]:
                        self.db.set("visibility_proof", None)
                    self.db.audit("playerok_connected", str(profile["id"]))
                proxy.replace(candidate)
                saved = True
        finally:
            if not saved:
                await candidate.close()

    def kosell_in_use(self):
        return bool(
            self.db.one(
                "SELECT id FROM orders WHERE historical=0 AND module IN ('kosell','extend') "
                "AND (stage NOT IN ('delivered','refunded','failed','cancelled') "
                "OR (stage='delivered' AND expires_at>? AND market_status!='ROLLED_BACK')) LIMIT 1",
                (now(),),
            )
            or self.db.one("SELECT id FROM expenses WHERE state='uncertain' LIMIT 1")
        )

    async def apply_kosell(self, token):
        if not 8 <= len(token) <= 4000 or any(c.isspace() for c in token):
            raise ValueError()
        proxy = self.managed("kosell")
        # Drain requests and guard financial intents both before and after the
        # asynchronous balance check: an order may arrive while it is pending.
        async with self.engine._purchase_lock:
            async with proxy.exclusive():
                if self.kosell_in_use():
                    raise ValueError("unsettled_orders")
                client = self.kosell_factory(
                    self.connection_config.kosell_url
                    if self.connection_config
                    else "https://ru.kosell.store",
                    token,
                )
                saved = False
                try:
                    async with asyncio.timeout(CHECK_TIMEOUT):
                        balance = await client.balance()
                    balance_text(balance)
                    if rub(balance["balance_rub"]) < 0:
                        raise ServiceError("kosell_invalid_balance")
                    if self.kosell_in_use():
                        raise ValueError("unsettled_orders")
                    with self.db.transaction():
                        self.db.set("kosell_token", self.db.encrypt(token))
                        connected_service(self.db, "kosell", balance)
                        self.db.set("kosell_last_check", {"ok": True, "at": now(), "reason": "ok"})
                        if not self.db.get("setup_source"):
                            self.db.set("setup_source", "kosell")
                        self.db.set("last_readiness", 0)
                        self.db.audit("kosell_credentials_changed", "")
                    proxy.replace(client)
                    saved = True
                finally:
                    if not saved:
                        await client.close()

    async def resolve_own_lot(self, value, *, active=False):
        kind, reference = item_reference(value)
        proxy = self.managed("market")
        # Pin the client until both reads finish, so reconnecting cannot mix
        # the profile of one account with a listing from another session.
        async with proxy.request():
            market = proxy.current
            await market.profile()
            item = await (market.item(reference) if kind == "id" else market.item_by_slug(reference))
            if not isinstance(item, dict):
                raise ServiceError("playerok_item_not_found")
            try:
                item_id = str(uuid.UUID(item["id"]))
            except (ValueError, KeyError, TypeError, AttributeError):
                raise ServiceError("playerok_item_invalid") from None
            if kind == "id" and item_id != reference:
                raise ServiceError("playerok_item_invalid")
            owner = item.get("user")
            if not isinstance(owner, dict) or not owner.get("id"):
                raise ServiceError("playerok_item_invalid")
            if owner["id"] != market.user_id:
                raise ServiceError("lot_not_owned")
            if active and item.get("status") != "APPROVED":
                raise ServiceError("lot_not_active")
            return {**item, "id": item_id}

    async def check_kosell(self, message):
        await message.answer("⏳ Проверяю подключение KOSell…")
        result = {"at": now(), "ok": False, "reason": "unavailable"}
        try:
            client = self.managed("kosell")
            if not getattr(client, "configured", True):
                raise ServiceError("kosell_not_configured")
            async with asyncio.timeout(CHECK_TIMEOUT):
                balance = await client.balance()
            amount = rub(balance["balance_rub"])
            if amount < 0:
                raise ValueError()
            result.update(ok=True, reason="ok")
            text = "✅ KOSell подключён\n\n💰 Кошельки:\n" + balance_text(balance)
        except TimeoutError:
            result["reason"] = "timeout"
            text = "⏱ KOSell не ответил за 15 секунд. Попробуйте позже. Сохранённый ключ не изменён."
        except ServiceError as error:
            result["reason"] = {
                "kosell_http_401": "auth",
                "kosell_not_configured": "not_configured",
            }.get(error.code, "unavailable")
            text = "⚠️ " + connection_error_text(error, playerok=False)
        except (ValueError, KeyError, TypeError, ArithmeticError):
            result["reason"] = "invalid_response"
            text = "⚠️ KOSell вернул неполные данные баланса. Попробуйте позже."
        self.db.set("kosell_last_check", result)
        await message.answer(
            text,
            reply_markup=buttons(
                [
                    ("🔄 Проверить снова", "connect:check:kosell"),
                    ("🔑 Заменить ключ", "connect:key:kosell"),
                    ("‹ Подключения", "connect:home"),
                    ("⚙️ Продолжить настройку", "setup:home"),
                ]
            ),
        )

    def record_visibility_error(self, error):
        code = (
            error.code
            if isinstance(error, ServiceError)
            else "timeout"
            if isinstance(error, TimeoutError)
            else "invalid_response"
        )
        known = {
            "timeout",
            "invalid_response",
            "visibility_not_verified",
            "visibility_restore_uncertain",
            "visibility_wrong_account",
            "visibility_recovery_required",
            "visibility_contract_missing",
            "playerok_visibility_network",
            "playerok_visibility_invalid_response",
            "playerok_visibility_failed",
            "playerok_network",
            "playerok_security_check",
            "playerok_rate_limit",
            "playerok_invalid_response",
            "test_requires_own_active_listing",
            "balance_decreased",
        }
        if code not in known and not re.fullmatch(r"playerok_(?:visibility_)?http_[45][0-9]{2}", code):
            code = "unexpected"
        self.db.set("visibility_last_error", code)

    def visibility_controls(self):
        rows = []
        if self.db.get("visibility_test_pending"):
            rows.append(("🔄 Проверить восстановление", "connect:visibility:recover"))
        rows.append(("‹ Продолжить настройку", "setup:home"))
        return buttons(rows)

    def visibility_failure_text(self):
        if self.db.get("visibility_test_pending"):
            return (
                "🛠 Лот пока не восстановлен\n\n"
                "Этот лот временно исключён из автоматизации. Остальные лоты продолжают работать. "
                "Откройте лот в Playerok и восстановите продажу, "
                "если он скрыт. Затем нажмите «Проверить восстановление». "
                "Эта кнопка не снимает лот с продажи повторно."
            )
        return "🛠 Не удалось восстановить продажу. Откройте лот в Playerok и включите продажу."

    async def visibility_item(self, market, item_id):
        method = getattr(market, "visibility_item", market.item)
        return await method(item_id)

    async def test_visibility(self, item_id):
        proxy = self.managed("market")
        async with proxy.exclusive() as market:
            if self.db.get("visibility_test_pending"):
                raise ServiceError("visibility_recovery_required")
            before = await market.profile()
            item = await self.visibility_item(market, item_id)
            if (
                not market.visibility
                or (item.get("user") or {}).get("id") != market.user_id
                or item["status"] != "APPROVED"
            ):
                raise ServiceError("test_requires_own_active_listing")
            self.db.set("live_enabled", False)
            self.db.set("visibility_proof", None)
            self.db.set("visibility_last_error", None)
            self.db.set(
                "visibility_test_pending",
                {
                    "item_id": item_id,
                    "account_id": market.user_id,
                    "phase": "hide_requested",
                    "at": now(),
                },
            )
            hidden = False
            try:
                async with asyncio.timeout(25):
                    await market.set_visible(item_id, False)
                hidden = True
                pending = self.db.get("visibility_test_pending")
                self.db.set("visibility_test_pending", {**pending, "phase": "hide_verified"})
            except (ServiceError, TimeoutError, ValueError, KeyError, TypeError) as error:
                self.record_visibility_error(error)
                raise
            finally:
                # The durable intent survives cancellation/process exit. Recovery
                # runs independently of Telegram delivery and never hides again.
                try:
                    async with asyncio.timeout(30):
                        await self.restore_visibility(market)
                except (ServiceError, TimeoutError, ValueError, KeyError, TypeError) as error:
                    self.record_visibility_error(error)
                    raise
            if not hidden:
                raise ServiceError("hide_uncertain")
            after = await market.profile()
            if rub(after["balance"]["value"]) < rub(before["balance"]["value"]):
                raise ServiceError("balance_decreased")
            self.db.set(
                "visibility_proof",
                {
                    "account_id": market.user_id,
                    "fingerprint": market.fingerprint(),
                    "item_id": item_id,
                    "verified_at": now(),
                },
            )
            self.db.set("last_readiness", 0)
            self.db.audit("visibility_test_passed", item_id)

    async def restore_visibility(self, market, *, retry=False):
        pending = self.db.get("visibility_test_pending")
        if not pending:
            return
        await market.profile()
        if market.user_id != pending["account_id"] or not market.visibility:
            raise ServiceError("visibility_wrong_account")
        item_id = pending["item_id"]
        item = await self.visibility_item(market, item_id)
        if (item.get("user") or {}).get("id") != market.user_id:
            raise ServiceError("visibility_wrong_account")
        phase = pending.get("phase", "legacy")
        self.db.set("visibility_last_status", item.get("status"))
        if item["status"] in market.visibility["hide"]["hidden_statuses"]:
            # A lost reply must not cause repeated restore mutations in the
            # background. A definite rejection can be retried by the admin.
            if phase == "restore_requested" or (phase == "restore_rejected" and not retry):
                raise ServiceError("visibility_restore_uncertain")
            self.db.set("visibility_test_pending", {**pending, "phase": "restore_requested"})
            try:
                await market.set_visible(item_id, True)
            except ServiceError as error:
                if not error.ambiguous:
                    self.db.set("visibility_test_pending", {**pending, "phase": "restore_rejected"})
                raise
        elif item["status"] != "APPROVED":
            # Never republish a sold, blocked, removed or moderation listing.
            raise ServiceError("visibility_restore_uncertain")
        elif phase == "hide_requested" and not retry:
            # Hide may have succeeded despite a failed/stale verification read.
            # Do not erase recovery intent on an immediate APPROVED response.
            raise ServiceError("visibility_restore_uncertain")
        for attempt in range(3):
            item = await self.visibility_item(market, item_id)
            self.db.set("visibility_last_status", item.get("status"))
            if item["status"] != "APPROVED":
                raise ServiceError("visibility_restore_uncertain")
            if attempt < 2:
                await asyncio.sleep(0.25)
        self.db.set("visibility_test_pending", None)
        self.db.set("visibility_last_result", {"at": now(), "restored": True})

    async def recover_visibility(self):
        if self.db.get("visibility_test_pending"):
            try:
                proxy = self.managed("market")
                async with proxy.request():
                    market = proxy.current
                    async with asyncio.timeout(30):
                        await self.restore_visibility(market)
            except (ServiceError, TimeoutError, ValueError, KeyError, TypeError) as error:
                self.record_visibility_error(error)
                raise

    async def automatic_recovery(self):
        """Recover a pre-upgrade test, without blocking connections or hiding again."""
        pending = self.db.get("visibility_test_pending")
        if not pending or self.db.get("paused"):
            return
        binding = self.db.one("SELECT paused FROM bindings WHERE item_id=?", (pending["item_id"],))
        if binding and binding["paused"]:
            return
        state = self.db.get("visibility_recovery", {})
        if state.get("item_id") != pending["item_id"]:
            state = {"item_id": pending["item_id"], "attempts": 0, "approved": 0}
        if now() < state.get("next_at", 0):
            return
        state["attempts"] += 1
        try:
            proxy = self.managed("market")
            async with proxy.request():
                market = proxy.current
                async with asyncio.timeout(30):
                    # An uncertain hide is only settled after fresh APPROVED
                    # readings across three background cycles. Never hide again.
                    if pending.get("phase") == "hide_requested":
                        await market.profile()
                        item = await self.visibility_item(market, pending["item_id"])
                        if (
                            market.user_id != pending["account_id"]
                            or (item.get("user") or {}).get("id") != market.user_id
                        ):
                            raise ServiceError("visibility_wrong_account")
                        state["approved"] = state["approved"] + 1 if item["status"] == "APPROVED" else 0
                        if state["approved"] in {1, 2}:
                            return
                    # Retry only a definite rejection, at most three automatic
                    # attempts. Lost mutation replies are never replayed.
                    retry = pending.get("phase") == "restore_rejected" and state["attempts"] <= 3
                    if pending.get("phase") == "hide_requested" and state["approved"] >= 3:
                        retry = True
                    await self.restore_visibility(market, retry=retry)
            self.db.notify(
                "✅ Тестовый лот снова в продаже. Дополнительные проверки не нужны.",
                "legacy-recovery:" + pending["item_id"] + ":done",
            )
            self.db.set("last_readiness", 0)
        except (ServiceError, TimeoutError, ValueError, KeyError, TypeError) as error:
            self.record_visibility_error(error)
            if state["attempts"] >= 3:
                self.db.notify(
                    "⚠️ Не удалось вернуть прежний тестовый лот в продажу. "
                    "Восстановите его в Playerok. Этот лот временно исключён из автоматизации; "
                    "остальные лоты продолжают работать.",
                    "legacy-recovery:" + pending["item_id"] + ":blocked",
                    purpose="legacy_recovery",
                )
        finally:
            state["next_at"] = now() + min(300, 30 * state["attempts"])
            self.db.set("visibility_recovery", state)
