"""No listing writes during external-service setup; opt-in visibility is explicit."""

import io
import json
import zipfile

import httpx
import pytest
from test_external import configure
from test_telegram_ui import callbacks
from test_telegram_ui import panel as panel
from test_vingboost import VingSupplier

from shopbot.external import encoded
from shopbot.listing_policy import auto_hide_external
from shopbot.listings import Listings
from shopbot.maintenance import diagnostics
from shopbot.models import ServiceError

ITEM = "5dff9154-a790-4160-b020-23481aef3064"


@pytest.mark.parametrize("preset", ["split", "vingboost"])
@pytest.mark.parametrize(
    "condition,reason",
    [
        ("paused", "manual_pause"),
        ("disabled", "provider_not_enabled_or_checked"),
        ("rate", "provider_exchange_rate_required"),
        ("balance", "provider_insufficient_balance"),
        ("limit", "provider_cost_limit_required"),
        ("network", "provider_check_failed"),
    ],
)
async def test_new_external_lot_stays_on_sale_without_purchase_even_if_not_ready(
    app, preset, condition, reason
):
    engine, db, market, _ = app
    supplier, pid = configure(engine, preset, item_id=ITEM)
    if preset == "vingboost":
        supplier = VingSupplier("smm")
        engine.external.transport = httpx.MockTransport(supplier)
    await engine.external.check(pid)
    if condition == "paused":
        db.execute("UPDATE bindings SET paused=1")
    elif condition == "disabled":
        db.execute("UPDATE providers SET enabled=0")
    elif condition == "rate":
        db.execute("UPDATE external_bindings SET currency='USD'")
        db.execute("UPDATE providers SET settings=?", (encoded({"rates": {}}),))
    elif condition == "balance":
        unit = "TON" if preset == "split" else "RUB"
        db.set("provider_balance:" + str(pid), {unit: "0"})
    elif condition == "limit":
        db.execute("UPDATE external_bindings SET max_native='0'")
    else:
        db.execute("UPDATE providers SET checked_at=0")
        engine.external.transport = httpx.MockTransport(lambda r: httpx.Response(503))
    manager = Listings(engine)
    await manager.tick()
    assert db.one("SELECT hide_reason FROM bindings")["hide_reason"] == reason
    for _ in range(3):
        await manager.tick()
    assert not auto_hide_external(db, ITEM)
    assert not market.transitions and not supplier.creates
    assert not db.all("SELECT * FROM orders")
    assert not db.get("visibility_intent:" + ITEM)
    assert (await market.item(ITEM))["status"] == "APPROVED"


async def test_opt_in_hides_on_precise_reason_and_restores_only_after_two_healthy_reads(app):
    engine, db, market, _ = app
    supplier, pid = configure(engine, "vingboost", item_id=ITEM)
    await engine.external.check(pid)
    db.set("external_auto_hide:" + ITEM, True)
    db.execute("UPDATE providers SET enabled=0")
    manager = Listings(engine)
    await manager.tick()
    assert market.transitions == [(ITEM, False)]
    assert db.one("SELECT hide_reason FROM bindings")["hide_reason"] == "provider_not_enabled_or_checked"
    db.execute("UPDATE providers SET enabled=1")
    await manager.tick()
    assert market.transitions == [(ITEM, False)]
    await manager.tick()
    assert market.transitions == [(ITEM, False), (ITEM, True)]
    assert not supplier.creates


async def test_global_pause_remains_explicit_and_resume_restores_only_its_own_external_lot(app):
    engine, db, market, _ = app
    _, pid = configure(engine, "vingboost", item_id=ITEM)
    await engine.external.check(pid)
    manager = Listings(engine)
    db.set("paused", True)
    await manager.tick()
    assert market.transitions == [(ITEM, False)]
    assert db.get("global_hide:" + ITEM)
    db.set("paused", False)
    await manager.tick()
    await manager.tick()
    assert market.transitions == [(ITEM, False), (ITEM, True)]
    assert not db.get("global_hide:" + ITEM)


async def test_preexisting_external_hide_is_not_replayed_and_user_can_open_lot(panel):
    ui, session, send = panel
    configure(ui.engine, "vingboost", item_id=ITEM)
    ui.db.execute("UPDATE bindings SET paused=1,hidden_by_bot=1,hide_reason='external_provider_not_ready'")
    ui.db.set("visibility_intent:" + ITEM, "hide")
    ui.engine.market.items[ITEM] = {"id": ITEM, "status": "PAUSED", "user": {"id": "seller"}}
    await Listings(ui.engine).tick()
    assert not ui.engine.market.transitions
    await send(callback="lot:" + ITEM)
    assert "Ранее бот отправлял снятие" in session.calls[-1].text
    assert any(
        b.url == "https://playerok.com/products/" + ITEM
        for row in session.calls[-1].reply_markup.inline_keyboard
        for b in row
    )


async def test_external_auto_hide_requires_confirmation_and_navigation_invalidates_offer(panel):
    ui, session, send = panel
    configure(ui.engine, "vingboost", item_id=ITEM)
    await send(callback="lot:" + ITEM)
    rid = ui.db.one("SELECT rowid rid FROM bindings")["rid"]
    await send(callback=f"x:visibility:{rid}", user=999)
    assert not auto_hide_external(ui.db, ITEM)
    await send(callback=f"x:visibility:{rid}")
    token = next(c for c in callbacks(session.calls[-1]) if c and c.startswith("x:confirm:"))
    assert "ещё до покупки" in session.calls[-1].text
    assert not auto_hide_external(ui.db, ITEM)
    await send(callback="menu:home")
    await send(callback=token)
    assert not auto_hide_external(ui.db, ITEM)
    await send(callback=f"x:visibility:{rid}")
    token = next(c for c in callbacks(session.calls[-1]) if c and c.startswith("x:confirm:"))
    await send(callback=token)
    assert auto_hide_external(ui.db, ITEM)
    await send(callback=f"x:visibility:{rid}")
    assert not auto_hide_external(ui.db, ITEM)
    assert not ui.engine.market.transitions


async def test_availability_currency_is_not_fabricated_from_binding_and_purchase_guards_remain(app):
    engine, db, market, _ = app
    supplier, pid = configure(engine, "vingboost", item_id=ITEM)
    await engine.external.check(pid)
    db.set("provider_balance:" + str(pid), {"USD": "100"})
    await Listings(engine).tick()
    assert db.one("SELECT hide_reason FROM bindings")["hide_reason"] == "provider_currency_mismatch"
    assert not market.transitions
    supplier.balance = "0"
    with pytest.raises(ServiceError) as exc:
        await engine.external.preview(engine.external.binding(ITEM), "https://t.me/channel/1")
    assert exc.value.code == "provider_insufficient_balance" and not supplier.creates


async def test_diagnostics_include_safe_lot_reason_and_separate_telegram_from_buyers(app, tmp_path):
    engine, db, _, _ = app
    configure(engine, "vingboost", item_id=ITEM)
    secret = "PRIVATE-API-KEY-DO-NOT-EXPORT"
    db.execute(
        "UPDATE bindings SET hide_reason='manual_pause',paused=1,title=?,instruction=?", (secret, secret)
    )
    db.set("admins", [42])
    db.notify("Telegram notice", "notice")
    db.execute("UPDATE outbox SET state='uncertain' WHERE channel='telegram'")
    assert engine.diagnostic()["uncertain"] == 0
    raw = zipfile.ZipFile(io.BytesIO(diagnostics(db, tmp_path))).read("diagnostics.json")
    payload = json.loads(raw)
    assert secret.encode() not in raw

    row = payload["workflows"]["lots"][0]
    assert row["availability_reason"] == "manual_pause" and row["external_auto_hide"] is False
    assert payload["workflows"]["uncertain_sends_by_channel"] == [{"channel": "telegram", "count": 1}]
    db.execute("UPDATE bindings SET hide_reason=?", (secret,))
    raw = zipfile.ZipFile(io.BytesIO(diagnostics(db, tmp_path))).read("diagnostics.json")
    assert secret.encode() not in raw


async def test_complete_vingboost_ui_creation_and_enabling_delivery_never_hides_new_listing(panel):
    ui, session, send = panel
    _, pid = configure(ui.engine, "vingboost", item_id="existing")
    supplier = VingSupplier("smm")
    ui.engine.external.transport = httpx.MockTransport(supplier)
    await send(callback=f"x:check:{pid}")
    await send(callback="x:new:smm")
    await send(ITEM)
    await send("Просмотры канала")
    await send(callback=f"x:choose:{pid}")
    await send(callback=f"x:service:{pid}:77")
    for value in ("500", "2", "100", "t.me", "-"):
        await send(value)
    save = next(c for c in callbacks(session.calls[-1]) if c and c.startswith("x:save:"))
    await send(callback=save)
    assert ui.db.one("SELECT paused FROM bindings WHERE item_id=?", (ITEM,))["paused"]
    await Listings(ui.engine).tick()
    await send(callback="lotflag:paused:" + ITEM)
    await Listings(ui.engine).tick()
    assert not ui.engine.market.transitions
    assert not supplier.creates and not ui.db.all("SELECT * FROM orders")
    assert (await ui.engine.market.item(ITEM))["status"] == "APPROVED"
