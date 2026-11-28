"""One editable admin panel; event notifications remain separate messages."""

from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendDocument, SendMessage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .ui_routes import form_origin, navigation_markup, view_route
from .ui_style import style_message, styled_keyboard

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
    rendered: bool = False
    route: str = "menu:home"
    parent: str = "menu:home"
    db: object = None

    def persist(self):
        if self.db:
            key = "ui_nav:" + ":".join(map(str, self.key))
            value = {
                "route": self.route,
                "parent": self.parent,
                "message_id": self.anchor.message_id if self.anchor else None,
            }
            if self.db.get(key) != value:
                self.db.set(key, value)

    def remember(self, message):
        if isinstance(message, Message):
            self.anchor = message
            self.panels[self.key] = message
            self.persist()


class PanelNavigation(BaseMiddleware):
    def __init__(self, panels, db=None):
        self.panels, self.db = panels, db

    async def __call__(self, handler, event, data):
        state = data.get("state")
        if not state:
            return await handler(event, data)
        message = event.message if isinstance(event, CallbackQuery) else event
        key = (data["bot"].id, message.chat.id, event.from_user.id)
        nav_key = "ui_nav:" + ":".join(map(str, key))
        saved = self.db.get(nav_key, {}) if self.db else {}
        anchor = self.panels.get(key)
        saved_id = saved.get("message_id")
        if anchor is None and isinstance(saved_id, int) and isinstance(message, Message):
            anchor = message.model_copy(update={"message_id": saved_id})
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
        if notification and (anchor is None or anchor.message_id < message.message_id):
            # An action from a fresh event must open below that event, rather
            # than editing a much older panel and scrolling the chat backwards.
            # Repeated clicks reuse the newly created panel below the event.
            anchor = None
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
        action = (event.data or "") if isinstance(event, CallbackQuery) else ""
        route = view_route(action) or view_route(saved.get("route", "")) or "menu:home"
        if not isinstance(event, CallbackQuery) and (message.text or "").split()[0:1] in [
            ["/start"],
            ["/menu"],
        ]:
            route = "menu:home"
        parent = (
            form_origin(action, saved.get("route", "menu:home"))
            if action
            else old_data.get("_nav_origin", saved.get("parent", "menu:home"))
        )
        if action in {"ui:back", "ui:cancel"}:
            parent = view_route(saved.get("parent", "")) or "menu:home"
        panel = PanelContext(state, key, self.panels, anchor, route=route, parent=parent, db=self.db)
        panel.persist()
        token = context.set(panel)
        try:
            result = await handler(event, data)
            if isinstance(event, CallbackQuery) and not panel.rendered:
                await message.answer(
                    "↩️ Этот экран уже обновился\n\nОткройте нужный раздел из главного меню.",
                    reply_markup=buttons([("🏠 Главное меню", "menu:home")]),
                )
            new_state, new_data = await state.get_state(), await state.get_data()
            if (
                new_state
                and not new_state.startswith("ConnectionForm:")
                and not (old_state or "").startswith("ConnectionForm:")
                and not (isinstance(event, CallbackQuery) and event.data in {"ui:back", "ui:cancel"})
            ):

                def clean(d):
                    return {
                        k: v
                        for k, v in d.items()
                        if not k.startswith("_nav_")
                        and k not in {"reply_nonce", "reply_text", "shop_confirm", "external_confirm"}
                    }

                if (old_state, clean(old_data)) != (new_state, clean(new_data)):
                    stack = old_data.get("_nav_stack", [])[-7:]
                    prompt = old_data.get("_nav_prompt" if old_state else "_nav_screen")
                    if prompt:
                        stack.append(
                            {
                                "state": old_state,
                                "data": clean(old_data),
                                "prompt": prompt,
                            }
                        )
                    await state.update_data(_nav_stack=stack)
            return result
        finally:
            context.reset(token)


class NavigationMessages:
    async def __call__(self, make_request, bot, method):
        is_error = (getattr(method, "text", None) or "").startswith(
            ("Не ", "Невер", "Некоррект", "Ссылка не", "⚠️", "↩️ Откройте")
        )
        if isinstance(method, (SendMessage, SendDocument)):
            style_message(method)
        panel = context.get()
        if (
            not panel
            or not isinstance(method, (SendMessage, SendDocument))
            or str(method.chat_id) != str(panel.key[1])
        ):
            return await make_request(bot, method)
        state = panel.state
        panel.rendered = True
        if isinstance(method, SendDocument):
            if method.reply_markup:
                method.reply_markup = styled_keyboard(
                    navigation_markup(method.reply_markup, route=panel.route, parent=panel.parent)
                )
            return await make_request(bot, method)
        current = await state.get_state()
        method.reply_markup = styled_keyboard(
            navigation_markup(
                method.reply_markup,
                route=panel.route,
                parent=panel.parent,
                form=bool(current),
                connection=(current or "").startswith("ConnectionForm:"),
            )
        )
        if current:
            await state.update_data(_nav_origin=panel.parent)
        if isinstance(method, SendMessage) and not (current or "").startswith("ConnectionForm:"):
            prompt = {
                "route": panel.route,
                "parent": panel.parent,
                "text": method.text,
                "parse_mode": method.parse_mode if isinstance(method.parse_mode, str) else None,
                "entities": [e.model_dump(mode="json") for e in method.entities] if method.entities else None,
                "markup": method.reply_markup.model_dump(mode="json") if method.reply_markup else None,
            }
            await state.update_data(_nav_screen=prompt)
            if current:
                if not is_error or not (await state.get_data()).get("_nav_prompt"):
                    await state.update_data(_nav_prompt=prompt)
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
