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
from .connections import EmailLogin, ManagedClient, session_input
from .external import save_provider
from .kosell import KOSell
from .models import ServiceError, now, rub
from .navigation import buttons
from .playerok import Playerok
from .providers import PRESETS

DOCS = (
    "https://github.com/renderr03/PlayerokHelper/blob/"
    + ("beta/v0.5.0" if "b" in __version__ else "main")
    + "/docs/CONNECTIONS.md"
)


class ConnectionForm(StatesGroup):
    token = State()
    email = State()
    code = State()
    key = State()
    visibility = State()


def controls(back="connect:home"):
    return buttons([("‹ Назад", back), ("✖ Отменить", "ui:cancel")])


def connection_error_text(error, *, playerok=True):
    """Show only known failure categories, never arbitrary server text or keys."""
    if not playerok:
        if error.code == "kosell_http_401":
            return "KOSell не принял API-ключ. Проверьте его в кабинете поставщика."
        if error.code == "kosell_rate_limit":
            return "KOSell ограничил частоту запросов. Подождите и повторите проверку."
        return "Поставщик не подтвердил подключение. Проверьте его доступность и API-ключ в кабинете."
    messages = {
        "playerok_auth_required": "Playerok не подтвердил сессию. Она могла истечь или быть отозвана. Войдите на сайте и получите новый токен.",
        "playerok_http_401": "Playerok не подтвердил сессию (HTTP 401). Она могла истечь или быть отозвана. Войдите на сайте и получите новый токен.",
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
        return "Playerok вернул HTTP " + match[1] + ". Подключение пока не подтверждено. Попробуйте позже."
    if error.code.startswith("playerok_login_"):
        return "Вход по почте не подтверждён. Проверьте код. Если сайт требует проверку браузера или второй фактор, войдите на сайте и используйте токен."
    return "Подключение Playerok не подтверждено. Проверьте доступ к аккаунту на сайте и повторите попытку."


class AccountUI:
    def init_connections(self):
        self.logins = {}
        self.connection_lock = asyncio.Lock()
        self.connection_config = None
        self.playerok_factory = Playerok
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
        profile = self.db.get("profile") or {}
        name = profile.get("username") or "не добавлен"
        await message.answer(
            f"Подключения магазина\nPlayerok: {name}\n"
            "Добавьте аккаунт и нужного поставщика. Для своего склада поставщик не нужен.",
            reply_markup=buttons(
                [
                    (
                        "➕ Добавить аккаунт Playerok" if not profile else "🔑 Переподключить Playerok",
                        "connect:playerok",
                    ),
                    ("🎮 Подключить KOSell", "connect:key:kosell"),
                    ("⭐ Подключить Split", "connect:key:split"),
                    ("Подключить SMM-панель", "connect:smm"),
                    ("🔄 Проверить подключение", "setup:home"),
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
        elif data == "connect:playerok":
            await state.clear()
            await self.expire_logins(message.chat.id)
            await message.answer(
                "Как добавить аккаунт Playerok?",
                reply_markup=buttons(
                    [
                        ("✉️ По почте", "connect:email"),
                        ("🔑 По токену", "connect:token"),
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
        elif data == "connect:smm":
            await state.clear()
            await message.answer(
                "Выберите SMM-панель:",
                reply_markup=buttons(
                    [(PRESETS[p][2], "connect:key:" + p) for p in ("twiboost", "vexboost", "jap")]
                    + [("‹ Назад", "connect:home")]
                ),
            )
        elif data.startswith("connect:key:"):
            preset = data.split(":")[2]
            if preset not in {*PRESETS, "kosell"}:
                return True
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
        elif data == "connect:visibility":
            await state.clear()
            await state.set_state(ConnectionForm.visibility)
            await message.answer(
                "Пришлите ссылку или ID своего активного тестового лота. "
                "На следующем шаге бот попросит подтверждение краткого скрытия и восстановления.",
                reply_markup=controls("setup:home"),
            )
        elif data == "connect:visibility:run":
            context = await state.get_data()
            item_id, expires = context.get("visibility_item"), context.get("visibility_expires", 0)
            await state.clear()
            if not item_id or expires < now():
                await message.answer("Подтверждение устарело. Выберите тестовый лот снова.")
            else:
                await message.answer("Проверяю скрытие и восстановление лота…")
                try:
                    await self.test_visibility(item_id)
                    await message.answer("Лот восстановлен. Проверка пройдена.")
                except (ServiceError, ValueError, KeyError, TypeError):
                    await message.answer(
                        "Проверка не завершена. Откройте лот и проверьте его статус. "
                        "Если восстановление не подтверждено, автоматизация останется отключённой."
                    )
                await self.setup_menu(message, state)
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
                elif form == ConnectionForm.key.state:
                    preset = context["preset"]
                    if preset == "kosell":
                        await self.apply_kosell(text)
                    else:
                        save_provider(self.db, preset, text)
                    await state.clear()
                    await message.answer(
                        "Ключ сохранён. Теперь проверьте подключение.",
                        reply_markup=buttons(
                            [
                                (
                                    "Проверить подключение",
                                    "setup:home"
                                    if preset == "kosell"
                                    else "x:provider:"
                                    + str(
                                        self.db.one("SELECT id FROM providers WHERE preset=?", (preset,))[
                                            "id"
                                        ]
                                    ),
                                ),
                                ("‹ Подключения", "connect:home"),
                            ]
                        ),
                    )
                elif form == ConnectionForm.visibility.state:
                    match = re.search(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", text)
                    item_id = str(uuid.UUID(match.group() if match else text))
                    await state.update_data(visibility_item=item_id, visibility_expires=now() + 120)
                    await message.answer(
                        "Лот будет ненадолго скрыт и восстановлен. "
                        "Используйте свой тестовый лот без активной сделки. Продолжить?",
                        reply_markup=buttons(
                            [
                                ("Проверить лот", "connect:visibility:run"),
                                ("Другой лот", "connect:visibility"),
                                ("Отменить", "ui:cancel"),
                            ]
                        ),
                    )
            except ServiceError as exc:
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
                await message.answer(
                    connection_error_text(exc, playerok=form != ConnectionForm.key.state),
                    reply_markup=controls(
                        "connect:playerok" if form != ConnectionForm.key.state else "connect:home"
                    ),
                )
            except (ValueError, KeyError, TypeError) as exc:
                reason = {
                    "Подождите минуту перед повторным запросом кода.": "Подождите минуту перед повторным запросом кода.",
                    "another_store": "В этой базе уже есть данные другого магазина. Переподключите прежний аккаунт Playerok.",
                    "unsettled_orders": "Ключ KOSell пока нельзя заменить: есть незавершённые заказы, действующие аренды или спорные списания. Сначала завершите их сверку.",
                    "Сначала завершите сверку текущих заказов этого поставщика": "У поставщика есть незавершённые заказы. Сначала завершите их сверку.",
                }.get(str(exc))
                fallback = {
                    ConnectionForm.email.state: "Проверьте адрес почты: например name@example.com.",
                    ConnectionForm.code.state: "Отправьте шесть цифр из письма Playerok.",
                    ConnectionForm.token.state: "Токен не распознан. Пришлите его значение, Cookie или JSON с cookie и user_agent.",
                    ConnectionForm.key.state: "Ключ не принят. Скопируйте API-ключ из кабинета поставщика без пробелов.",
                    ConnectionForm.visibility.state: "Пришлите ссылку с UUID или сам UUID своего активного лота.",
                }
                await message.answer(
                    reason or fallback.get(form, "Проверьте данные и повторите ввод."),
                    reply_markup=controls(
                        "connect:playerok" if form != ConnectionForm.key.state else "connect:home"
                    ),
                )

    async def connected(self, message):
        profile = self.db.get("profile") or {}
        await message.answer(
            "Аккаунт Playerok подключён: " + str(profile.get("username") or profile["id"]),
            reply_markup=buttons([("Продолжить настройку", "setup:home"), ("В главное меню", "menu:home")]),
        )

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
                    self.db.set("live_enabled", False)
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
                    balance = await client.balance()
                    if rub(balance["balance_rub"]) < 0:
                        raise ServiceError("kosell_invalid_balance")
                    if self.kosell_in_use():
                        raise ValueError("unsettled_orders")
                    with self.db.transaction():
                        self.db.set("kosell_token", self.db.encrypt(token))
                        self.db.set("last_readiness", 0)
                        self.db.audit("kosell_credentials_changed", "")
                    proxy.replace(client)
                    saved = True
                finally:
                    if not saved:
                        await client.close()

    async def test_visibility(self, item_id):
        proxy = self.managed("market")
        async with proxy.exclusive() as market:
            if self.db.get("visibility_test_pending"):
                raise ServiceError("visibility_recovery_required")
            before = await market.profile()
            item = await market.item(item_id)
            if (
                not market.visibility
                or (item.get("user") or {}).get("id") != market.user_id
                or item["status"] != "APPROVED"
            ):
                raise ServiceError("test_requires_own_active_listing")
            self.db.set("live_enabled", False)
            self.db.set("visibility_proof", None)
            self.db.set("visibility_test_pending", {"item_id": item_id, "account_id": market.user_id})
            hidden = False
            try:
                await market.set_visible(item_id, False)
                hidden = True
            finally:
                await self.restore_visibility(market)
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

    async def restore_visibility(self, market):
        pending = self.db.get("visibility_test_pending")
        if not pending:
            return
        await market.profile()
        if market.user_id != pending["account_id"]:
            raise ServiceError("visibility_wrong_account")
        item_id = pending["item_id"]
        item = await market.item(item_id)
        if item["status"] in market.visibility["hide"]["hidden_statuses"]:
            await market.set_visible(item_id, True)
        if (await market.item(item_id))["status"] != "APPROVED":
            raise ServiceError("visibility_restore_uncertain")
        self.db.set("visibility_test_pending", None)

    async def recover_visibility(self):
        if self.db.get("visibility_test_pending"):
            self.db.set("live_enabled", False)
            async with self.managed("market").exclusive() as market:
                await self.restore_visibility(market)
