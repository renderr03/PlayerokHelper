"""One editable admin panel; event notifications remain separate messages."""

from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendDocument, SendMessage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

context = ContextVar("panel_navigation", default=None)


def buttons(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=value)] for text, value in rows]
    )


@dataclass
class PanelContext:
    state: object
    key: tuple
    panels: dict
    anchor: Message | None

    def remember(self, message):
        if isinstance(message, Message):
            self.anchor = message
            self.panels[self.key] = message


class PanelNavigation(BaseMiddleware):
    def __init__(self, panels, db=None):
        self.panels, self.db = panels, db

    async def __call__(self, handler, event, data):
        state = data.get("state")
        if not state:
            return await handler(event, data)
        message = event.message if isinstance(event, CallbackQuery) else event
        key = (data["bot"].id, message.chat.id, event.from_user.id)
        anchor = self.panels.get(key)
        notification = isinstance(event, CallbackQuery) and (
            (event.data or "").startswith("card:")
            or (
                self.db is not None
                and self.db.one(
                    "SELECT id FROM outbox WHERE channel='telegram' AND target=? AND remote_id=? LIMIT 1",
                    (str(message.chat.id), str(message.message_id)),
                )
            )
        )
        if (
            isinstance(event, CallbackQuery)
            and not notification
            and isinstance(message, Message)
            and message.text
        ):
            # Resume after a restart, but an older menu cannot replace the newer
            # active panel. Notification actions use their own card: namespace.
            if anchor is None or message.message_id >= anchor.message_id:
                anchor = message
        old_state, old_data = await state.get_state(), deepcopy(await state.get_data())
        token = context.set(PanelContext(state, key, self.panels, anchor))
        try:
            result = await handler(event, data)
            new_state, new_data = await state.get_state(), await state.get_data()
            if (
                old_state
                and new_state
                and not new_state.startswith("ConnectionForm:")
                and not old_state.startswith("ConnectionForm:")
                and not (isinstance(event, CallbackQuery) and event.data in {"ui:back", "ui:cancel"})
            ):

                def clean(d):
                    return {k: v for k, v in d.items() if not k.startswith("_nav_")}

                if (old_state, clean(old_data)) != (new_state, clean(new_data)):
                    stack = old_data.get("_nav_stack", [])[-7:]
                    stack.append(
                        {"state": old_state, "data": clean(old_data), "prompt": old_data.get("_nav_prompt")}
                    )
                    await state.update_data(_nav_stack=stack)
            return result
        finally:
            context.reset(token)


class NavigationMessages:
    async def __call__(self, make_request, bot, method):
        panel = context.get()
        if (
            not panel
            or not isinstance(method, (SendMessage, SendDocument))
            or str(method.chat_id) != str(panel.key[1])
        ):
            return await make_request(bot, method)
        state = panel.state
        current = await state.get_state()
        markup = method.reply_markup
        rows = deepcopy(markup.inline_keyboard) if isinstance(markup, InlineKeyboardMarkup) else []
        callbacks = {b.callback_data for row in rows for b in row}
        if current:
            if not current.startswith("ConnectionForm:"):
                if "ui:back" not in callbacks:
                    rows.append([InlineKeyboardButton(text="‹ Назад", callback_data="ui:back")])
                if "ui:cancel" not in callbacks:
                    rows.append([InlineKeyboardButton(text="✖ Отменить", callback_data="ui:cancel")])
        elif not callbacks.intersection({"menu:home", "ui:cancel"}):
            rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")])
        if rows:
            method.reply_markup = InlineKeyboardMarkup(inline_keyboard=rows)
        if current and isinstance(method, SendMessage) and not current.startswith("ConnectionForm:"):
            await state.update_data(
                _nav_prompt={
                    "text": method.text,
                    "markup": method.reply_markup.model_dump(mode="json") if method.reply_markup else None,
                }
            )
        if isinstance(method, SendDocument):
            # Files are separate deliverables, never the anchor of the text panel.
            return await make_request(bot, method)
        if panel.anchor:
            edit = EditMessageText(
                chat_id=method.chat_id,
                message_id=panel.anchor.message_id,
                text=method.text,
                parse_mode=method.parse_mode,
                entities=method.entities,
                link_preview_options=method.link_preview_options,
                disable_web_page_preview=method.disable_web_page_preview,
                reply_markup=method.reply_markup,
            )
            try:
                result = await make_request(bot, edit)
                panel.remember(result)
                return result
            except TelegramBadRequest as error:
                reason = error.message.lower()
                if "message is not modified" in reason:
                    panel.remember(panel.anchor)
                    return panel.anchor
                if not any(
                    value in reason
                    for value in (
                        "message to edit not found",
                        "message can't be edited",
                        "message can not be edited",
                        "there is no text in the message to edit",
                    )
                ):
                    raise
                # Known absent/uneditable message: create exactly one replacement.
                # Unknown errors/timeouts never cause a duplicate send.
        result = await make_request(bot, method)
        panel.remember(result)
        return result
