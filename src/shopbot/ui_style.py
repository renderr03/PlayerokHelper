"""Telegram HTML headings, restrained emoji and native button accents."""

from __future__ import annotations

from copy import deepcopy
from html import escape

from aiogram.client.default import Default
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions

from .presentation import clip


def has_icon(text):
    return bool(text) and (ord(text[0]) >= 0x1F000 or 0x2190 <= ord(text[0]) <= 0x2BFF)


def panel_html(value, *, limit=3900):
    text = clip(value, limit).strip()
    if not text:
        return ""
    first, _, rest = text.partition("\n")
    if first.startswith(("Не ", "Невер", "Некоррект", "Ссылка не", "Подтверждение устарело", "Проверьте")):
        return "<b>💡 Следующий шаг</b>\n\n" + escape(text)
    if len(first) > 100:
        title = "💬 PlayerokHelper"
        if first.startswith(("Пришлите", "Введите", "Отправьте", "Выберите")):
            title = "✍️ Настройка"
        elif first.startswith(("Обновление", "Установить")):
            title = "⬆️ Обновление бота"
        elif first.startswith(("Нахожу", "Проверяю", "Загружаю")):
            title = "⏳ Подключаемся"
        return f"<b>{title}</b>\n\n{escape(text)}"
    icon = "✍️" if first.startswith(("Пришлите", "Введите", "Отправьте", "Выберите")) else "✨"
    if first.startswith(("Нахожу", "Проверяю", "Загружаю")):
        icon = "⏳"
    title = first if has_icon(first) else icon + " " + first
    return "<b>" + escape(title) + "</b>" + ("\n\n" + escape(rest.lstrip()) if rest.strip() else "")


def styled_keyboard(markup):
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup
    result = deepcopy(markup)
    labels = {
        "Проверить лот": "🧪 Проверить лот",
        "Другой лот": "🔎 Другой лот",
        "Выбрать лот": "🔎 Выбрать лот",
        "Продолжить настройку": "✨ Продолжить настройку",
        "В главное меню": "🏠 В главное меню",
        "Отменить": "✖ Отменить",
        "Отмена": "✖ Отмена",
        "Telegram Stars": "⭐ Telegram Stars",
        "SMM-услуги": "💜 SMM-услуги",
        "Настроить поставщика": "🔌 Настроить поставщика",
        "Статус подключения": "🔎 Статус подключения",
        "Инструкция на GitHub": "📖 Инструкция на GitHub",
        "Подключить аккаунт": "🔌 Подключить аккаунт",
        "Настроить магазин": "🧭 Настроить магазин",
        "Загрузить историю": "🗂 Загрузить историю",
        "Включить": "▶️ Включить",
        "Выключить": "⏸ Выключить",
        "Запустить": "▶️ Запустить",
        "Остановить": "⏸ Остановить",
        "Лимит операции": "💰 Лимит операции",
        "Суточный бюджет": "🗓 Суточный бюджет",
        "Настроить лоты": "📦 Настроить лоты",
        "Каталог услуг": "📚 Каталог услуг",
        "Разрешённые сайты": "🌐 Разрешённые сайты",
        "Количество": "🔢 Количество",
        "Инструкция": "📝 Инструкция",
        "Поиск": "🔎 Поиск",
        "Отправить": "✉️ Отправить",
        "Отправить запрос": "✉️ Отправить запрос",
        "Подтверждаю возврат": "↩️ Подтверждаю возврат",
        "Пауза": "⏸ Пауза",
        "Снять паузу": "▶️ Снять паузу",
        "Тексты этого лота": "💬 Тексты этого лота",
        "Настройки сообщений этого лота": "💬 Сообщения этого лота",
        "Курс для учёта в рублях": "💱 Курс учёта в рублях",
        "Порог низкого баланса": "🔔 Порог баланса",
        "Как получить токен": "📖 Как получить токен",
        "VingBoost": "💜 VingBoost",
        "Twiboost": "💜 Twiboost",
        "Vexboost": "💜 Vexboost",
        "JustAnotherPanel": "💜 JustAnotherPanel",
    }
    for row in result.inline_keyboard:
        for button in row:
            button.text = labels.get(button.text, button.text)
            for prefix, icon in (
                ("Подтверждение получателя:", "👤"),
                ("Автовозврат:", "↩️"),
                ("Уведомления:", "🔔"),
            ):
                if button.text.startswith(prefix):
                    button.text = icon + " " + button.text
            if button.style is not None:
                continue
            action = button.callback_data or ""
            if action.startswith(("updates:install:", "connect:visibility:run:", "reply_send:", "sent:")):
                button.style = "success"
            elif action == "ui:cancel" or action.startswith(("refund:", "refund_yes:", "card:refund:")):
                button.style = "danger"
            elif action.startswith("x:confirm:"):
                button.style = "primary"
            elif button.text.startswith("‹"):
                button.style = "primary"
            elif action in {
                "connect:playerok",
                "connect:home",
                "setup:home",
                "updates:offer",
                "updates:check",
                "bind:new",
            }:
                button.style = "primary"
    return result


def style_message(method):
    """Escape plain admin text; already formatted cards/entities stay intact."""
    method.reply_markup = styled_keyboard(method.reply_markup)
    if hasattr(method, "link_preview_options"):
        method.link_preview_options = LinkPreviewOptions(is_disabled=True)
    field = "text" if hasattr(method, "text") else "caption"
    entities = getattr(method, "entities" if field == "text" else "caption_entities", None)
    value = getattr(method, field, None)
    if value and not entities and (method.parse_mode is None or isinstance(method.parse_mode, Default)):
        setattr(method, field, panel_html(value, limit=3900 if field == "text" else 900))
        method.parse_mode = "HTML"
    return method
