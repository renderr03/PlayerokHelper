"""Bounded HTTP adapters. Paid requests are never retried by the transport."""

from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal, InvalidOperation

import httpx

from .models import ServiceError, timestamp

PRESETS = {
    "split": ("split", "https://api.split.tg", "Split"),
    "twiboost": ("smm", "https://twiboost.com/api/v2", "Twiboost"),
    "vingboost": ("smm", "https://vingboost.com/api/v2", "VingBoost"),
    "vexboost": ("smm", "https://vexboost.ru/api/v2", "Vexboost"),
    "jap": ("smm", "https://justanotherpanel.com/api/v2", "JustAnotherPanel"),
}


# Official VingBoost developer reference: these task types use only
# service + link + quantity. Comments/votes/comment reactions need context
# our form does not collect; never silently treat them as a Default order.
VINGBOOST_SIMPLE_TYPES = {
    "default",
    "like",
    "subscribe",
    "dislike",
    "repost",
    "friend",
    "retweet",
    "follow",
    "favorite",
}


def amount(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result > Decimal("1e18"):
            raise ValueError()
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise ServiceError("provider_invalid_amount") from None


def currency(value):
    value = str(value).upper()
    value = "USD" if value == "USDT" else value
    if not re.fullmatch(r"[A-Z]{3,8}", value):
        raise ServiceError("provider_invalid_currency")
    return value


class ProviderClient:
    def __init__(self, preset, token, *, transport=None):
        self.preset = preset
        self.kind, url, _ = PRESETS[preset]
        self.http = httpx.AsyncClient(
            base_url=url if self.kind == "split" else "",
            timeout=20,
            follow_redirects=False,
            transport=transport,
            headers={"Authorization": "Bearer " + token} if self.kind == "split" else {},
        )
        self.url, self.token = url, token

    async def close(self):
        await self.http.aclose()

    async def request(self, method, path="", *, paid=False, **kwargs):
        try:
            async with self.http.stream(
                method, path if self.kind == "split" else self.url, **kwargs
            ) as response:
                if response.status_code != 200:
                    raise ServiceError("provider_http_" + str(response.status_code), ambiguous=paid)
                chunks = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 4_000_000:
                        raise ServiceError("provider_response_too_large", ambiguous=paid)
                    chunks.append(chunk)
        except httpx.HTTPError:
            raise ServiceError("provider_network", ambiguous=paid) from None
        try:
            data = json.loads(b"".join(chunks), parse_float=Decimal)
        except ValueError:
            raise ServiceError("provider_invalid_json", ambiguous=paid) from None
        if isinstance(data, dict) and (data.get("error") or data.get("ok") is False):
            # Even error responses to paid calls are held for reconciliation.
            raise ServiceError("provider_rejected", ambiguous=paid)
        return data

    async def split(self, method, path, *, paid=False, **kwargs):
        data = await self.request(method, path, paid=paid, **kwargs)
        if (
            not isinstance(data, dict)
            or data.get("ok") is not True
            or not isinstance(data.get("message"), dict)
        ):
            raise ServiceError("provider_contract_changed", ambiguous=paid)
        return data["message"]

    async def smm(self, action, *, paid=False, **params):
        return await self.request("POST", paid=paid, data={"key": self.token, "action": action, **params})

    async def balance(self):
        if self.kind == "split":
            data = await self.split("GET", "/balance/")
            return {
                "TON": amount(data["ton_balance"]["real"]["available"]) / 10**9,
                "USD": amount(data["usdt_balance"]["available"]) / 10**9,
            }
        data = await self.smm("balance")
        return {currency(data["currency"]): amount(data["balance"])}

    async def services(self):
        rows = await self.smm("services")
        if not isinstance(rows, list) or len(rows) > 20000:
            raise ServiceError("provider_invalid_catalog")
        result = []
        for row in rows:
            if not isinstance(row, dict):
                raise ServiceError("provider_invalid_catalog")
            supported = (
                (isinstance(row.get("type"), str) and row["type"].casefold() in VINGBOOST_SIMPLE_TYPES)
                if self.preset == "vingboost"
                else row.get("type") == "Default"
            )
            if not supported:
                continue
            result.append(
                {
                    "id": str(int(row["service"])),
                    "name": str(row["name"])[:150],
                    "rate": str(amount(row["rate"])),
                    "min": int(row["min"]),
                    "max": int(row["max"]),
                    "cancel": row.get("cancel") is True,
                    "refill": row.get("refill") is True,
                }
            )
            if (
                len(result[-1]["id"]) > 20
                or int(result[-1]["id"]) <= 0
                or not 1 <= result[-1]["min"] <= result[-1]["max"]
            ):
                raise ServiceError("provider_invalid_catalog")
        return result

    async def recipient(self, username):
        data = await self.split("POST", "/recipients/stars", json={"username": username})
        if not data.get("recipient") or not data.get("name"):
            raise ServiceError("recipient_not_found")
        return {"name": " ".join(str(data["name"]).split())[:150], "recipient": str(data["recipient"])}

    async def quote(self, binding, destination):
        balances = await self.balance()
        if self.kind == "split":
            recipient = await self.recipient(destination)
            label = recipient["name"]
            data = await self.split(
                "POST",
                "/buy/estimate",
                json={
                    "product": "stars",
                    "quantity": binding["quantity"],
                    "method": binding["payment_method"],
                    "username": destination,
                },
            )
            unit = currency(data["currency"])
            if unit != ("TON" if binding["payment_method"] == "balance" else "USD"):
                raise ServiceError("provider_currency_mismatch")
            cost = amount(data["amount"])
        else:
            service = next((r for r in await self.services() if r["id"] == binding["service_id"]), None)
            if not service or not service["min"] <= binding["quantity"] <= service["max"]:
                raise ServiceError("provider_service_or_quantity_unavailable")
            if len(balances) != 1:
                raise ServiceError("provider_currency_mismatch")
            unit = next(iter(balances))
            cost = amount(service["rate"]) * binding["quantity"] / 1000
            label = service["name"]
        return {
            "amount": str(cost),
            "currency": unit,
            "balance": str(balances.get(unit, 0)),
            "label": label,
            **({"recipient_ref": recipient["recipient"]} if self.kind == "split" else {}),
        }

    async def create(self, binding, destination, reference):
        if self.kind == "split":
            data = await self.split(
                "POST",
                "/buy/stars",
                paid=True,
                json={
                    "username": destination,
                    "quantity": binding["quantity"],
                    "payment_method": binding["payment_method"],
                },
            )
            invoice = data.get("invoice") or {}
            remote = invoice.get("id")
            if not remote:
                raise ServiceError("provider_missing_order_id", ambiguous=True)
            try:
                return str(uuid.UUID(str(remote)))
            except ValueError:
                raise ServiceError("provider_invalid_order_id", ambiguous=True) from None
        data = await self.smm(
            "add", paid=True, service=binding["service_id"], link=destination, quantity=binding["quantity"]
        )
        if not isinstance(data, dict):
            raise ServiceError("provider_invalid_order_id", ambiguous=True)
        remote = str(data.get("order", ""))
        if not remote.isdigit() or int(remote) <= 0:
            raise ServiceError("provider_missing_order_id", ambiguous=True)
        return remote

    async def status(self, remote, snapshot):
        if self.kind == "split":
            # payment_succeeded alone is NOT proof of delivery. Reconcile the invoice.
            for offset in range(0, 1000, 100):
                data = await self.split("GET", "/user/invoices", params={"limit": 100, "offset": offset})
                rows = data["items"]
                row = next((r for r in rows if str(r["id"]) == remote), None)
                if row:
                    product, payment = row["data"], row["payment"]
                    if (
                        product["type"] != "stars"
                        or int(product["quantity"]) != snapshot["quantity"]
                        or product["username"].lstrip("@").casefold() != snapshot["destination"].casefold()
                    ):
                        raise ServiceError("provider_order_mismatch")
                    state = payment["status"]
                    if state == "completed" and payment.get("completed_at"):
                        if not isinstance(payment["completed_at"], str):
                            raise ServiceError("provider_invalid_delivery_time")
                        timestamp(payment["completed_at"])
                        state = "completed"
                    elif state == "completed":
                        state = "processing"
                    elif state in {
                        "failed",
                        "cancelled",
                        "reversed",
                        "chargeback",
                        "wrong_amount",
                        "freezed",
                        "stale",
                    }:
                        state = "review"
                    else:
                        state = "processing"
                    return {
                        "state": state,
                        "amount": str(amount(payment["amount"])),
                        "currency": currency(payment["currency"]),
                        "remains": None,
                        "refunded": "0",
                        "cost_known": state == "completed" or bool(payment.get("paid_at")),
                    }
                if len(rows) < 100:
                    break
            raise ServiceError("provider_invoice_not_found")
        data = await self.smm("status", order=remote)
        mapping = {
            "pending": "processing",
            "in progress": "processing",
            "processing": "processing",
            "completed": "completed",
            "partial": "partial",
            "canceled": "cancelled",
            "cancelled": "cancelled",
        }
        if self.preset == "vingboost":
            mapping.update({"awaiting": "processing", "fail": "review"})
        state = mapping.get(str(data["status"]).casefold(), "review")
        cost, unit = amount(data["charge"]), currency(data["currency"])
        remains = int(data["remains"])
        if not 0 <= remains <= snapshot["quantity"]:
            raise ServiceError("provider_invalid_remains")
        if state == "completed" and remains:
            state = "review"
        return {
            "state": state,
            "amount": str(cost),
            "currency": unit,
            "remains": remains,
            "refunded": "0",
            "cost_known": True,
            "zero_delivery": state == "cancelled" and remains == snapshot["quantity"] and cost == 0,
        }

    async def cancel_or_refill(self, remote, action):
        if self.kind != "smm" or action not in {"cancel", "refill"}:
            raise ServiceError("provider_action_unavailable")
        # These requests also have a persisted intent in the application.
        result = await self.smm(
            action,
            paid=True,
            **(
                {"orders": remote} if action == "cancel" and self.preset != "vingboost" else {"order": remote}
            ),
        )
        if self.preset == "vingboost":
            if not isinstance(result, dict):
                raise ServiceError("provider_action_not_confirmed", ambiguous=True)
            if action == "cancel":
                if result.get("ok") is not True and result.get("ok") != "true":
                    raise ServiceError("provider_action_not_confirmed", ambiguous=True)
            else:
                reference = str(result.get("refill", ""))
                if not reference.isdigit() or int(reference) <= 0:
                    raise ServiceError("provider_action_not_confirmed", ambiguous=True)
        return result
