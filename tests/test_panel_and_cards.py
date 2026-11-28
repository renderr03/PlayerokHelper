from dataclasses import replace
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import EditMessageText, SendDocument, SendMessage
from conftest import binding, order
from test_telegram_ui import panel as panel

from shopbot.conversations import Conversations
from shopbot.models import Message, now
from shopbot.presentation import Cards
from shopbot.runtime import Runtime


def text_calls(session):
    return [call for call in session.calls if isinstance(call, (SendMessage, EditMessageText))]


async def test_menu_and_forms_reuse_one_panel_message(panel):
    ui, session, send = panel
    await send("/start")
    original = session.calls[-1]
    anchor = ui.panels[(ui.bot.id, 42, 42)]
    for action in ["menu:auto", "menu:profile", "menu:account", "setup:home", "bind:new"]:
        await send(callback=action)
        assert isinstance(session.calls[-1], EditMessageText)
        assert session.calls[-1].message_id == anchor.message_id
    await send("wrong uuid")
    assert session.calls[-1].message_id == anchor.message_id
    await send(callback="ui:cancel")
    assert session.calls[-1].message_id == anchor.message_id
    assert sum(isinstance(c, SendMessage) for c in session.calls) == 1
    assert original.text.startswith("<b>🎮 PlayerokHelper</b>") and original.parse_mode == "HTML"


@pytest.mark.parametrize("reason", ["message is not modified", "message to edit not found"])
async def test_unchanged_or_deleted_panel_has_no_message_spam(panel, reason):
    ui, session, send = panel
    await send("/start")
    first = ui.panels[(ui.bot.id, 42, 42)].message_id
    original = session.make_request
    failed = False

    async def request(bot, method, timeout=None):
        nonlocal failed
        if isinstance(method, EditMessageText) and not failed:
            failed = True
            session.calls.append(method)
            raise TelegramBadRequest(method=method, message=reason)
        return await original(bot, method, timeout)

    session.make_request = request
    await send(callback="menu:auto")
    assert sum(isinstance(c, SendMessage) for c in session.calls) == (1 if "not modified" in reason else 2)
    current = ui.panels[(ui.bot.id, 42, 42)].message_id
    await send(callback="menu:account")
    assert isinstance(session.calls[-1], EditMessageText)
    assert session.calls[-1].message_id == current
    assert current == first if "not modified" in reason else current != first


@pytest.mark.parametrize("network", [True, False])
async def test_unknown_edit_failure_never_falls_back_to_duplicate_send(panel, network):
    _, session, send = panel
    await send("/start")
    original = session.make_request

    async def request(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            error = TelegramNetworkError if network else TelegramBadRequest
            raise error(method=method, message="unknown delivery result")
        return await original(bot, method, timeout)

    session.make_request = request
    with pytest.raises(TelegramNetworkError if network else TelegramBadRequest):
        await send(callback="menu:auto")
    assert sum(isinstance(c, SendMessage) for c in session.calls) == 1


async def test_order_card_is_html_escaped_with_compact_real_actions(panel):
    ui, session, _ = panel
    binding(ui.db)
    deal = order(ui.engine, ui.engine.market, title="<script>& product", buyer_name="<b>buyer</b>")
    ui.db.set("profile", {"username": "<i>seller</i>"})
    remote = await ui.send_admin("42", "old fallback text", deal.id, "card:order:new")
    card = session.calls[-1]
    assert isinstance(card, SendMessage) and card.parse_mode == "HTML"
    assert "<script>" not in card.text and "&lt;script&gt;" in card.text
    assert "<i>seller</i>" not in card.text and "&lt;i&gt;seller" in card.text
    assert "<b>buyer</b>" not in card.text
    assert "Заказ №1" in card.text and "Открыть чат" in card.text
    assert len(card.reply_markup.inline_keyboard[0]) == 3
    assert all(b.callback_data.startswith("card:") for row in card.reply_markup.inline_keyboard for b in row)
    assert ui.cards.route(card.reply_markup.inline_keyboard[0][0].callback_data).startswith("s:reply:")
    assert int(remote) in session.messages


async def test_order_number_is_stable_and_local_day_is_respected(panel):
    ui, _, _ = panel
    binding(ui.db)
    first = order(ui.engine, ui.engine.market, "one", created_at=now() - 2)
    second = order(ui.engine, ui.engine.market, "two", created_at=now() - 1)
    assert "№2" in ui.cards.order(second.id, "new").text
    assert "№1" in ui.cards.order(first.id, "new").text
    assert "№2" in ui.cards.order(second.id, "new").text


@pytest.mark.parametrize("restart", [False, True])
async def test_notification_actions_preserve_event_and_use_active_panel(panel, restart):
    ui, session, send = panel
    binding(ui.db)
    deal = order(ui.engine, ui.engine.market)
    await send("/start")
    anchor = ui.panels[(ui.bot.id, 42, 42)].message_id
    remote = await ui.send_admin("42", "fallback", deal.id, "card:order:new")
    notification = session.messages[int(remote)]
    callback = notification.reply_markup.inline_keyboard[0][0].callback_data
    if restart:
        ui.panels.clear()
    await send(callback=callback, source=notification)
    last = session.calls[-1]
    assert isinstance(last, SendMessage)
    new_anchor = ui.panels[(ui.bot.id, 42, 42)].message_id
    assert new_anchor > notification.message_id and new_anchor != anchor
    await send(callback=callback, source=notification)
    assert isinstance(session.calls[-1], EditMessageText)
    assert session.calls[-1].message_id == new_anchor
    await send(callback="ui:cancel")
    assert session.calls[-1].message_id == new_anchor
    assert session.messages[notification.message_id].text == notification.text


async def test_reply_and_refund_actions_use_existing_workflows(panel):
    ui, session, send = panel
    binding(ui.db)
    deal = order(ui.engine, ui.engine.market)
    remote = await ui.send_admin("42", "fallback", deal.id, "card:order:new")
    card = session.messages[int(remote)]
    action = next(
        b.callback_data
        for row in card.reply_markup.inline_keyboard
        for b in row
        if "refund" in b.callback_data
    )
    await send(callback=action, source=card)
    assert "Вернуть средства" in session.calls[-1].text
    assert ui.engine.market.deals[deal.id].status == "PAID"
    assert any(
        b.callback_data.startswith("refund_yes:")
        for row in session.calls[-1].reply_markup.inline_keyboard
        for b in row
    )
    await send(callback="ui:cancel")
    assert ui.engine.market.deals[deal.id].status == "PAID"


async def test_incoming_and_platform_notifications_remain_separate_and_escaped(panel):
    ui, session, send = panel
    binding(ui.db)
    deal = order(ui.engine, ui.engine.market)
    ui.db.set("admins", [42])
    await send("/start")
    old_anchor = ui.panels[(ui.bot.id, 42, 42)].message_id
    for system, message_id in [(False, "buyer-text"), (True, "platform-text")]:
        msg = Message(
            message_id, deal.chat_id, deal.buyer_id, "<b>unsafe</b> & secret-like text", now(), system
        )
        await Conversations(ui.engine).handle(msg)
        row = ui.db.one("SELECT * FROM outbox WHERE purpose='card:message' ORDER BY id DESC LIMIT 1")
        await ui.send_admin(row["target"], ui.db.decrypt(row["ciphertext"]), row["order_id"], row["purpose"])
        card = session.calls[-1]
        assert isinstance(card, SendMessage) and card.parse_mode == "HTML"
        assert "&lt;b&gt;unsafe&lt;/b&gt;" in card.text
        assert ("от Playerok" in card.text) == system
        assert ui.panels[(ui.bot.id, 42, 42)].message_id == old_anchor


def test_refund_card_survives_repeated_rolled_back_polls_and_is_unique(app):
    engine, db, market, _ = app
    db.set("admins", [42])
    binding(db)
    deal = order(engine, market)
    rolled = replace(deal, status="ROLLED_BACK")
    engine.accept(rolled)
    engine.accept(rolled)
    rows = db.all("SELECT * FROM outbox WHERE purpose='card:order:refund'")
    assert len(rows) == 1 and rows[0]["state"] == "pending"
    card = Cards(engine).order(deal.id, "refund")
    assert "Возврат средств" in card.text
    assert not any("refund:" in b.callback_data for row in card.keyboard.inline_keyboard for b in row)


async def test_daily_report_has_frozen_html_summary_and_excel(panel):
    ui, session, _ = panel
    ui.db.set("daily_report", True)
    ui.db.set("report_time", "00:00")
    ui.db.set("profile", {"username": "<owner>"})
    runtime = Runtime(SimpleNamespace(data_dir=ui.data_dir, admins=[42]), ui.db, ui.engine, ui)
    runtime.maintenance()
    row = ui.db.one("SELECT * FROM outbox WHERE purpose='report'")
    filename = ui.db.decrypt(row["ciphertext"])
    snapshot = ui.db.decrypt(ui.db.get("report_caption:" + filename))
    binding(ui.db)
    order(ui.engine, ui.engine.market)
    await ui.send_admin("42", filename, purpose="report")
    result = session.calls[-1]
    assert isinstance(result, SendDocument) and result.caption == snapshot
    assert "&lt;owner&gt;" in result.caption
    assert "Итоги за день" in result.caption and "Предварительная прибыль" in result.caption
    assert result.parse_mode == "HTML"
    assert {b.callback_data for row in result.reply_markup.inline_keyboard for b in row} == {
        "card:analytics",
        "card:reports",
    }


class Plain(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def test_daily_caption_fits_telegram_limit_with_long_seller_name(app):
    engine, db, _, _ = app
    db.set("profile", {"username": "🎮" * 200})
    caption = Cards(engine).daily(now() - 86400, now())
    parsed = Plain()
    parsed.feed(caption)
    assert len("".join(parsed.parts).encode("utf-16-le")) // 2 <= 1024


async def test_order_and_chat_panels_are_readable_html_and_keep_controls(panel):
    ui, session, send = panel
    binding(ui.db)
    deal = order(ui.engine, ui.engine.market, title="<name> & title")
    ui.engine.update(deal.id, error="<bad>")
    await send(callback="order:" + deal.id)
    response = session.calls[-1]
    assert response.parse_mode == "HTML" and "<b>Статус:</b> Оплачен" in response.text
    assert "<bad>" not in response.text and "&lt;name&gt;" in response.text
    assert "bad" not in response.text  # raw transport errors stay out of the admin UI
    assert any(
        b.callback_data == "refund:" + deal.id for row in response.reply_markup.inline_keyboard for b in row
    )
    await Conversations(ui.engine).handle(
        Message("history", deal.chat_id, deal.buyer_id, "<history>& text", now())
    )
    chat = ui.db.one("SELECT rowid rid FROM chats WHERE id=?", (deal.chat_id,))
    await send(callback="s:chat:" + str(chat["rid"]))
    response = session.calls[-1]
    assert isinstance(response, EditMessageText) and response.parse_mode == "HTML"
    assert "&lt;history&gt;&amp;" in response.text


async def test_legacy_outbox_notification_is_not_overwritten(panel):
    ui, session, send = panel
    ui.db.set("admins", [42])
    ui.db.notify("Legacy event", "legacy-card")
    row = ui.db.one("SELECT * FROM outbox WHERE channel='telegram' ORDER BY id DESC LIMIT 1")
    remote = await ui.send_admin("42", "Legacy event")
    ui.db.execute("UPDATE outbox SET remote_id=?,state='sent' WHERE id=?", (remote, row["id"]))
    source = session.messages[int(remote)]
    await send(callback="menu:home", source=source)
    assert isinstance(session.calls[-1], SendMessage)
    assert session.messages[source.message_id].text == source.text


async def test_not_modified_after_restart_remembers_panel_for_typed_form(panel):
    ui, session, send = panel
    await send("/start")
    source = ui.panels[(ui.bot.id, 42, 42)]
    ui.panels.clear()
    original = session.make_request

    async def request(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            raise TelegramBadRequest(method=method, message="message is not modified")
        return await original(bot, method, timeout)

    session.make_request = request
    await send(callback="menu:home", source=source)
    assert ui.panels[(ui.bot.id, 42, 42)].message_id == source.message_id
    session.make_request = original
    await send(callback="text:greeting")
    await send("Новый текст")
    assert isinstance(session.calls[-1], EditMessageText)
    assert session.calls[-1].message_id == source.message_id


async def test_buyer_name_from_message_is_used_before_any_purchase(panel):
    ui, session, _ = panel
    ui.db.set("admins", [42])
    msg = Message(
        "pre-sale", "chat-before-sale", "user-uuid", "Вопрос о товаре", now(), user_name="BuyerName"
    )
    await Conversations(ui.engine).handle(msg)
    row = ui.db.one("SELECT * FROM outbox WHERE purpose='card:message'")
    await ui.send_admin("42", ui.db.decrypt(row["ciphertext"]), purpose=row["purpose"])
    assert "BuyerName" in session.calls[-1].text
    assert "profile/BuyerName" in session.calls[-1].text


@pytest.mark.parametrize("action", ["s:reply:", "s:quick:choose:"])
async def test_old_reply_preview_cannot_follow_navigation_into_another_chat(panel, action):
    ui, session, send = panel
    for chat in ["A", "B"]:
        ui.db.execute("INSERT INTO chats(id) VALUES (?)", (chat,))
    await send("/reply A Reply for A")
    old = session.calls[-1].reply_markup.inline_keyboard[0][0].callback_data
    rid = ui.db.one("SELECT rowid rid FROM chats WHERE id='B'")["rid"]
    await send(callback=action + str(rid))
    await send(callback=old)
    assert not ui.db.all("SELECT * FROM outbox WHERE channel='playerok'")


async def test_invalid_new_reply_also_invalidates_previous_preview(panel):
    ui, session, send = panel
    for chat in ["A", "B"]:
        ui.db.execute("INSERT INTO chats(id) VALUES (?)", (chat,))
    await send("/reply A Reply for A")
    old = session.calls[-1].reply_markup.inline_keyboard[0][0].callback_data
    await send("/reply B " + "x" * 3501)
    await send(callback=old)
    assert not ui.db.all("SELECT * FROM outbox WHERE channel='playerok'")


async def test_automatic_replies_do_not_become_platform_notifications(panel):
    ui, _, _ = panel
    ui.db.set("admins", [42])
    await Conversations(ui.engine).handle(
        Message("auto", "chat", "buyer", "Автоответ", now(), system=True, auto_response=True)
    )
    assert not ui.db.all("SELECT * FROM outbox WHERE purpose='card:message'")
