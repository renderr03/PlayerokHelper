import json
from html.parser import HTMLParser

from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from test_telegram_ui import panel as panel

from shopbot.models import now
from shopbot.ui_style import panel_html, style_message, styled_keyboard
from shopbot.updates import release_summary


class Plain(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.tags = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)


def test_shared_style_escapes_dynamic_data_and_fits_telegram_utf16_limit():
    text = 'Профиль <seller>\n\nТовар: <script> & "пример"\n' + "🎮" * 5000
    result = panel_html(text)
    plain = Plain()
    plain.feed(result)
    assert plain.tags == ["b"]
    assert "<script>" not in result and "&lt;script&gt;" in result
    assert "\n\n" in result
    assert len("".join(plain.parts).encode("utf-16-le")) // 2 <= 4096


def test_existing_html_and_entity_offsets_are_not_rewritten():
    html = SendMessage(chat_id=42, text="<b>Заказ</b>\n&lt;товар&gt;", parse_mode="HTML")
    original = html.text
    style_message(html)
    assert html.text == original and html.parse_mode == "HTML"
    entities = [MessageEntity(type="bold", offset=0, length=5)]
    text = SendMessage(chat_id=42, text="Заказ покупателя", entities=entities)
    style_message(text)
    assert text.text == "Заказ покупателя" and text.entities == entities
    assert html.link_preview_options.is_disabled and text.link_preview_options.is_disabled


def test_native_button_accents_preserve_actions_and_never_mutate_shared_markup():
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Обновить", callback_data="updates:offer")],
            [InlineKeyboardButton(text="Установить", callback_data="updates:install:nonce")],
            [InlineKeyboardButton(text="Отменить", callback_data="ui:cancel")],
            [InlineKeyboardButton(text="Инструкция", url="https://github.com/renderr03/PlayerokHelper")],
        ]
    )
    styled = styled_keyboard(markup)
    assert [row[0].style for row in styled.inline_keyboard] == ["primary", "success", "danger", None]
    assert markup.inline_keyboard[0][0].style is None
    assert styled.inline_keyboard[1][0].callback_data == "updates:install:nonce"
    assert styled.inline_keyboard[3][0].url == markup.inline_keyboard[3][0].url


async def test_main_menu_is_compact_escaped_and_status_does_not_claim_running(panel):
    ui, session, send = panel
    ui.db.set("profile", {"username": "<seller>"})
    ui.db.set("live_enabled", False)
    await send("/start")
    result = session.calls[-1]
    assert result.parse_mode == "HTML" and "&lt;seller&gt;" in result.text
    assert "⚪ Не запущена" in result.text and "🟢 Работает" not in result.text
    assert len(result.reply_markup.inline_keyboard) <= 8
    assert any(len(row) == 2 for row in result.reply_markup.inline_keyboard)
    assert result.link_preview_options.is_disabled
    ui.db.set("live_enabled", True)
    ui.db.set("last_poll", now())
    await send(callback="menu:home")
    assert "🟢 Работает" in session.calls[-1].text


async def test_back_restores_formatted_prompt_without_showing_literal_html(panel):
    _, session, send = panel
    await send("/bind")
    initial = session.calls[-1].text
    await send("02d57c00-d5bd-4f31-8a31-ab3b2fb2af13")
    modes = session.calls[-1].text
    await send(callback="s:bindtype:stock")
    await send(callback="ui:back")
    assert session.calls[-1].text == modes and session.calls[-1].parse_mode == "HTML"
    assert "&lt;b&gt;" not in session.calls[-1].text
    await send(callback="ui:back")
    assert session.calls[-1].text == initial


async def test_video_update_notification_opens_below_event_then_confirmation_reuses_panel(panel):
    ui, session, send = panel
    folder = ui.updates.folder
    folder.mkdir()
    (folder / "enabled.json").write_text('{"protocol": 1}')
    release = {
        "tag": "v9.0.0",
        "sha": "a" * 40,
        "body": "Исправлены ссылки.",
        "url": "https://github.com/renderr03/PlayerokHelper/releases/tag/v9.0.0",
    }
    ui.db.set("update_release", release)
    ui.db.set("admins", [42])
    await send("/start")
    old_panel = ui.panels[(ui.bot.id, 42, 42)].message_id
    ui.db.notify("Доступно обновление", "release:v9.0.0", purpose="update")
    row = ui.db.one("SELECT id FROM outbox WHERE purpose='update'")
    remote = await ui.send_admin("42", "🔔 PlayerokHelper v9.0.0\n\nИсправлены ссылки.", purpose="update")
    ui.db.execute("UPDATE outbox SET remote_id=?,state='sent' WHERE id=?", (remote, row["id"]))
    event = session.messages[int(remote)]
    notice = session.calls[-1]
    assert notice.link_preview_options.is_disabled
    assert any(b.url == release["url"] for r in notice.reply_markup.inline_keyboard for b in r)
    await send(callback="updates:home", source=event)
    assert isinstance(session.calls[-1], SendMessage)
    new_panel = ui.panels[(ui.bot.id, 42, 42)].message_id
    assert new_panel > event.message_id > old_panel
    await send(callback="updates:home", source=event)
    assert isinstance(session.calls[-1], EditMessageText)
    assert session.calls[-1].message_id == new_panel
    await send(callback="updates:offer")
    assert session.calls[-1].message_id == new_panel
    confirm = session.calls[-1].reply_markup.inline_keyboard[0][0]
    assert confirm.style == "success"
    await send(callback=confirm.callback_data)
    assert session.calls[-1].message_id == new_panel
    assert "Обновление поставлено в очередь" in session.calls[-1].text
    request = json.loads((folder / "request.json").read_text())
    assert request["tag"] == release["tag"] and request["sha"] == release["sha"]
    assert session.messages[event.message_id].text == event.text


def test_release_preview_omits_duplicate_report_links_and_markdown():
    preview = release_summary(
        "## Изменения\n- **Новое меню**\nОтчёт: https://github.com/renderr03/PlayerokHelper\n"
        "Подробности: https://github.com/renderr03/PlayerokHelper/releases\n"
    )
    assert "Новое меню" in preview and "https://" not in preview
    assert "##" not in preview and "**" not in preview
