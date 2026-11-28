"""Human-facing labels. Persisted machine states and errors stay unchanged."""

STAGES = {
    "new": "⏳ В очереди",
    "rent_pending": "🛒 Закупаем аренду",
    "preparing": "📦 Готовим доступ",
    "sending": "✉️ Отправляем покупателю",
    "delivered": "✅ Выдан",
    "cancelled": "↩️ Отменён",
    "review": "⏸ Обработка приостановлена",
    "failed": "⚠️ Ошибка обработки",
    "historical": "🗂 Из истории",
    "ignored": "📦 Выдача не настроена",
    "wait_selection": "👤 Ждём выбор покупателя",
    "extending": "🔄 Продлеваем аренду",
    "extension_ready": "✅ Готов к продлению",
}


def stage_label(value):
    return STAGES.get(value, "⏳ Обрабатывается")


def issue_text(code):
    """Only known categories reach the UI; provider text may contain secrets."""
    code = str(code or "")
    messages = {
        "manual_pause": "Автовыдача этого лота выключена. Включите её в карточке лота, когда закончите настройку.",
        "external_provider_not_ready": "Откройте поставщика и включите закупки после настройки.",
        "external_provider_invalid_response": "Поставщик вернул неполные данные. Обновите баланс и каталог в его карточке.",
        "provider_insufficient_balance": "На балансе поставщика недостаточно средств для этого пакета. Пополните баланс или уменьшите количество.",
        "playerok_not_configured": "Добавьте аккаунт Playerok в разделе «Подключения».",
        "kosell_not_configured": "Добавьте ключ KOSell в разделе «Подключения».",
        "provider_not_found": "Поставщик удалён. Выберите подключение заново.",
        "provider_not_ready": "Откройте поставщика и включите закупки после настройки.",
        "provider_action_uncertain": "Ответ на запрос потерян. Откройте заказ у поставщика, чтобы узнать результат. Повторный запрос не отправлен.",
        "provider_action_not_confirmed": "Поставщик не вернул результат запроса. Откройте заказ в его кабинете. Повторный запрос не отправлен.",
        "provider_action_unavailable": "Это действие недоступно для выбранной услуги или состояния заказа.",
        "provider_service_or_quantity_unavailable": "Выберите доступную услугу и количество из каталога поставщика.",
        "provider_currency_mismatch": "Валюта баланса изменилась. Обновите настройки валюты у поставщика.",
        "provider_rejected": "Поставщик отклонил запрос. Откройте его кабинет, чтобы уточнить причину.",
        "lot_not_owned": "Выберите лот подключённого магазина.",
        "lot_not_active": "Лот сейчас не продаётся. Откройте его в Playerok и включите продажу.",
        "insufficient_balance": "Пополните баланс поставщика для продолжения выдачи.",
        "paused": "Выдача приостановлена в настройках магазина.",
        "stock_empty": "На складе закончились товары. Загрузите новые позиции.",
        "own_pool_empty": "В пуле нет свободных аккаунтов. Добавьте аккаунт или завершите подготовку после аренды.",
        "own_pool_not_configured": "Выберите пул аккаунтов для продления в карточке лота.",
        "own_rental_expired": "Аренда уже закончилась. Откройте новую покупку для выдачи доступа.",
        "own_extension_mismatch": "Выберите действующую аренду из пула этого лота.",
        "kosell_unavailable": "KOSell временно недоступен. Бот повторит подключение автоматически.",
        "product_missing": "Товар исчез из каталога KOSell. Выберите доступную игру для лота.",
        "no_accounts": "У поставщика закончились свободные аккаунты. Бот отслеживает пополнение.",
        "invalid_duration": "Укажите срок аренды в пределах выбранной услуги.",
        "invalid_rental_duration": "Укажите срок аренды в пределах выбранной услуги.",
        "purchase_limit_exceeded": "Цена закупки выше лимита лота. Измените лимит или дождитесь снижения цены.",
        "provider_purchase_limit_exceeded": "Цена закупки выше лимита лота. Измените лимит или дождитесь снижения цены.",
        "cost_limit_not_configured": "Укажите лимит закупки в карточке лота.",
        "configure_product_and_cost_limit": "Выберите игру и укажите лимит закупки в карточке лота.",
        "provider_cost_limit_required": "Укажите лимиты закупки в валюте поставщика и в рублях.",
        "provider_exchange_rate_required": "Добавьте актуальный курс валюты в настройках поставщика.",
        "provider_check_failed": "Поставщик сейчас недоступен. Обновите баланс и каталог в его карточке позже.",
        "provider_check_cooldown": "Следующее подключение доступно через минуту.",
        "provider_not_enabled_or_checked": "Откройте поставщика и нажмите «Запустить».",
    }
    if code in messages:
        return messages[code]
    if "security_check" in code or code.endswith("http_403"):
        return "Playerok или поставщик ограничил доступ с сервера. Откройте «Подключения», чтобы восстановить доступ."
    if "auth" in code or code.endswith("http_401"):
        return "Сессия или API-ключ больше не действует. Обновите подключение."
    if "rate_limit" in code or code.endswith("http_429"):
        return "Сервис ограничил частоту запросов. Попробуйте через минуту."
    if "network" in code or "timeout" in code or "http_5" in code:
        return "Сервис временно недоступен. Попробуйте позже."
    if "balance" in code:
        return "Откройте баланс поставщика и лимиты закупки в настройках лота."
    if "visibility" in code:
        return "Не удалось изменить продажу лота. Откройте его на Playerok; остальные лоты работают отдельно."
    if not code:
        return "—"
    return "Операция остановлена. Откройте заказ или подключение; подробности сохранены в диагностике."
