"""Regressions from the seller's Back-button video and reference layout."""

import pytest
from aiogram.methods import EditMessageText, SendMessage
from conftest import binding
from test_telegram_ui import callbacks
from test_telegram_ui import panel as panel


def footer(result):
    return [b for row in result.reply_markup.inline_keyboard for b in row if b.text.startswith("‹")]


async def test_video_rules_back_and_cancel_return_to_autoreplies_not_connections(panel):
    ui, session, send = panel
    await send("/start")
    for action in ["menu:auto", "auto:replies", "edit:rules"]:
        await send(callback=action)
    for _ in range(3):
        await send("Неверный формат")
    await send(callback="ui:back")
    assert "Автоответчик" in session.calls[-1].text
    assert "edit:rules" in callbacks(session.calls[-1])
    assert "connect:home" not in callbacks(session.calls[-1])
    await send(callback="edit:rules")
    await send(callback="ui:cancel")
    assert "Автоответчик" in session.calls[-1].text
    await send(callback=footer(session.calls[-1])[0].callback_data)
    assert "Сообщения покупателям" in str(session.calls[-1].reply_markup)
    await send(callback=footer(session.calls[-1])[0].callback_data)
    assert "Мои лоты" in str(session.calls[-1].reply_markup)
    assert not footer(session.calls[-1])
    assert sum(isinstance(c, SendMessage) for c in session.calls) == 1
    assert not ui.db.get("rules")


@pytest.mark.parametrize(
    "action,parent",
    [
        ("edit:rules", "auto:replies"),
        ("edit:work_hours", "auto:schedule"),
        ("edit:report_time", "analytics:settings"),
        ("text:greeting", "auto:event:greeting"),
        ("connect:token", "connect:playerok"),
    ],
)
@pytest.mark.parametrize("button", ["ui:back", "ui:cancel"])
async def test_form_navigation_survives_restart_without_restoring_secrets(panel, action, parent, button):
    ui, session, send = panel
    await send("/start")
    await send(callback=action)
    anchor = ui.panels[(ui.bot.id, 42, 42)]
    await send.fsm.clear()
    ui.panels.clear()
    await send(callback=button, source=anchor)
    result = session.calls[-1]
    assert isinstance(result, EditMessageText) and result.message_id == anchor.message_id
    value = ui.db.get("ui_nav:" + ":".join(map(str, (ui.bot.id, 42, 42))))
    assert value["route"] == parent
    assert set(value) == {"route", "parent", "message_id"}
    assert not await send.fsm.get_state()
    assert "Ввод отменён" not in result.text


async def test_event_cards_keep_feature_and_template_settings_together(panel):
    ui, session, send = panel
    await send(callback="auto:messages")
    assert all(not c.startswith("feature:") for c in callbacks(session.calls[-1]))
    await send(callback="auto:event:greeting")
    assert {"feature:greeting", "text:greeting", "auto:messages"} == callbacks(session.calls[-1])
    await send(callback="feature:greeting")
    assert ui.db.feature("greeting")
    assert {"feature:greeting", "text:greeting", "auto:messages"} == callbacks(session.calls[-1])
    await send(callback="text:greeting")
    await send("Добро пожаловать, {buyer}!")
    assert "Добро пожаловать" in session.calls[-1].text
    assert "text:greeting" in callbacks(session.calls[-1])
    assert not await send.fsm.get_state()


async def test_restarting_then_clicking_old_menu_edits_latest_panel_below_notification(panel):
    ui, session, send = panel
    ui.db.set("admins", [42])
    await send("/start")
    old = ui.panels[(ui.bot.id, 42, 42)]
    remote = await ui.send_admin("42", "Новость", purpose="update")
    event = session.messages[int(remote)]
    ui.db.notify("Новость", "nav-test-notice", purpose="update")
    ui.db.execute("UPDATE outbox SET remote_id=? WHERE purpose='update'", (remote,))
    await send(callback="updates:home", source=event)
    fresh = ui.panels[(ui.bot.id, 42, 42)]
    assert fresh.message_id > event.message_id > old.message_id
    ui.panels.clear()
    await send.fsm.clear()
    await send(callback="menu:auto", source=old)
    assert isinstance(session.calls[-1], EditMessageText)
    assert session.calls[-1].message_id == fresh.message_id
    assert session.messages[event.message_id].text == event.text


async def test_footer_has_one_back_one_cancel_and_no_tool_hub_on_form(panel):
    ui, session, send = panel
    binding(ui.db, module="stock")
    await send(callback="lot:item1")
    await send(callback="lotedit:hours:item1")
    result = session.calls[-1]
    actions = [b.callback_data for row in result.reply_markup.inline_keyboard for b in row]
    assert actions.count("ui:back") == actions.count("ui:cancel") == 1
    assert "v6:home" not in actions and "menu:home" not in actions
    assert len(result.reply_markup.inline_keyboard[-1]) == 2
    assert result.reply_markup.inline_keyboard[-1][0].style == "primary"
    await send(callback="ui:cancel")
    assert "lotedit:hours:item1" in callbacks(session.calls[-1])
    await send(callback="menu:home")
    actions = callbacks(session.calls[-1])
    assert not {"v6:home", "setup:home", "connect:home", "s:loyalty", "updates:home"} & actions
    assert all(len(row) == 1 for row in session.calls[-1].reply_markup.inline_keyboard)


async def test_business_confirmations_remain_required_when_navigation_is_simplified(panel):
    ui, session, send = panel
    await send(callback="analytics:settings")
    await send(callback="edit:fee_percent")
    await send(callback="ui:cancel")
    assert "fee_percent" not in (await send.fsm.get_data()).get("edit", "")
    assert not ui.db.all("SELECT * FROM provider_orders")
    assert not ui.engine.market.transitions and not ui.engine.kosell.rents
