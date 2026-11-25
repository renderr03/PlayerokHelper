"""Navigation for every admin form, including validation errors and file uploads."""

from contextvars import ContextVar
from copy import deepcopy

from aiogram import BaseMiddleware
from aiogram.methods import SendDocument, SendMessage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

context = ContextVar("panel_navigation", default=None)


def buttons(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=value)] for text, value in rows]
    )


class PanelNavigation(BaseMiddleware):
    async def __call__(self, handler, event, data):
        state = data.get("state")
        if not state:
            return await handler(event, data)
        old_state, old_data = await state.get_state(), deepcopy(await state.get_data())
        token = context.set(state)
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
        state = context.get()
        if state and isinstance(method, (SendMessage, SendDocument)):
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
                        "markup": method.reply_markup.model_dump(mode="json")
                        if method.reply_markup
                        else None,
                    }
                )
        return await make_request(bot, method)
