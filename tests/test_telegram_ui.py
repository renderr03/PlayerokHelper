import asyncio
from datetime import datetime, timezone

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, GetMe, SendDocument, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update, User

from shopbot.telegram_ui import TelegramUI, dispatcher


class CaptureSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, GetMe):
            return User(id=bot.id, is_bot=True, first_name="Shop", username="shopbot")
        if isinstance(method, (SendMessage, SendDocument)):
            return Message(
                message_id=len(self.calls),
                date=datetime.now(timezone.utc),
                chat=Chat(id=int(method.chat_id), type="private"),
                from_user=User(id=bot.id, is_bot=True, first_name="Shop"),
                text=method.text if isinstance(method, SendMessage) else None,
            )
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

    async def send(text=None, callback=None, user=42):
        nonlocal counter
        counter += 1
        actor = User(id=user, is_bot=False, first_name="Admin")
        message = Message(
            message_id=counter,
            date=datetime.now(timezone.utc),
            chat=Chat(id=user, type="private"),
            from_user=actor,
            text=text,
            entities=[MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))]
            if text and text.startswith("/")
            else None,
        )
        if callback:
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
    assert isinstance(response, SendMessage)
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
    ui.db.set(
        "visibility_proof",
        {
            "account_id": ui.engine.market.user_id,
            "fingerprint": ui.engine.market.fingerprint(),
        },
    )
    await send("/setup", user=999)
    assert not session.calls
    await send(callback="setup:source:kosell", user=999)
    assert ui.db.get("setup_source") is None
    await send("/setup")
    assert "Мастер первого запуска" in session.calls[-1].text
    assert "setup --connections" in session.calls[-1].text
    assert "stop playerokhelper" in session.calls[-1].text
    assert not ui.db.get("live_enabled")  # diagnostic UI cannot enable live actions
    await send(callback="setup:source:stock")
    assert ui.db.get("setup_source") == "stock"
    assert "✅ 2. Источник: свой склад" in session.calls[-1].text
    binding(ui.db, module="stock")
    ui.db.add_stock("item1", ["login:do-not-show-this-password"])
    await send(callback="preview:lot:item1", user=999)
    assert ui.db.get("last_preview") is None
    await send(callback="preview:lot:item1")
    assert "проверки пройдены" in session.calls[-1].text
    assert "do-not-show-this-password" not in session.calls[-1].text
    assert ui.db.one("SELECT state FROM stock")["state"] == "free"
    assert not ui.db.get("live_enabled")
    await send("/setup")
    assert "✅ 5." in session.calls[-1].text
    ui.db.execute("UPDATE bindings SET instruction='changed'")
    await send("/setup")
    assert "⬜ 5." in session.calls[-1].text


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


async def test_updates_menu_confirmation_cancel_and_auth(panel):
    import json

    from shopbot import __version__

    ui, session, send = panel
    await send("/version", user=999)
    assert not session.calls
    await send("/version")
    assert __version__ in session.calls[-1].text
    folder = ui.updates.folder
    folder.mkdir()
    (folder / "enabled.json").write_text('{"protocol": 1}')
    ui.db.set("update_release", {"tag": "v0.5.0", "sha": "b" * 40, "url": "", "body": "notes"})
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
