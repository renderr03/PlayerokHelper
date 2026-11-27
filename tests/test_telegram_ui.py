import asyncio
from datetime import datetime, timezone

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageText,
    GetMe,
    SendDocument,
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update, User

from shopbot.telegram_ui import TelegramUI, dispatcher


class CaptureSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.messages = {}

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (AnswerCallbackQuery, DeleteMessage)):
            return True
        if isinstance(method, GetMe):
            return User(id=bot.id, is_bot=True, first_name="Shop", username="shopbot")
        if isinstance(method, (SendMessage, SendDocument, EditMessageText)):
            result = Message(
                message_id=method.message_id if isinstance(method, EditMessageText) else len(self.calls),
                date=datetime.now(timezone.utc),
                chat=Chat(id=int(method.chat_id), type="private"),
                from_user=User(id=bot.id, is_bot=True, first_name="Shop"),
                text=method.text if isinstance(method, (SendMessage, EditMessageText)) else None,
                reply_markup=method.reply_markup,
            )
            self.messages[result.message_id] = result
            return result
        raise AssertionError(type(method).__name__)

    async def stream_content(self, *args, **kwargs):
        yield b""


@pytest.fixture
async def panel(app, tmp_path):
    engine, db, market, kosell = app
    session = CaptureSession()
    bot = Bot("123456789:fake_bot_token_for_tests_only", session=session)
    ui = TelegramUI(bot, engine, [42], tmp_path, project_dir=tmp_path)
    dp = dispatcher(ui)
    counter = 0

    async def send(text=None, callback=None, user=42, chat_type="private", source=None):
        nonlocal counter
        counter += 1
        actor = User(id=user, is_bot=False, first_name="Admin")
        message = Message(
            message_id=counter,
            date=datetime.now(timezone.utc),
            chat=Chat(id=user, type=chat_type),
            from_user=actor,
            text=text,
            entities=[MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))]
            if text and text.startswith("/")
            else None,
        )
        if callback:
            anchor = ui.panels.get((bot.id, user, user))
            if source is not None:
                message = source
            elif anchor:
                message = anchor
            update = Update(
                update_id=counter,
                callback_query=CallbackQuery(
                    id=str(counter), from_user=actor, chat_instance="instance", message=message, data=callback
                ),
            )
        else:
            update = Update(update_id=counter, message=message)
        await asyncio.wait_for(dp.feed_update(bot, update), timeout=3)

    yield ui, session, send
    await dp.storage.close()
    await bot.session.close()


async def test_admin_menu_and_auth_gate(panel):
    ui, session, send = panel
    await send("/start", user=999)
    assert not session.calls
    await send("/start")
    response = session.calls[-1]
    assert isinstance(response, (SendMessage, EditMessageText))
    labels = [button.text for row in response.reply_markup.inline_keyboard for button in row]
    assert "⭐ Автонастройки" in labels
    assert "📈 Аналитика" in labels
    assert not any("Подписка" in label for label in labels)
    await send(callback="feature:greeting", user=999)
    assert ui.db.feature("greeting") is False


async def test_binding_wizard_stock_upload_and_report(panel):
    ui, session, send = panel
    item_id = "02d57c00-d5bd-4f31-8a31-ab3b2fb2af13"
    await send("/bind")
    for answer in [item_id, "stock", "Мой товар", "0", "1", "0", "Инструкция"]:
        await send(answer)
    row = ui.db.one("SELECT * FROM bindings")
    assert row["item_id"] == item_id
    assert row["module"] == "stock"
    await send(callback="stock:" + item_id)
    await send("cost=12.50\nlogin-one:secret-one\nlogin-two:secret-two")
    assert ui.db.one("SELECT count(*) n FROM stock")["n"] == 2
    assert ui.db.one("SELECT cost FROM stock LIMIT 1")["cost"] == 1250
    assert b"secret-one" not in ui.db.path.read_bytes()
    await send(callback="report:all")
    assert isinstance(session.calls[-1], SendDocument)


async def test_template_edit_toggle_and_cancelled_form(panel):
    ui, session, send = panel
    await send(callback="feature:greeting")
    assert ui.db.feature("greeting")
    await send(callback="text:greeting")
    await send("Привет, {buyer}!")
    assert ui.db.get("templates")["greeting"] == "Привет, {buyer}!"
    await send(callback="edit:daily_budget")
    await send(callback="menu:auto")
    await send("1000")
    assert ui.db.get("daily_budget") == 0  # navigating away cancels the form


async def test_stale_reply_preview_cannot_send_a_different_draft(panel):
    ui, session, send = panel
    for chat_id in ["A", "B"]:
        ui.db.execute("INSERT INTO chats(id) VALUES (?)", (chat_id,))
    await send("/reply A Первый ответ")
    old_callback = session.calls[-1].reply_markup.inline_keyboard[0][0].callback_data
    await send("/reply B Второй ответ")
    new_callback = session.calls[-1].reply_markup.inline_keyboard[0][0].callback_data
    await send(callback=old_callback)
    assert not ui.db.all("SELECT * FROM outbox WHERE channel='playerok'")
    await send(callback=new_callback)
    row = ui.db.one("SELECT * FROM outbox WHERE channel='playerok'")
    assert row["target"] == "B"
    assert ui.db.decrypt(row["ciphertext"]) == "Второй ответ"


async def test_setup_wizard_stock_preview_and_auth(panel):
    from conftest import binding

    ui, session, send = panel
    ui.db.set("live_enabled", False)
    await send("/setup", user=999)
    assert not session.calls
    await send(callback="setup:source:kosell", user=999)
    assert ui.db.get("setup_source") is None
    await send("/setup")
    assert "Ваш магазин" in session.calls[-1].text
    assert "connect:visibility" not in callbacks(session.calls[-1])
    assert "connect:check:kosell" not in callbacks(session.calls[-1])
    assert ui.db.get("live_enabled")
    assert not ui.engine.market.transitions
    await send(callback="setup:source:stock")
    assert ui.db.get("setup_source") == "stock"
    assert "Источник: свой склад" in session.calls[-1].text
    binding(ui.db, module="stock")
    ui.db.add_stock("item1", ["login:do-not-show-this-password"])
    await send(callback="preview:lot:item1", user=999)
    assert ui.db.get("last_preview") is None
    await send(callback="preview:lot:item1")
    assert "проверки пройдены" in session.calls[-1].text
    assert "do-not-show-this-password" not in session.calls[-1].text
    assert ui.db.one("SELECT state FROM stock")["state"] == "free"
    ui.db.set("last_preview", None)
    await send("/setup")
    assert "Настроено лотов: 1" in session.calls[-1].text
    assert ui.db.get("live_enabled") and not ui.engine.market.transitions


async def test_alert_settings_validation_cancel_and_toggle(panel):
    ui, session, send = panel
    await send(callback="alerts:edit:stock_low", user=999)
    assert len(session.calls) == 1 and isinstance(session.calls[0], AnswerCallbackQuery)
    assert ui.db.get("stock_low") == 3
    await send(callback="alerts:home")
    assert "Ранние предупреждения" in session.calls[-1].text
    await send(callback="alerts:edit:stock_low")
    await send("-1")
    assert ui.db.get("stock_low") == 3
    await send("7")
    assert ui.db.get("stock_low") == 7
    await send(callback="alerts:edit:rental_low")
    await send("/cancel")
    await send("5")
    assert ui.db.get("rental_low") == 2
    await send(callback="alerts:toggle")
    assert ui.db.get("alerts_enabled") is False
    await send(callback="alerts:toggle")
    assert ui.db.get("alerts_enabled") is True


async def test_extension_preview_selects_existing_rental_and_revalidates(panel):
    from conftest import binding, order

    from shopbot.models import now

    ui, session, send = panel
    market = ui.engine.market
    ui.db.set(
        "visibility_proof",
        {
            "account_id": market.user_id,
            "fingerprint": market.fingerprint(),
        },
    )
    binding(ui.db, item_id="original")
    binding(ui.db, module="extend", item_id="extension")
    order(ui.engine, market, item_id="original")
    ui.engine.update(
        "order1",
        stage="delivered",
        expires_at=now() + 3600,
        rental_uid="uid:playerok:rent:order1",
    )
    await send(callback="preview:lot:extension")
    callback = session.calls[-1].reply_markup.inline_keyboard[0][0].callback_data
    assert callback.startswith("preview:extend:") and len(callback.encode()) <= 64
    await send(callback=callback)
    assert "проверки пройдены" in session.calls[-1].text
    ui.engine.update("order1", expires_at=now() - 1)
    await send(callback=callback)
    assert "действующую аренду" in session.calls[-1].text
    assert ui.engine.kosell.extensions == 0


async def test_updates_menu_confirmation_cancel_and_auth(panel, monkeypatch):
    import json

    from shopbot import __version__

    ui, session, send = panel
    await send("/version", user=999)
    assert not session.calls
    await send("/version")
    assert __version__ in session.calls[-1].text
    monkeypatch.setattr("shopbot.updates.__version__", __version__.split("b")[0])
    folder = ui.updates.folder
    folder.mkdir()
    (folder / "enabled.json").write_text('{"protocol": 1}')
    ui.db.set(
        "update_release",
        {"tag": "v9.9.9", "sha": "b" * 40, "url": "", "body": "## Изменения\n- **Чаты:** `/inbox`."},
    )
    await send("/version")
    assert "• Чаты: /inbox." in session.calls[-1].text
    assert "##" not in session.calls[-1].text and "**" not in session.calls[-1].text
    await send(callback="updates:offer", user=999)
    assert ui.db.get("update_offer") is None
    await send(callback="updates:offer")
    old_callback = session.calls[-1].reply_markup.inline_keyboard[0][0].callback_data
    await send(callback="updates:home")
    await send(callback=old_callback)
    assert not (folder / "request.json").exists()
    await send(callback="updates:offer")
    callback = session.calls[-1].reply_markup.inline_keyboard[0][0].callback_data
    assert len(callback.encode()) <= 64
    await send(callback=callback)
    assert json.loads((folder / "request.json").read_text())["sha"] == "b" * 40
    await send(callback=callback)
    assert "Предложение устарело" in session.calls[-1].text
    await ui.send_admin("42", "release", purpose="update")
    assert session.calls[-1].reply_markup.inline_keyboard[0][0].text == "⬆️ Обновить бота"


async def test_maintenance_rejects_callbacks_and_messages(panel):
    ui, session, send = panel
    folder = ui.updates.folder
    folder.mkdir()
    (folder / "maintenance.json").write_text('{"nonce": "test"}')
    before = ui.db.get("features")
    await send(callback="feature:greeting")
    assert ui.db.get("features") == before
    assert isinstance(session.calls[-1], AnswerCallbackQuery)
    await send("/bind")
    assert "приостановлены" in session.calls[-1].text
    assert not ui.db.all("SELECT * FROM bindings")


def callbacks(method):
    return (
        {b.callback_data for row in method.reply_markup.inline_keyboard for b in row}
        if method.reply_markup
        else set()
    )


async def test_button_cancel_validation_and_wizard_back(panel):
    ui, session, send = panel
    await send(callback="bind:new")
    assert {"ui:cancel", "ui:back"} <= callbacks(session.calls[-1])
    await send("wrong-id")
    assert {"ui:cancel", "ui:back"} <= callbacks(session.calls[-1])
    item_id = "02d57c00-d5bd-4f31-8a31-ab3b2fb2af13"
    await send(item_id)
    assert "s:bindtype:stock" in callbacks(session.calls[-1])
    await send(callback="s:bindtype:stock")
    assert "Название лота" in session.calls[-1].text
    await send("First title")
    assert "Инструкция покупателю" in session.calls[-1].text
    await send(callback="ui:back")
    assert "Название лота" in session.calls[-1].text
    await send("Corrected title")
    await send("-")
    assert ui.db.one("SELECT title FROM bindings")["title"] == "Corrected title"
    await send(callback="text:greeting")
    await send(callback="ui:cancel")
    await send("Do not save this")
    assert ui.db.get("templates")["greeting"] != "Do not save this"
    assert "menu:home" in callbacks(session.calls[-1])


async def test_private_token_connection_is_encrypted_and_live_without_restart(panel):
    from aiogram.methods import DeleteMessage
    from conftest import FakeMarket

    from shopbot.connections import ManagedClient

    ui, session, send = panel
    candidates = []

    class Candidate(FakeMarket):
        def __init__(self, payload, visibility):
            super().__init__()
            assert payload["cookie"] == "token=private-credential"
            candidates.append(self)

        async def close(self):
            pass

    ui.playerok_factory = Candidate
    await send("/start")
    assert "connect:playerok" in callbacks(session.calls[-1])
    await send(callback="connect:playerok")
    assert {"connect:email", "connect:token"} <= callbacks(session.calls[-1])
    await send(callback="connect:token")
    await send("private-credential", user=999)
    assert not candidates
    await send("private-credential")
    assert len(candidates) == 1
    assert isinstance(ui.engine.market, ManagedClient)
    assert ui.engine.market.current is candidates[0]
    assert ui.db.get("profile")["id"] == "seller"
    assert b"private-credential" not in ui.db.path.read_bytes()
    assert "private-credential" not in "".join(
        m.text for m in session.calls if isinstance(m, (SendMessage, EditMessageText))
    )
    assert any(isinstance(m, DeleteMessage) for m in session.calls)
    assert ui.db.get("live_enabled") is True
    await ui.engine.market.close()


async def test_email_flow_cancel_cooldown_attempt_limit_and_no_secret_echo(panel):
    from conftest import FakeMarket

    from shopbot.models import ServiceError

    ui, session, send = panel
    created = []

    class Login:
        def __init__(self):
            self.closed = False
            self.calls = []
            created.append(self)

        async def send(self, email):
            assert email == "owner@example.org"

        async def confirm(self, email, code):
            self.calls.append(code)
            if code != "123456":
                raise ServiceError("wrong_code")
            return {"cookie": "token=email-secret", "user_agent": "test-browser"}

        async def close(self):
            self.closed = True

    class Candidate(FakeMarket):
        def __init__(self, payload, visibility):
            super().__init__()
            assert payload["cookie"] == "token=email-secret"

        async def close(self):
            pass

    ui.login_factory, ui.playerok_factory = Login, Candidate
    await send(callback="connect:email")
    await send("owner@example.org")
    assert "Код запрошен" in session.calls[-1].text
    await send(callback="ui:cancel")
    assert created[0].closed and not ui.logins
    await send("123456")
    assert not ui.db.get("playerok_session")
    await send(callback="connect:email")
    await send("owner@example.org")
    assert len(created) == 1  # cancellation cannot bypass the resend limit
    ui.db.set("login_retry:42", 0)
    await send("owner@example.org")
    for _ in range(5):
        await send("000000")
    assert created[1].closed and not ui.logins
    await send("123456")
    assert not ui.db.get("playerok_session")
    ui.db.set("login_retry:42", 0)
    await send(callback="connect:email")
    await send("owner@example.org")
    await send("123456")
    assert ui.db.get("playerok_session") and not ui.logins and created[-1].closed
    assert "123456" not in "".join(
        m.text for m in session.calls if isinstance(m, (SendMessage, EditMessageText))
    )
    await ui.engine.market.close()


async def test_old_visibility_buttons_cannot_hide_and_legacy_recovery_still_works(panel):
    from shopbot.models import ServiceError

    ui, session, send = panel
    market = ui.engine.market
    item_id = "02d57c00-d5bd-4f31-8a31-ab3b2fb2af13"
    await send(callback="connect:visibility")
    assert "Тест скрытия больше не нужен" in session.calls[-1].text
    await send(callback="connect:visibility:run:old-nonce")
    assert not market.transitions and not ui.db.get("visibility_proof")
    original = market.set_visible

    async def lost_hide(item, visible):
        await original(item, visible)
        if not visible:
            raise ServiceError("lost_hide", ambiguous=True)

    market.set_visible = lost_hide
    with pytest.raises(ServiceError):
        await ui.test_visibility(item_id)
    assert (await market.item(item_id))["status"] == "APPROVED"
    assert ui.db.get("visibility_proof") is None
    market.items[item_id]["status"] = "PAUSED"
    ui.db.set("visibility_test_pending", {"account_id": "seller", "item_id": item_id})
    await ui.recover_visibility()
    assert (await market.item(item_id))["status"] == "APPROVED"
    assert ui.db.get("visibility_test_pending") is None


async def test_cannot_mix_playerok_stores_or_replace_supplier_during_rental(panel):
    from conftest import FakeMarket, binding, order

    from shopbot.models import now

    ui, _, _ = panel
    binding(ui.db)

    class Candidate(FakeMarket):
        user_id = "other-store"

        def __init__(self, payload, visibility):
            super().__init__()
            self.closed = False

        async def close(self):
            self.closed = True

    candidate = Candidate(None, None)
    ui.playerok_factory = lambda *args: candidate
    with pytest.raises(ValueError, match="another_store"):
        await ui.apply_playerok({"cookie": "token=test-token", "user_agent": "test-browser"})
    assert candidate.closed and ui.db.get("playerok_session") is None
    order(ui.engine, ui.engine.market.current)
    ui.engine.update("order1", stage="delivered", expires_at=now() + 3600)
    with pytest.raises(ValueError, match="unsettled_orders"):
        await ui.apply_kosell("new-supplier-token")
    assert ui.db.get("kosell_token") is None


async def test_provider_key_buttons_and_simple_help(panel):
    ui, session, send = panel
    await send(callback="x:help:stars")
    assert "API-ключ" in session.calls[-1].text
    assert "ui:cancel" in callbacks(session.calls[-1])
    await send("split-private-key")
    row = ui.db.one("SELECT * FROM providers")
    assert row["preset"] == "split" and not row["enabled"]
    assert ui.db.decrypt(row["token"]) == "split-private-key"
    assert b"split-private-key" not in ui.db.path.read_bytes()
    await send(callback="x:help:smm")
    assert {"connect:key:twiboost", "connect:key:vexboost", "connect:key:jap"} <= callbacks(session.calls[-1])
    for action in ["setup:home", "menu:help", "menu:account", "updates:home"]:
        await send(callback=action)
    all_text = "\n".join(m.text for m in session.calls if isinstance(m, (SendMessage, EditMessageText)))
    for forbidden in ["/cancel", "/setup", "/reply", "systemctl", ".venv/bin", "sudo ", "provider-setup"]:
        assert forbidden not in all_text


async def test_credentials_are_not_accepted_in_groups(panel):
    ui, session, send = panel
    await send(callback="connect:token", chat_type="group")
    assert "личном чате" in session.calls[-1].text
    await send("group-private-token", chat_type="group")
    assert not ui.db.get("playerok_session")
    await send(callback="connect:key:split", chat_type="group")
    await send("group-private-key", chat_type="group")
    assert not ui.db.all("SELECT * FROM providers")


async def test_reconnect_same_account_can_recover_pending_visibility(panel):
    from conftest import FakeMarket, binding

    ui, _, _ = panel
    binding(ui.db)
    ui.db.set("visibility_test_pending", {"item_id": "test-lot", "account_id": "seller"})

    class Candidate(FakeMarket):
        def __init__(self, *args):
            super().__init__()
            self.items["test-lot"] = {"id": "test-lot", "status": "PAUSED", "user": {"id": "seller"}}

        async def close(self):
            pass

    ui.playerok_factory = Candidate
    await ui.apply_playerok({"cookie": "token=renewed-session", "user_agent": "test-browser"})
    assert ui.db.get("visibility_test_pending")
    await ui.recover_visibility()
    assert not ui.db.get("visibility_test_pending")
    assert (await ui.engine.market.item("test-lot"))["status"] == "APPROVED"
    await ui.engine.market.close()


async def test_supplier_swap_rechecks_intents_after_balance_await(panel):
    from conftest import binding, order

    ui, _, _ = panel
    binding(ui.db)

    class Candidate:
        closed = False

        async def balance(self):
            # Another worker accepts a paid order while the candidate key is checked.
            order(ui.engine, ui.engine.market)
            return {"balance_rub": 100}

        async def close(self):
            self.closed = True

    candidate = Candidate()
    ui.kosell_factory = lambda *args: candidate
    with pytest.raises(ValueError, match="unsettled_orders"):
        await ui.apply_kosell("candidate-secret-key")
    assert candidate.closed and not ui.db.get("kosell_token")


async def test_failed_legacy_restore_does_not_disable_whole_store(panel):
    from shopbot.models import ServiceError
    from shopbot.runtime import readiness

    ui, _, _ = panel
    market = ui.engine.market
    original = market.set_visible

    async def fail_restore(item, visible):
        if visible:
            raise ServiceError("restore_failed")
        return await original(item, visible)

    market.set_visible = fail_restore
    with pytest.raises(ServiceError):
        await ui.test_visibility("test-lot")
    assert ui.db.get("visibility_test_pending") and not ui.db.get("visibility_proof")
    await readiness(ui.db, ui.engine.market, ui.engine.kosell, needs_kosell=False)
    assert ui.db.get("live_enabled")
    market.set_visible = original
    with pytest.raises(ServiceError):
        await ui.recover_visibility()  # no automatic repeat of a rejected write
    async with ui.managed("market").exclusive() as client:
        await ui.restore_visibility(client, retry=True)
    assert not ui.db.get("visibility_test_pending")
    assert (await market.item("test-lot"))["status"] == "APPROVED"


async def test_token_connection_succeeds_after_realistic_playerok_cache_500(panel):
    import httpx

    from shopbot.playerok import Playerok

    ui, session, send = panel
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["cookie"] == "token=correct-private-token"
        if request.method == "GET":
            return httpx.Response(
                500, json={"errors": [{"extensions": {"code": "PERSISTED_QUERY_NOT_FOUND"}}]}
            )
        return httpx.Response(
            200, json={"data": {"viewer": {"id": "seller", "username": "myshop", "balance": {"value": 100}}}}
        )

    ui.playerok_factory = lambda session, visibility: Playerok(
        session, visibility, transport=httpx.MockTransport(handle)
    )
    await send(callback="connect:token")
    await send("correct-private-token")
    assert "Playerok подключён" in session.calls[-1].text
    assert [r.method for r in requests] == ["GET", "POST"]
    assert ui.db.get("playerok_session")
    assert "correct-private-token" not in "".join(
        c.text for c in session.calls if isinstance(c, (SendMessage, EditMessageText))
    )
    await ui.engine.market.close()


@pytest.mark.parametrize(
    "code,expected",
    [
        ("playerok_http_403", "HTTP 403"),
        ("playerok_http_500", "HTTP 500"),
        ("playerok_auth_required", "сессию"),
        ("playerok_network", "связаться"),
        ("playerok_query_rejected", "формат запроса"),
    ],
)
async def test_token_failure_shows_specific_safe_reason(panel, code, expected):
    from shopbot.models import ServiceError

    ui, session, send = panel

    class Candidate:
        async def profile(self):
            raise ServiceError(code)

        async def close(self):
            pass

    ui.playerok_factory = lambda *args: Candidate()
    await send(callback="connect:token")
    await send("private-test-token")
    assert expected in session.calls[-1].text
    assert "private-test-token" not in session.calls[-1].text
    assert "ui:cancel" in callbacks(session.calls[-1])
    assert not ui.db.get("playerok_session")


async def test_kosell_check_without_bindings_shows_balance_and_retains_panel(panel):
    ui, session, send = panel
    calls = []

    async def balance():
        calls.append("balance")
        return {"balance_rub": "1250.55"}

    ui.engine.kosell.balance = balance
    assert not ui.db.get("setup_source") and not ui.db.all("SELECT * FROM bindings")
    await send(callback="connect:home")
    await send(callback="connect:check:kosell")
    assert calls == ["balance"]
    assert "1250.55" in session.calls[-1].text
    assert "KOSell подключён" in session.calls[-1].text
    assert "connect:check:kosell" in callbacks(session.calls[-1])
    assert not ui.db.get("setup_source")
    assert ui.db.get("kosell_last_check")["ok"]
    assert isinstance(session.calls[-1], EditMessageText)


async def test_kosell_check_timeout_finishes_and_keeps_key(panel, monkeypatch):
    import shopbot.account_ui as accounts

    ui, session, send = panel
    ui.db.set("kosell_token", ui.db.encrypt("private-key"))
    monkeypatch.setattr(accounts, "CHECK_TIMEOUT", 0.01)

    async def stalled():
        await asyncio.Event().wait()

    ui.engine.kosell.balance = stalled
    await send(callback="connect:check:kosell")
    assert "не ответил" in session.calls[-1].text
    assert ui.db.get("kosell_last_check")["reason"] == "timeout"
    assert ui.db.decrypt(ui.db.get("kosell_token")) == "private-key"
    assert "private-key" not in session.calls[-1].text


async def test_legacy_recovery_result_remains_in_same_panel(panel):
    ui, session, send = panel
    market = ui.engine.market
    item_id = "02d57c00-d5bd-4f31-8a31-ab3b2fb2af13"
    market.items[item_id] = {"status": "PAUSED", "user": {"id": "seller"}}
    ui.db.set(
        "visibility_test_pending", {"item_id": item_id, "account_id": "seller", "phase": "restore_rejected"}
    )
    await send(callback="connect:visibility:recover")
    assert "снова в продаже" in session.calls[-1].text
    assert market.transitions == [(item_id, True)]
    assert not ui.db.get("visibility_test_pending")


async def test_uncertain_restore_is_not_repeated_and_can_be_reconciled(panel):
    from shopbot.models import ServiceError

    ui, _, _ = panel
    market = ui.engine.market
    original = market.set_visible
    restore_calls = []

    async def lost_restore(item, visible):
        if visible:
            restore_calls.append(item)
            raise ServiceError("playerok_visibility_network", ambiguous=True)
        return await original(item, visible)

    market.set_visible = lost_restore
    with pytest.raises(ServiceError):
        await ui.test_visibility("test-lot")
    for _ in range(2):
        with pytest.raises(ServiceError):
            await ui.recover_visibility()
    assert restore_calls == ["test-lot"]
    market.items["test-lot"]["status"] = "APPROVED"  # delayed success or manual restore
    await ui.recover_visibility()
    assert not ui.db.get("visibility_test_pending")
    assert restore_calls == ["test-lot"]


async def test_stale_hide_read_does_not_erase_recovery_intent(panel):
    from shopbot.models import ServiceError

    ui, _, _ = panel
    market = ui.engine.market
    original = market.set_visible
    fresh = False

    async def lost_hide(item, visible):
        await original(item, visible)
        if not visible:
            raise ServiceError("visibility_not_verified", ambiguous=True)

    async def read(item):
        result = await market.item(item)
        if not fresh:
            result["status"] = "APPROVED"
        return result

    market.set_visible = lost_hide
    market.visibility_item = read
    with pytest.raises(ServiceError):
        await ui.test_visibility("test-lot")
    assert ui.db.get("visibility_test_pending")["phase"] == "hide_requested"
    assert market.items["test-lot"]["status"] == "PAUSED"
    fresh = True
    await ui.recover_visibility()
    assert market.items["test-lot"]["status"] == "APPROVED"
    assert not ui.db.get("visibility_test_pending")


async def test_cancel_after_hide_still_restores_and_never_grants_proof(panel):
    ui, _, _ = panel
    market = ui.engine.market
    original = market.set_visible
    hidden = asyncio.Event()

    async def hide_then_wait(item, visible):
        result = await original(item, visible)
        if not visible:
            hidden.set()
            await asyncio.Event().wait()
        return result

    market.set_visible = hide_then_wait
    task = asyncio.create_task(ui.test_visibility("test-lot"))
    await hidden.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert market.items["test-lot"]["status"] == "APPROVED"
    assert not ui.db.get("visibility_test_pending")
    assert not ui.db.get("visibility_proof")


@pytest.mark.parametrize("status,owner", [("SOLD", "seller"), ("BLOCKED", "seller"), ("PAUSED", "other")])
async def test_visibility_recovery_does_not_republish_unsafe_listing(panel, status, owner):
    from shopbot.models import ServiceError

    ui, _, _ = panel
    market = ui.engine.market
    market.items["test-lot"] = {"status": status, "user": {"id": owner}}
    ui.db.set("visibility_test_pending", {"item_id": "test-lot", "account_id": "seller"})
    with pytest.raises(ServiceError):
        await ui.recover_visibility()
    assert not market.transitions
    assert ui.db.get("visibility_test_pending")


async def test_saved_kosell_key_checks_automatically_and_shows_balance(panel):
    ui, session, send = panel
    calls = []

    class Candidate:
        configured = True

        async def balance(self):
            calls.append("balance")
            return {"balance_rub": "700.00"}

        async def close(self):
            pass

    ui.kosell_factory = lambda *args: Candidate()
    await send(callback="connect:key:kosell")
    await send("supplier-private-api-key")
    assert "connect:check:kosell" not in callbacks(session.calls[-1])
    assert "bind:new" in callbacks(session.calls[-1])
    assert calls == ["balance"]
    await send(callback="setup:home")
    assert calls == ["balance"]
    assert "700.00" in session.calls[-1].text
    assert ui.db.get("setup_source") == "kosell"


async def test_legacy_recovery_delivery_failure_does_not_interrupt_restore(panel):
    from aiogram.exceptions import TelegramNetworkError

    ui, session, send = panel
    item_id = "02d57c00-d5bd-4f31-8a31-ab3b2fb2af13"
    market = ui.engine.market
    market.items[item_id] = {"status": "PAUSED", "user": {"id": "seller"}}
    ui.db.set("visibility_test_pending", {"item_id": item_id, "account_id": "seller"})
    original = session.make_request

    async def fail_result(bot, method, timeout=None):
        if isinstance(method, (SendMessage, EditMessageText)) and "снова в продаже" in method.text:
            raise TelegramNetworkError(method=method, message="lost result")
        return await original(bot, method, timeout=timeout)

    session.make_request = fail_result
    with pytest.raises(TelegramNetworkError):
        await send(callback="connect:visibility:recover")
    assert not ui.db.get("visibility_test_pending")
    assert market.transitions == [(item_id, True)]


async def test_two_token_onboarding_is_automatic_and_never_hides_a_lot(panel):
    from conftest import FakeMarket

    ui, session, send = panel
    reads = []

    class Market(FakeMarket):
        def __init__(self, *args):
            super().__init__()

        async def profile(self):
            reads.append("profile")
            return await super().profile()

        async def close(self):
            pass

    class Supplier:
        configured = True

        async def balance(self):
            reads.append("balance")
            return {"balance_rub": "999.50", "balance_usd": "10"}

        async def close(self):
            pass

    ui.playerok_factory = Market
    ui.kosell_factory = lambda *args: Supplier()
    await send(callback="connect:token")
    await send("private-playerok-key")
    assert "connect:key:kosell" in callbacks(session.calls[-1])
    assert ui.db.get("live_enabled")
    await send(callback="connect:key:kosell")
    await send("private-kosell-key")
    assert reads == ["profile", "balance"]
    assert "999.50" in session.calls[-1].text
    assert "connect:check:kosell" not in callbacks(session.calls[-1])
    await send(callback="setup:home")
    assert reads == ["profile", "balance"]
    assert "connect:visibility" not in callbacks(session.calls[-1])
    assert ui.db.get("live_enabled") and not ui.db.get("visibility_proof")
    assert not ui.engine.market.current.transitions
    assert not ui.db.all("SELECT * FROM expenses")
    assert not ui.db.all("SELECT * FROM orders")


async def test_slow_legacy_recovery_does_not_block_connections(panel, tmp_path):
    from types import SimpleNamespace

    from shopbot.runtime import Runtime

    ui, _, _ = panel
    market = ui.engine.market
    started, release = asyncio.Event(), asyncio.Event()
    original = market.item

    async def slow_item(item):
        started.set()
        await release.wait()
        return await original(item)

    market.visibility_item = slow_item
    ui.db.set("visibility_test_pending", {"item_id": "old-lot", "account_id": "seller"})
    task = asyncio.create_task(ui.automatic_recovery())
    await asyncio.wait_for(started.wait(), 1)
    runtime = Runtime(SimpleNamespace(data_dir=tmp_path), ui.db, ui.engine, ui)
    try:
        await asyncio.wait_for(runtime.connections(), 1)
        assert ui.db.get("live_enabled")
    finally:
        release.set()
        await task
    assert not ui.db.get("visibility_test_pending")


async def test_automatic_legacy_recovery_retries_definite_rejections_but_not_lost_writes(panel):
    from shopbot.models import ServiceError

    ui, _, _ = panel
    ui.db.set("admins", [42])
    market = ui.engine.market
    market.items["old-lot"] = {"status": "PAUSED", "user": {"id": "seller"}}
    ui.db.set(
        "visibility_test_pending", {"item_id": "old-lot", "account_id": "seller", "phase": "restore_rejected"}
    )
    calls = []

    async def rejected(item, visible):
        calls.append((item, visible))
        raise ServiceError("playerok_visibility_http_403")

    market.set_visible = rejected
    for _ in range(5):
        state = ui.db.get("visibility_recovery", {})
        state["next_at"] = 0
        ui.db.set("visibility_recovery", state)
        await ui.automatic_recovery()
    assert calls == [("old-lot", True)] * 3
    assert ui.db.get("live_enabled")
    assert len(ui.db.all("SELECT * FROM outbox WHERE purpose='legacy_recovery'")) == 1
    ui.db.set(
        "visibility_test_pending",
        {"item_id": "old-lot", "account_id": "seller", "phase": "restore_requested"},
    )
    state = ui.db.get("visibility_recovery")
    state["next_at"] = 0
    ui.db.set("visibility_recovery", state)
    await ui.automatic_recovery()
    assert len(calls) == 3  # uncertain write cannot be repeated
    market.items["old-lot"]["status"] = "APPROVED"
    state["next_at"] = 0
    ui.db.set("visibility_recovery", state)
    await ui.automatic_recovery()
    assert not ui.db.get("visibility_test_pending")
    assert len(calls) == 3


async def test_uncertain_legacy_hide_is_settled_automatically_after_separate_fresh_reads(panel):
    ui, _, _ = panel
    ui.db.set(
        "visibility_test_pending", {"item_id": "old-lot", "account_id": "seller", "phase": "hide_requested"}
    )
    for attempt in range(3):
        state = ui.db.get("visibility_recovery", {})
        state["next_at"] = 0
        ui.db.set("visibility_recovery", state)
        await ui.automatic_recovery()
        assert bool(ui.db.get("visibility_test_pending")) == (attempt < 2)
    assert not ui.engine.market.current.transitions


@pytest.mark.parametrize("pause", ["store", "lot"])
async def test_automatic_legacy_recovery_respects_manual_pause(panel, pause):
    from conftest import binding

    ui, _, _ = panel
    market = ui.engine.market
    market.items["old-lot"] = {"status": "PAUSED", "user": {"id": "seller"}}
    ui.db.set("visibility_test_pending", {"item_id": "old-lot", "account_id": "seller"})
    if pause == "store":
        ui.db.set("paused", True)
    else:
        binding(ui.db, module="stock", item_id="old-lot", paused=1)
    await ui.automatic_recovery()
    assert not market.transitions and ui.db.get("visibility_test_pending")
