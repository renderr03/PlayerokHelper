"""Visibility policy and allowlisted diagnostic reasons for external lots."""

import re

EXTERNAL_MODES = {"stars", "smm"}
EXTERNAL_ISSUES = {
    "external_provider_not_ready",
    "external_provider_invalid_response",
    "provider_not_enabled_or_checked",
    "provider_check_cooldown",
    "provider_check_failed",
    "provider_exchange_rate_required",
    "provider_cost_limit_required",
    "provider_service_or_quantity_unavailable",
    "provider_currency_mismatch",
    "provider_purchase_limit_exceeded",
    "provider_insufficient_balance",
    "provider_network",
    "provider_rejected",
    "provider_invalid_json",
    "provider_response_too_large",
    "provider_invalid_catalog",
    "provider_invalid_amount",
    "provider_invalid_currency",
    "provider_contract_changed",
}
LISTING_ISSUES = EXTERNAL_ISSUES | {
    "manual_pause",
    "own_pool_empty",
    "own_pool_not_configured",
    "stock_empty",
    "kosell_unavailable",
    "product_missing",
    "cost_limit_not_configured",
    "no_accounts",
    "invalid_duration",
    "purchase_limit_exceeded",
    "insufficient_rub_balance",
    "insufficient_usd_balance",
    "quote_unavailable",
}


def auto_hide_external(db, item_id):
    # Absence on existing installations also means opt-out.
    return db.get("external_auto_hide:" + item_id, False) is True


def safe_reason(value):
    if value is None:
        return None
    return (
        value
        if isinstance(value, str)
        and (value in LISTING_ISSUES or re.fullmatch(r"provider_http_[45][0-9]{2}", value))
        else "external_provider_invalid_response"
    )
