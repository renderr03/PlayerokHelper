"""Separate KOSell wallets. No implicit exchange or addition of balances."""

from decimal import Decimal

from .models import ServiceError, rub


def number(value):
    n = Decimal(str(value))
    if not n.is_finite() or n < 0 or n > Decimal("1e12"):
        raise ValueError("Invalid balance")
    return n


def balance_text(balance):
    parts = []
    for currency in ("RUB", "USD"):
        value = balance.get("balance_" + currency.lower())
        parts.append(
            currency
            + ": "
            + (
                format(number(value), ".4f" if currency == "USD" else ".2f")
                if value is not None
                else "нет данных"
            )
        )
    return "\n".join(parts)


def wallet(db):
    result = db.get("kosell_currency", "RUB")
    if result not in {"RUB", "USD"}:
        raise ValueError("Invalid wallet")
    return result


def affordable(balance, quote, currency):
    key = "total_" + currency.lower()
    balance_key = "balance_" + currency.lower()
    if quote.get(key) is None or balance.get(balance_key) is None:
        raise ServiceError("kosell_currency_data_missing")
    return number(balance[balance_key]) >= number(quote[key])


def rate(quote):
    usd = number(quote["total_usd"])
    if usd <= 0:
        raise ServiceError("kosell_currency_data_missing")
    result = number(quote["total_rub"]) / usd
    if result <= 0:
        raise ServiceError("kosell_currency_data_missing")
    return result


def charge(db, order_id, currency, amount, exchange=None):
    native = number(amount)
    converted = (
        rub(native) if currency == "RUB" else rub(native * number(exchange)) if exchange is not None else None
    )
    db.execute(
        "INSERT OR REPLACE INTO kosell_charges VALUES (?,?,?,?,?)",
        (
            order_id,
            currency,
            str(native),
            str(exchange) if exchange is not None else None,
            int(currency == "USD"),
        ),
    )
    return converted
