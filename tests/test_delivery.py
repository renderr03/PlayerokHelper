import asyncio
import json
from dataclasses import replace

from conftest import binding, order, telegram

from shopbot.engine import Engine
from shopbot.models import now


async def test_rent_end_to_end_deduplicates_events_and_uses_rub(app):
    e, db, market, kosell = app
    binding(db)
    deal = order(e, market)
    await asyncio.gather(e.process(deal.id), e.process(deal.id))
    e.accept(deal)
    await e.process(deal.id)
    await e.flush(telegram)
    assert len(kosell.rents) == 1
    assert kosell.rents[0][0]["currency"] == "RUB"
    assert e.order(deal.id)["stage"] == "delivered"
    assert market.sends == 1
    assert "secret-password" not in db.path.read_bytes().decode(errors="ignore")


async def test_rent_timeout_reuses_key_and_exact_saved_request_after_restart(app):
    e, db, market, kosell = app
    binding(db)
    order(e, market)
    kosell.lost_rent = True
    await e.process("order1")
    assert e.order("order1")["stage"] == "rent_pending"
    db.execute("UPDATE bindings SET hours=24,max_cost=99999")
    restarted = Engine(db, market, kosell)
    restarted.recover()
    await restarted.process("order1")
    assert len(kosell.cache) == 1
    assert kosell.rents[0] == kosell.rents[1]
    assert restarted.order("order1")["stage"] == "sending"


async def test_wait_for_preparation_and_notify_refund_state(app):
    e, db, market, kosell = app
    binding(db)
    order(e, market)
    kosell.state = "checking"
    await e.process("order1")
    assert e.order("order1")["stage"] == "preparing"
    assert not db.all("SELECT * FROM outbox WHERE purpose='delivery'")
    kosell.state = "failed"
    await e.process("order1")
    assert e.order("order1")["stage"] == "review"
    assert "refunded=True" in e.order("order1")["error"]
    assert len(kosell.rents) == 1


async def test_no_purchase_when_quote_exceeds_limit(app):
    e, db, market, kosell = app
    binding(db, max_cost=100)
    order(e, market)
    await e.process("order1")
    assert e.order("order1")["error"] == "purchase_limit_exceeded"
    assert not kosell.rents


async def test_no_purchase_for_old_refunded_or_problem_orders(app):
    e, db, market, kosell = app
    binding(db)
    order(e, market, "old", created_at=now() - 86400)
    order(e, market, "refund", status="ROLLED_BACK")
    order(e, market, "problem", problem=True)
    for ref in ["old", "refund", "problem"]:
        await e.process(ref)
    assert not kosell.rents


async def test_production_gate_blocks_financial_side_effects(app):
    e, db, market, kosell = app
    binding(db)
    order(e, market)
    db.set("live_enabled", False)
    await e.process("order1")
    assert not kosell.rents


async def test_stock_concurrent_orders_get_distinct_positions(app):
    e, db, market, kosell = app
    binding(db, module="stock")
    db.add_stock("item1", ["login1:pass1", "login2:pass2"], 200)
    order(e, market, "a")
    order(e, market, "b")
    await asyncio.gather(e.process("a"), e.process("b"))
    rows = db.all("SELECT * FROM stock ORDER BY id")
    assert {r["order_id"] for r in rows} == {"a", "b"}
    assert {r["state"] for r in rows} == {"reserved"}
    await e.flush(telegram)
    assert {r["state"] for r in db.all("SELECT * FROM stock")} == {"used"}
    assert e.order("a")["cost"] == 200


async def test_lost_send_reconciles_history_without_resend_or_second_stock(app):
    e, db, market, kosell = app
    binding(db, module="stock")
    db.add_stock("item1", ["account-secret", "spare-secret"])
    order(e, market)
    await e.process("order1")
    market.lost_send = True
    await e.flush(telegram)
    assert db.one("SELECT state FROM outbox WHERE purpose='delivery'")["state"] == "uncertain"
    e.recover()
    await e.flush(telegram)
    assert market.sends == 1
    assert e.order("order1")["stage"] == "delivered"
    assert db.one("SELECT count(*) n FROM stock WHERE state='free'")["n"] == 1


async def test_crash_after_send_ack_before_order_stage_recovers(app):
    e, db, market, kosell = app
    binding(db, module="stock")
    db.add_stock("item1", ["secret"])
    order(e, market)
    await e.process("order1")
    db.execute("UPDATE outbox SET state='sent',remote_id='remote' WHERE purpose='delivery'")
    await e.flush(telegram)
    assert e.order("order1")["stage"] == "delivered"
    assert market.sends == 0


async def test_refund_between_queue_and_send_cancels_delivery(app):
    e, db, market, kosell = app
    binding(db, module="stock")
    db.add_stock("item1", ["secret"])
    deal = order(e, market)
    await e.process("order1")
    market.deals[deal.id] = replace(deal, status="ROLLED_BACK")
    await e.flush(telegram)
    assert market.sends == 0
    assert e.order("order1")["stage"] == "cancelled"


async def test_extension_debits_once_and_updates_original_rental(app):
    e, db, market, kosell = app
    binding(db)
    order(e, market)
    await e.process("order1")
    await e.flush(telegram)
    binding(db, module="extend", item_id="ext", hours=3)
    order(e, market, "extension", "ext")
    db.execute("INSERT INTO selections VALUES (?,?,?)", ("buyer", "order1", now() + 1800))
    old_expiry = e.order("order1")["expires_at"]
    await e.process("extension")
    await e.flush(telegram)
    assert kosell.extensions == 1
    assert e.order("order1")["expires_at"] == old_expiry + 3 * 3600
    assert e.order("extension")["stage"] == "delivered"
    assert not db.all("SELECT * FROM selections")


async def test_extension_timeout_and_restart_never_repeats_debit(app):
    e, db, market, kosell = app
    binding(db)
    order(e, market)
    await e.process("order1")
    await e.flush(telegram)
    binding(db, module="extend", item_id="ext")
    order(e, market, "extension", "ext")
    db.execute("INSERT INTO selections VALUES (?,?,?)", ("buyer", "order1", now() + 1800))
    kosell.lost_extend = True
    await e.process("extension")
    assert e.order("extension")["stage"] == "review"
    e.recover()
    await e.process("extension")
    assert kosell.extensions == 1
    assert json.loads(e.order("extension")["request_json"])["previous_expiry"]


async def test_extension_without_selection_waits_without_charge(app):
    e, db, market, kosell = app
    binding(db, module="extend")
    order(e, market)
    await e.process("order1")
    assert e.order("order1")["stage"] == "wait_selection"
    assert kosell.extensions == 0


async def test_historical_import_cannot_fulfil_later(app):
    e, db, market, kosell = app
    binding(db)
    deal = order(e, market, created_at=now() - 86400)
    db.execute("DELETE FROM orders")
    e.accept(deal, historical=True)
    e.accept(deal)
    await e.process(deal.id)
    assert e.order(deal.id)["stage"] == "historical"
    assert not kosell.rents


async def test_history_import_does_not_suppress_new_paid_orders(app):
    e, db, market, kosell = app
    binding(db)
    deal = order(e, market)
    db.execute("DELETE FROM orders")
    e.accept(deal, historical=True)
    assert e.order(deal.id)["stage"] == "new"
    assert not kosell.rents
    await e.process(deal.id)
    assert len(kosell.rents) == 1
