from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import binding, order
from cryptography.fernet import Fernet

from shopbot.alerts import Alerts
from shopbot.checks import connection_snapshot, needs_kosell, preview_delivery
from shopbot.db import Database
from shopbot.engine import Engine
from shopbot.listings import Listings
from shopbot.models import ServiceError, now
from shopbot.runtime import Runtime, UnconfiguredPlayerok, readiness


def proof(db, market):
    db.set("visibility_proof", {"account_id": market.user_id, "fingerprint": market.fingerprint()})


def forbid_mutations(monkeypatch, engine, db, market, kosell):
    async def forbidden(*args, **kwargs):
        raise AssertionError("A preview attempted a remote mutation or secret retrieval")

    for name in ["rent", "extend", "credentials", "code", "delivery"]:
        monkeypatch.setattr(kosell, name, forbidden)
    for name in ["set_visible", "send_message", "listing_action", "mark_sent", "refund"]:
        monkeypatch.setattr(market, name, forbidden)

    def no_local_mutation(*args, **kwargs):
        raise AssertionError("A preview attempted a reservation or secret decryption")

    monkeypatch.setattr(db, "reserve_stock", no_local_mutation)
    monkeypatch.setattr(db, "decrypt", no_local_mutation)


@pytest.mark.parametrize("module", ["stock", "kosell", "extend"])
async def test_preview_is_read_only_for_all_modules(app, monkeypatch, module):
    engine, db, market, kosell = app
    proof(db, market)
    binding(db, module=module)
    db.add_stock("item1", ["secret-login:secret-password"])
    rental_id = None
    if module == "extend":
        binding(db, item_id="rental-lot")
        order(engine, market, item_id="rental-lot")
        engine.update(
            "order1",
            stage="delivered",
            expires_at=now() + 3600,
            rental_uid="uid:playerok:rent:order1",
        )
        rental_id = "order1"
    before = list(db.conn.iterdump())
    forbid_mutations(monkeypatch, engine, db, market, kosell)
    if module == "stock":
        kosell.balance = AsyncMock(side_effect=AssertionError("stock preview called KOSell"))
        kosell.products = AsyncMock(side_effect=AssertionError("stock preview called KOSell"))
    result = await preview_delivery(engine, "item1", rental_id=rental_id)
    assert result.issues == []
    assert result.sample
    assert "secret-password" not in result.text(engine.expiry)
    assert list(db.conn.iterdump()) == before
    assert not market.transitions and not market.actions and not kosell.rents
    assert kosell.extensions == kosell.code_calls == market.sends == 0


@pytest.mark.parametrize(
    "change, fragment",
    [
        ("paused", "пауза"),
        ("empty", "свободных аккаунтов"),
        ("price", "выше лимита"),
        ("balance", "Недостаточно средств"),
        ("duration", "вне диапазона"),
        ("owner", "не принадлежит"),
        ("status", "активном статусе"),
        ("pending", "ожидает восстановления"),
        ("outage", "Нет актуальных данных"),
        ("malformed", "формат ответа"),
    ],
)
async def test_preview_reports_blocks_without_side_effects(app, monkeypatch, change, fragment):
    engine, db, market, kosell = app
    proof(db, market)
    binding(db)
    await market.item("item1")
    if change == "paused":
        db.set("paused", True)
    elif change == "empty":
        kosell.available = 0
    elif change == "price":
        kosell.price = 100
    elif change == "balance":
        kosell.rub_balance = 0
    elif change == "duration":
        db.execute("UPDATE bindings SET hours=101")
    elif change == "owner":
        market.items["item1"]["user"] = {"id": "another-store"}
    elif change == "status":
        market.items["item1"]["status"] = "PAUSED"
    elif change == "pending":
        db.set("visibility_test_pending", {"item_id": "item1", "account_id": "seller"})
    elif change == "outage":
        kosell.quote = AsyncMock(side_effect=ServiceError("kosell_network"))
    elif change == "malformed":
        kosell.quote = AsyncMock(return_value={})
    forbid_mutations(monkeypatch, engine, db, market, kosell)
    before = list(db.conn.iterdump())
    result = await preview_delivery(engine, "item1")
    assert any(fragment in issue for issue in result.issues)
    assert list(db.conn.iterdump()) == before


async def test_stock_empty_and_extension_selection_checks(app):
    engine, db, market, kosell = app
    proof(db, market)
    binding(db, module="stock")
    assert "Склад пуст." in (await preview_delivery(engine, "item1")).issues
    binding(db, module="extend", item_id="extension")
    result = await preview_delivery(engine, "extension", rental_id="not-in-this-store")
    assert "Выберите действующую аренду этого магазина." in result.issues
    assert kosell.extensions == 0


async def test_stock_only_readiness_and_snapshot_do_not_enable_worker(app, monkeypatch):
    engine, db, market, kosell = app
    proof(db, market)
    binding(db, module="stock")
    db.set("live_enabled", False)
    kosell.balance = AsyncMock(side_effect=AssertionError("KOSell not required"))
    before = list(db.conn.iterdump())
    result = await connection_snapshot(db, market, kosell, check_kosell=needs_kosell(db))
    assert not result["issues"]
    assert list(db.conn.iterdump()) == before and not db.get("live_enabled")
    assert (await readiness(db, market, kosell, needs_kosell=needs_kosell(db)))[0]
    assert db.get("live_enabled")
    market.profile = AsyncMock(side_effect=ServiceError("network"))
    await readiness(db, market, kosell, needs_kosell=False)
    assert not db.get("live_enabled")


def notifications(db):
    return [db.decrypt(r["ciphertext"]) for r in db.all("SELECT * FROM outbox WHERE channel='telegram'")]


def test_stock_alert_edges_survive_restart_and_new_problem(app, tmp_path):
    _, _, market, kosell = app
    key = Fernet.generate_key().decode()
    path = tmp_path / "alerts.sqlite"
    db = Database(path, key)
    db.set("admins", [42])
    binding(db, module="stock")
    alerts = Alerts(Engine(db, market, kosell))
    alerts.stock("item1")
    alerts.stock("item1")
    assert len(notifications(db)) == 1
    db.close()
    reopened = Database(path, key)
    try:
        alerts = Alerts(Engine(reopened, market, kosell))
        alerts.stock("item1")
        assert len(notifications(reopened)) == 1
        reopened.add_stock("item1", ["a", "b", "c", "d"])
        alerts.stock("item1")
        reopened.execute("UPDATE stock SET state='used'")
        alerts.stock("item1")
        assert len(notifications(reopened)) == 3
    finally:
        reopened.close()


async def test_balance_alert_threshold_unknown_data_and_recovery(app):
    engine, db, market, kosell = app
    binding(db)
    binding(db, module="extend", item_id="extension")
    db.set("admins", [42])
    alerts = Alerts(engine)
    rows = db.all("SELECT * FROM bindings")
    products = {p["id"]: p for p in await kosell.products()}
    quotes = {(7, 1): {"total_rub": 10}}
    await alerts.availability(rows, products, 2000, quotes)
    assert len(notifications(db)) == 1
    assert "2 аренд" in notifications(db)[0]
    await alerts.availability(rows, None, None, {})
    await alerts.availability(rows, products, 2000, quotes)
    assert len(notifications(db)) == 1
    await alerts.availability(rows, products, 3000, quotes)
    await alerts.availability(rows, products, 1000, quotes)
    assert len(notifications(db)) == 3
    assert not any("extension" in msg for msg in notifications(db))
    db.set("alerts_enabled", False)
    await alerts.availability(rows, products, 10000, quotes)
    assert len(notifications(db)) == 3


def test_connection_alerts_need_consecutive_failures_and_recoveries(app):
    engine, db, _, _ = app
    db.set("admins", [42])
    alerts = Alerts(engine)

    def observe(ok, configured=True):
        alerts.connections({"playerok": {"configured": configured, "ok": ok}})

    observe(False, configured=False)
    observe(False)
    observe(False)
    observe(True)
    observe(False)
    observe(False)
    assert not notifications(db)
    observe(False)
    assert len(notifications(db)) == 1
    observe(True)
    observe(False)
    observe(True)
    assert len(notifications(db)) == 1
    observe(True)
    assert len(notifications(db)) == 2
    assert "восстановлено" in notifications(db)[1]


async def test_listing_quotes_and_balance_reused_for_alerts(app):
    engine, db, market, kosell = app
    binding(db)
    binding(db, item_id="second")
    kosell.quote = AsyncMock(wraps=kosell.quote)
    kosell.balance = AsyncMock(side_effect=AssertionError("snapshot already provides balance"))
    await Listings(engine).tick(
        snapshot={
            "services": {"kosell": {"ok": True}},
            "kosell_balance": {"balance_rub": 1000},
        }
    )
    assert kosell.quote.await_count == 1


async def test_failed_quote_is_not_retried_by_alerts_or_other_lots(app):
    engine, db, _, kosell = app
    binding(db)
    binding(db, item_id="second")
    kosell.quote = AsyncMock(side_effect=ServiceError("kosell_network"))
    await Listings(engine).tick()
    assert kosell.quote.await_count == 1
    assert db.get("alert:balance:item1") is None


async def test_stock_delivery_and_periodic_warning_share_state(app):
    engine, db, market, _ = app
    binding(db, module="stock")
    db.set("admins", [42])
    db.add_stock("item1", ["first", "second"])
    order(engine, market)
    engine.stock_delivery(engine.order("order1"), db.one("SELECT * FROM bindings"))
    await Alerts(engine).availability(db.all("SELECT * FROM bindings"), None, None, {})
    messages = notifications(db)
    assert len(messages) == 2  # new-order notification plus one stock alert
    assert sum("Мало товара" in message for message in messages) == 1


async def test_runtime_alerts_continue_while_playerok_disconnected(app, tmp_path):
    engine, db, _, kosell = app
    binding(db, module="stock")
    engine.market = UnconfiguredPlayerok()
    db.set("admins", [42])
    ui = SimpleNamespace(send_admin=AsyncMock(return_value="sent"))
    runtime = Runtime(SimpleNamespace(data_dir=tmp_path, admins=[42]), db, engine, ui)
    await runtime.cycle()
    assert not db.get("live_enabled")
    assert len(notifications(db)) == 1
    assert "Мало товара" in notifications(db)[0]
    assert ui.send_admin.await_count == 1


def test_existing_database_preserves_settings_and_stock(tmp_path):
    key = Fernet.generate_key().decode()
    path = tmp_path / "existing.sqlite"
    db = Database(path, key)
    db.set("stock_low", 7)
    db.add_stock("old-lot", ["old-secret"])
    db.execute("DELETE FROM settings WHERE key IN ('alerts_enabled','rental_low')")
    db.close()
    db = Database(path, key)
    try:
        assert db.get("alerts_enabled") is True and db.get("rental_low") == 2
        assert db.get("stock_low") == 7
        assert db.decrypt(db.one("SELECT ciphertext FROM stock")["ciphertext"]) == "old-secret"
    finally:
        db.close()


async def test_tokens_only_readiness_requires_no_test_or_mutations(app, monkeypatch):
    engine, db, market, kosell = app
    db.set("visibility_proof", None)
    forbid_mutations(monkeypatch, engine, db, market, kosell)
    ok, issues = await readiness(db, market, kosell)
    assert ok and not issues and db.get("live_enabled")
    assert db.get("visibility_proof") is None
    assert db.get("profile")["id"] == "seller"
    assert db.get("kosell_balance")["balance_rub"] == 1000
    assert not market.transitions and not kosell.rents


async def test_connection_timeout_is_reported_and_other_service_is_still_checked(app, monkeypatch):
    import asyncio

    import shopbot.checks as checks

    _, db, market, kosell = app
    monkeypatch.setattr(checks, "CONNECTION_TIMEOUT", 0.01)

    async def stalled():
        await asyncio.Event().wait()

    market.profile = stalled
    snapshot = await connection_snapshot(db, market, kosell)
    assert snapshot["issues"] == ["playerok_timeout"]
    assert snapshot["services"]["kosell"]["ok"]
    assert not snapshot["services"]["playerok"]["ok"]


async def test_pending_test_only_blocks_new_orders_of_that_listing(app):
    engine, db, market, _ = app
    binding(db, module="stock", item_id="blocked")
    binding(db, module="stock", item_id="healthy")
    db.add_stock("blocked", ["blocked-secret"])
    db.add_stock("healthy", ["healthy-secret"])
    db.set("visibility_test_pending", {"item_id": "blocked", "account_id": "seller"})
    order(engine, market, order_id="blocked-order", item_id="blocked")
    order(engine, market, order_id="healthy-order", item_id="healthy")
    await readiness(db, market, engine.kosell, needs_kosell=False)
    await engine.process("blocked-order")
    await engine.process("healthy-order")
    assert db.get("live_enabled")
    assert engine.order("blocked-order")["stage"] == "new"
    assert engine.order("healthy-order")["stage"] == "sending"
    assert db.one("SELECT state FROM stock WHERE item_id='blocked'")["state"] == "free"


async def test_pending_test_does_not_change_listing_or_hide_healthy_other_lot(app):
    engine, db, market, _ = app
    binding(db, module="stock", item_id="blocked")
    binding(db, module="stock", item_id="healthy")
    db.add_stock("healthy", ["secret"])
    db.set("visibility_test_pending", {"item_id": "blocked", "account_id": "seller"})
    await Listings(engine).tick()
    assert not market.transitions
    assert "blocked" not in market.items


def test_supplier_validation_does_not_revalidate_stale_playerok_session(app):
    from shopbot.checks import connected_service

    _, db, _, _ = app
    db.set(
        "connection_snapshot",
        {
            "at": now() - 3600,
            "services": {"playerok": {"ok": True, "configured": True}},
            "issues": [],
            "profile": {"id": "seller", "username": "old", "balance": {"value": 100}},
        },
    )
    connected_service(db, "kosell", {"balance_rub": 100})
    assert not db.get("live_enabled")
    assert not db.get("connection_snapshot")["services"]["playerok"]["ok"]


async def test_background_errors_are_debounced_and_never_show_internal_names(app, tmp_path, monkeypatch):
    import asyncio

    engine, db, _, _ = app
    db.set("admins", [42])
    runtime = Runtime(SimpleNamespace(data_dir=tmp_path), db, engine, SimpleNamespace())
    calls = 0
    sleeps = 0

    async def operation():
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise ServiceError("private-exception-details")

    async def advance(delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            assert not notifications(db)
        if sleeps >= 5:
            raise asyncio.CancelledError()

    monkeypatch.setattr("shopbot.runtime.asyncio.sleep", advance)
    with pytest.raises(asyncio.CancelledError):
        await runtime.scheduled("connections", operation, 1)
    messages = notifications(db)
    assert len(messages) == 2
    assert all("connections" not in m and "private" not in m for m in messages)
    assert "повторит попытку автоматически" in messages[0]
    assert "восстановлена" in messages[1]
    assert db.get("worker_health:connections")["ok"]


@pytest.mark.parametrize("configured", [True, False])
async def test_supplier_outage_waits_without_failing_paid_order_or_blocking_stock(app, configured):
    engine, db, market, kosell = app
    kosell.configured = configured
    binding(db, module="kosell", item_id="rental")
    binding(db, module="stock", item_id="stock")
    db.add_stock("stock", ["secret"])
    order(engine, market, order_id="rental-order", item_id="rental")
    order(engine, market, order_id="stock-order", item_id="stock")
    db.set("connection_snapshot", {"services": {"kosell": {"configured": configured, "ok": False}}})
    await engine.process("rental-order")
    await engine.process("stock-order")
    assert engine.order("rental-order")["stage"] == "new"
    assert engine.order("stock-order")["stage"] == "sending"
    assert not kosell.rents
    assert db.get("live_enabled")
