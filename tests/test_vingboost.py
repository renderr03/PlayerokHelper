# ruff: noqa: F811
import json
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest
from test_external import Supplier, configure, confirm, prepare
from test_telegram_ui import callbacks, panel  # noqa: F401

from shopbot.models import ServiceError
from shopbot.providers import PRESETS, VINGBOOST_SIMPLE_TYPES, ProviderClient
from shopbot.shop_tools import get_template, save_template


def form(request):
    assert str(request.url) == "https://vingboost.com/api/v2"
    assert request.method == "POST"
    assert "key" not in request.url.params
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def service(sid=1, kind="subscribe"):
    return {
        "service": sid,
        "name": "Followers",
        "type": kind,
        "rate": "2.50",
        "min": 10,
        "max": 10000,
        "cancel": True,
        "refill": False,
    }


async def test_vingboost_documented_balance_catalog_quote_and_purchase_payload():
    calls = []

    def request(req):
        data = form(req)
        calls.append(data)
        assert data["key"] == "PRIVATE_KEY"
        if data["action"] == "balance":
            return httpx.Response(200, json={"balance": "99.80", "currency": "USD"})
        if data["action"] == "services":
            return httpx.Response(200, json=[service()])
        assert data == {
            "key": "PRIVATE_KEY",
            "action": "add",
            "service": "1",
            "link": "https://t.me/channel",
            "quantity": "100",
        }
        return httpx.Response(200, json={"order": 123})

    client = ProviderClient("vingboost", "PRIVATE_KEY", transport=httpx.MockTransport(request))
    binding = {"service_id": "1", "quantity": 100}
    try:
        assert await client.balance() == {"USD": Decimal("99.80")}
        assert (await client.services())[0]["cancel"] is True
        quote = await client.quote(binding, "https://t.me/channel")
        assert quote["amount"] == "0.25" and quote["currency"] == "USD"
        assert await client.create(binding, "https://t.me/channel", "reference") == "123"
        assert sum(c["action"] == "add" for c in calls) == 1
    finally:
        await client.close()


async def test_vingboost_supported_types_do_not_enable_custom_or_recurring_jobs():
    rows = [service(i + 1, t) for i, t in enumerate(VINGBOOST_SIMPLE_TYPES)]
    rows += [
        service(100, t)
        for t in (
            "comment",
            "like_to_comment",
            "dislike_to_comment",
            "vote",
            "Subscription",
            "Custom Comments",
            "unknown",
        )
    ]
    client = ProviderClient(
        "vingboost", "KEY", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=rows))
    )
    other = ProviderClient(
        "twiboost",
        "KEY",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=[service(1, "subscribe"), service(2, "Default")])
        ),
    )
    try:
        assert len(await client.services()) == len(VINGBOOST_SIMPLE_TYPES)
        assert [r["id"] for r in await other.services()] == ["2"]
    finally:
        await client.close()
        await other.close()


@pytest.mark.parametrize(
    "status,remains,expected",
    [
        ("Awaiting", 100, "processing"),
        ("In progress", 100, "processing"),
        ("Completed", 0, "completed"),
        ("Completed", 1, "review"),
        ("Partial", 25, "partial"),
        ("Fail", 100, "review"),
        ("Canceled", 100, "cancelled"),
    ],
)
async def test_vingboost_statuses_preserve_partial_and_failed_orders(status, remains, expected):
    def request(req):
        assert form(req)["order"] == "123"
        return httpx.Response(
            200, json={"status": status, "remains": str(remains), "charge": "0.25", "currency": "USD"}
        )

    client = ProviderClient("vingboost", "KEY", transport=httpx.MockTransport(request))
    try:
        result = await client.status("123", {"quantity": 100})
        assert result["state"] == expected and result["remains"] == remains
        assert not result["zero_delivery"]
    finally:
        await client.close()


@pytest.mark.parametrize("action", ["cancel", "refill"])
async def test_vingboost_action_uses_singular_order_and_validates_ack(action):
    calls = []

    def request(req):
        data = form(req)
        calls.append(data)
        assert data == {"key": "PRIVATE_KEY", "action": action, "order": "123"}
        return httpx.Response(200, json={"ok": "true"} if action == "cancel" else {"refill": 3})

    client = ProviderClient("vingboost", "PRIVATE_KEY", transport=httpx.MockTransport(request))
    try:
        await client.cancel_or_refill("123", action)
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.parametrize(
    "action,response",
    [
        ("cancel", {}),
        ("cancel", {"ok": "false"}),
        ("cancel", []),
        ("refill", {"refill": 0}),
        ("refill", {"refill": True}),
    ],
)
async def test_vingboost_invalid_action_ack_is_uncertain(action, response):
    client = ProviderClient(
        "vingboost", "KEY", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response))
    )
    try:
        with pytest.raises(ServiceError) as exc:
            await client.cancel_or_refill("123", action)
        assert exc.value.ambiguous
    finally:
        await client.close()


async def test_vingboost_lost_purchase_does_not_retry_or_expose_key():
    calls = []

    def request(req):
        calls.append(req)
        raise httpx.ReadTimeout("PRIVATE_KEY", request=req)

    client = ProviderClient("vingboost", "PRIVATE_KEY", transport=httpx.MockTransport(request))
    try:
        with pytest.raises(ServiceError) as exc:
            await client.create({"service_id": "1", "quantity": 100}, "https://t.me/channel", "reference")
        assert exc.value.ambiguous and "PRIVATE_KEY" not in str(exc.value)
        assert len(calls) == 1
    finally:
        await client.close()


class VingSupplier(Supplier):
    def __call__(self, request):
        data = form(request)
        if data["action"] == "services":
            self.requests.append(request)
            return httpx.Response(200, json=[service(77)])
        if data["action"] == "cancel":
            assert "order" in data and "orders" not in data
            self.actions.append("cancel")
            return httpx.Response(200, json={"ok": "true"})
        response = super().__call__(request)
        if data["action"] == "status" and self.state == "processing":
            result = response.json()
            result["status"] = "Awaiting"
            return httpx.Response(200, json=result)
        return response


async def test_vingboost_end_to_end_encrypted_configuration_and_completed_order(app):
    engine, db, market, _ = app
    _, pid = configure(engine, "vingboost")
    supplier = VingSupplier("smm")
    engine.external.transport = httpx.MockTransport(supplier)
    balances, catalog = await engine.external.check(pid)
    assert balances["RUB"] == 100 and catalog[0]["id"] == "77"
    deal, token = await prepare(engine, market, destination="https://t.me/channel/123")
    await confirm(engine, deal, token)
    assert len(supplier.creates) == 1
    await engine.external.tick()
    assert db.one("SELECT state FROM provider_orders")["state"] == "processing"
    supplier.state = "completed"
    db.execute("UPDATE provider_orders SET next_poll=0")
    await engine.external.tick()
    assert engine.order(deal.id)["stage"] == "delivered"
    assert engine.order(deal.id)["cost"] == 100
    tid = save_template(db, "item1", "VingBoost settings")
    template = get_template(db, tid)
    assert template["external"]["preset"] == "vingboost"
    assert "TEST_SECRET_TOKEN_DO_NOT_EXPOSE" not in json.dumps(template)


async def test_vingboost_connection_button_and_private_key_form(panel):
    ui, session, send = panel
    supplier = VingSupplier("smm")
    ui.engine.external.transport = httpx.MockTransport(supplier)
    await send(callback="connect:smm")
    assert "connect:key:vingboost" in callbacks(session.calls[-1])
    await send(callback="connect:key:vingboost")
    assert "VingBoost" in session.calls[-1].text
    await send("private-vingboost-key")
    row = ui.db.one("SELECT * FROM providers WHERE preset='vingboost'")
    assert row and not row["enabled"] and ui.db.decrypt(row["token"]) == "private-vingboost-key"
    assert row["checked_revision"] == row["revision"]
    assert "VingBoost подключён" in session.calls[-1].text
    assert not supplier.creates
    assert [form(r)["action"] for r in supplier.requests] == ["balance", "services"]
    assert b"private-vingboost-key" not in ui.db.path.read_bytes()
    assert "private-vingboost-key" not in session.calls[-1].text
    assert PRESETS["vingboost"][1] == "https://vingboost.com/api/v2"


async def test_vingboost_failed_automatic_check_preserves_disabled_key_without_leaking(panel):
    ui, session, send = panel
    calls = []

    def request(req):
        calls.append(req)
        return httpx.Response(400, json={"error": "private-vingboost-key"})

    ui.engine.external.transport = httpx.MockTransport(request)
    await send(callback="connect:key:vingboost")
    await send("private-vingboost-key")
    row = ui.db.one("SELECT * FROM providers WHERE preset='vingboost'")
    assert row and not row["enabled"] and row["checked_revision"] != row["revision"]
    assert "Подключиться сейчас не удалось" in session.calls[-1].text
    assert "private-vingboost-key" not in session.calls[-1].text
    assert len(calls) == 1 and form(calls[0])["action"] == "balance"
