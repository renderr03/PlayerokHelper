"""Canonical screen hierarchy, including parents for forms after a restart."""

import re

VIEWS = {
    "menu:home",
    "menu:auto",
    "menu:plugins",
    "menu:analytics",
    "menu:account",
    "menu:profile",
    "menu:help",
    "menu:attention",
    "auto:messages",
    "auto:replies",
    "auto:schedule",
    "analytics:settings",
    "analytics:items",
    "connect:home",
    "connect:playerok",
    "connect:smm",
    "connect:status",
    "setup:home",
    "bindings:all",
    "scopes",
    "templates",
    "promotion",
    "alerts:home",
    "updates:home",
    "uncertain:list",
    "expenses:list",
    "s:loyalty",
    "s:own-catalog",
    "v6:home",
    "v6:templates",
    "v6:rules",
    "v6:wallet",
    "v6:external",
}
VIEW_PATTERN = re.compile(
    r"(?:auto:event:[a-z_]+|(?:lot|order):[\w-]+|(?:orders|attention-orders):\d+|"
    r"s:(?:inbox:[a-z]+:\d+|chat:\d+|pool:\d+:\d+|account:\d+)|"
    r"x:(?:home:(?:stars|smm)|provider:\d+|orders:(?:stars|smm):\d+|order:\d+|services:\d+:\d+)|"
    r"v6:(?:templates:\d+|template:[a-f0-9]{12}|customers:\d+|customer:\d+|calendar:[17]:\d+|"
    r"stats:(?:1|7|30)|prices:\d+|price:\d+|rule:[a-f0-9]+))"
)


def view_route(action):
    if action == "v6:home":
        return "menu:home"
    if action == "templates":
        return "auto:messages"
    return action if action in VIEWS or VIEW_PATTERN.fullmatch(action) else None


def parent_route(route):
    if route in {"auto:messages", "auto:replies", "auto:schedule", "promotion", "alerts:home", "scopes"}:
        return "menu:auto"
    if route.startswith("auto:event:"):
        return "auto:messages"
    if route in {"connect:home", "connect:status", "updates:home"}:
        return "menu:account"
    if route.startswith("connect:"):
        return "connect:home"
    if route in {"analytics:settings", "analytics:items"} or route.startswith("v6:stats:"):
        return "menu:analytics"
    if route.startswith("lot:") or route.startswith(("v6:template", "v6:price")):
        return "bindings:all"
    if route.startswith("order:"):
        return "orders:0"
    if route.startswith("v6:customers:") or route.startswith("orders:") or route == "menu:attention":
        return "menu:profile"
    if route.startswith("v6:customer:"):
        return "v6:customers:0"
    if route.startswith("s:chat:"):
        return "s:inbox:all:0"
    if route.startswith("v6:rule"):
        return "auto:replies"
    if route.startswith(("s:pool:", "s:account:", "v6:calendar:")):
        return "s:own-catalog"
    if route.startswith("x:home:") or route in {"v6:wallet", "s:own-catalog", "s:loyalty"}:
        return "menu:plugins"
    if route.startswith("x:provider:"):
        return "menu:plugins"
    if route.startswith(("x:services:", "x:orders:", "x:order:")):
        return "menu:plugins"
    if route in {"uncertain:list", "expenses:list"} or route.startswith("attention-orders:"):
        return "menu:attention"
    return "menu:home"


def form_origin(action, current="menu:home"):
    if action == "edit:rules":
        return "auto:replies"
    if action == "edit:work_hours":
        return "auto:schedule"
    if action in {"edit:action_limit", "edit:daily_budget"}:
        return "promotion"
    if action in {"edit:fee_percent", "edit:report_time"}:
        return "analytics:settings"
    if action == "edit:report_filter":
        return "menu:analytics"
    if action.startswith("text:"):
        return "auto:event:" + action.split(":", 1)[1]
    if action.startswith("lotedit:"):
        return "lot:" + action.split(":", 2)[2]
    if action.startswith("stock:"):
        return "lot:" + action.split(":", 1)[1]
    if action.startswith("connect:key:"):
        return current if current in {"connect:smm", "connect:home"} else "connect:home"
    if action in {"connect:token", "connect:email", "connect:session"}:
        return "connect:playerok"
    if action.startswith("x:new:") or action == "bind:new":
        return "bindings:all" if action == "bind:new" else "x:home:" + action.rsplit(":", 1)[1]
    return view_route(current) or "menu:home"


def navigation_markup(markup, *, route, parent, form=False, connection=False):
    """One footer; de-duplicate actions without touching business buttons."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows, seen, explicit_back = [], set(), None
    for row in markup.inline_keyboard if isinstance(markup, InlineKeyboardMarkup) else []:
        kept = []
        for button in row:
            action = button.callback_data
            label = button.text.lstrip()
            if action == "ui:cancel" and not form and explicit_back is None:
                explicit_back = parent
            back = (
                label.startswith(("‹", "← Назад"))
                or (len(label) < 24 and label.endswith(("Отмена", "Отменить")))
            ) and action
            if back and action not in {"ui:back", "ui:cancel", "v6:home"} and explicit_back is None:
                explicit_back = action
            if action in {"menu:home", "ui:back", "ui:cancel"} or back:
                continue
            key = (action, button.url) if action or button.url else button.text
            if key not in seen:
                kept.append(button.model_copy(deep=True))
                seen.add(key)
        if kept:
            rows.append(kept)
    if form:
        back = (explicit_back or parent) if connection else "ui:back"
        rows.append(
            [
                InlineKeyboardButton(text="‹ Назад", callback_data=back),
                InlineKeyboardButton(text="✖ Отменить", callback_data="ui:cancel"),
            ]
        )
    elif route != "menu:home" or not {
        "menu:auto",
        "menu:profile",
        "menu:analytics",
        "menu:plugins",
        "menu:account",
    }.issubset({b.callback_data for row in rows for b in row}):
        back = explicit_back or parent_route(route)
        rows = [[b for b in row if b.callback_data != back] for row in rows]
        rows = [row for row in rows if row]
        rows.append([InlineKeyboardButton(text="‹ Назад", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
