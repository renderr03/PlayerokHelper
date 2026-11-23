from dataclasses import replace

import pytest
from conftest import binding, order, telegram

from shopbot.analytics import Analytics
from shopbot.conversations import Conversations
from shopbot.loyalty import Loyalty
from shopbot.models import Message, now
from shopbot.rentals import Rentals


async def test_own_rental_is_reserved_once_and_requires_rotation_after_expiry(app):
    engine, db, market, kosell = app
    binding(db, module="stock", fulfillment="own_rent")
    pool = Rentals(engine)
    pool.add("item1", ["login:original-password"], 100)
    deal = order(engine, market)
    await engine.process(deal.id)
    await engine.process(deal.id)
    assert db.one("SELECT state FROM own_accounts")["state"] == "reserved"
    await engine.flush(telegram)
    assert db.one("SELECT state FROM own_accounts")["state"] == "active"
    assert engine.order(deal.id)["module"] == "own_rent"
    assert len(kosell.rents) == 0 and market.sends == 1
    assert b"original-password" not in db.path.read_bytes()
    db.execute("UPDATE own_accounts SET expires_at=?", (now() - 1,))
    db.execute("UPDATE own_leases SET expires_at=?", (now() - 1,))
    pool.tick()
    assert db.one("SELECT state FROM own_accounts")["state"] == "reset"
    with pytest.raises(ValueError):
        pool.reset(1, "login:original-password")
    pool.reset(1, "login:changed-password")
    assert db.one("SELECT state FROM own_accounts")["state"] == "free"
    second = order(engine, market, "order2", buyer="other")
    await engine.process(second.id)
    await engine.flush(telegram)
    assert "changed-password" in market.history[-1].text


async def test_one_account_cannot_be_allocated_to_two_buyers(app):
    import asyncio

    engine, db, market, _ = app
    binding(db, module="stock", fulfillment="own_rent")
    Rentals(engine).add("item1", ["account:password"])
    order(engine, market, "one")
    order(engine, market, "two", buyer="another")
    await asyncio.gather(engine.process("one"), engine.process("two"))
    assert db.one("SELECT count(*) n FROM own_leases")["n"] == 1
    assert {engine.order(x)["stage"] for x in ["one", "two"]} == {"sending", "failed"}


async def test_own_extension_is_paid_owned_and_exactly_once(app):
    engine, db, market, _ = app
    binding(db, module="stock", fulfillment="own_rent")
    binding(db, module="stock", item_id="ext", fulfillment="own_extend", pool_item="item1", hours=2)
    Rentals(engine).add("item1", ["account:password"])
    order(engine, market)
    await engine.process("order1")
    await engine.flush(telegram)
    old = engine.order("order1")["expires_at"]
    chats = Conversations(engine)
    await chats.handle(Message("select", "chat:buyer", "buyer", "!продлить order1", now()))
    order(engine, market, "extension", item_id="ext")
    await engine.process("extension")
    await engine.process("extension")
    assert engine.order("order1")["expires_at"] == old + 7200
    assert db.one("SELECT count(*) n FROM own_extensions")["n"] == 1
    order(engine, market, "attacker", item_id="ext", buyer="intruder")
    engine.update("attacker", target_order_id="order1", stage="extension_ready")
    await engine.process("attacker")
    assert engine.order("order1")["expires_at"] == old + 7200
    assert engine.order("attacker")["stage"] == "failed"


async def test_expired_pending_delivery_is_not_sent_and_uncertain_account_not_reused(app):
    engine, db, market, _ = app
    binding(db, module="stock", fulfillment="own_rent")
    pool = Rentals(engine)
    pool.add("item1", ["account:password"])
    order(engine, market)
    await engine.process("order1")
    db.execute("UPDATE own_accounts SET expires_at=?", (now() - 1,))
    db.execute("UPDATE own_leases SET expires_at=?", (now() - 1,))
    await engine.flush(telegram)
    assert not market.sends and engine.order("order1")["stage"] == "review"
    db.execute("UPDATE outbox SET state='uncertain' WHERE purpose='delivery'")
    with pytest.raises(ValueError, match="неопределённую"):
        pool.reset(1, "new:password")


async def completed_purchase(engine, db, market, name, buyer="buyer"):
    deal = order(engine, market, name, buyer=buyer)
    engine.update(name, stage="delivered", delivered_at=now(), cost=0)
    market.deals[name] = replace(deal, status="CONFIRMED")
    engine.accept(market.deals[name])


async def test_loyalty_counts_only_new_completed_purchases_and_deduplicates(app):
    engine, db, market, _ = app
    binding(db, module="stock")
    await completed_purchase(engine, db, market, "before-program")
    gifts = Loyalty(engine)
    gifts.configure(2)
    gifts.add(["gift:secret"], 50)
    await completed_purchase(engine, db, market, "one")
    gifts.tick()
    assert not db.all("SELECT * FROM bonus_claims")
    await completed_purchase(engine, db, market, "two")
    gifts.tick()
    gifts.tick()
    assert db.one("SELECT count(*) n FROM bonus_claims")["n"] == 1
    await engine.flush(telegram)
    gifts.tick()
    await engine.flush(telegram)
    assert market.sends == 1
    assert db.one("SELECT state FROM bonus_stock")["state"] == "used"
    db.set("fee_percent", 0)
    report = Analytics(db).summary()
    assert report["bonus_cost"] == 50 and report["promotion"] == 0
    assert b"gift:secret" not in db.path.read_bytes()


async def test_refund_before_bonus_send_releases_gift_and_can_earn_again(app):
    engine, db, market, _ = app
    binding(db, module="stock")
    gifts = Loyalty(engine)
    gifts.configure(1)
    gifts.add(["gift:secret"], 0)
    await completed_purchase(engine, db, market, "one")
    gifts.tick()
    market.deals["one"] = replace(market.deals["one"], status="ROLLED_BACK")
    engine.accept(market.deals["one"])
    gifts.tick()
    assert db.one("SELECT state FROM bonus_stock")["state"] == "free"
    await engine.flush(telegram)
    assert market.sends == 0
    await completed_purchase(engine, db, market, "two")
    gifts.tick()
    await engine.flush(telegram)
    assert market.sends == 1
    assert db.one("SELECT generation FROM bonus_claims")["generation"] == 1


async def test_ambiguous_bonus_is_not_reallocated_and_history_proof_finishes_it(app):
    engine, db, market, _ = app
    binding(db, module="stock")
    gifts = Loyalty(engine)
    gifts.configure(1)
    gifts.add(["gift:secret"], None)
    await completed_purchase(engine, db, market, "one")
    gifts.tick()
    market.lost_send = True
    await engine.flush(telegram)
    assert db.one("SELECT state FROM bonus_stock")["state"] == "reserved"
    assert Analytics(db).summary()["bonus_uncertain"] == 1
    await engine.flush(telegram)
    assert market.sends == 1
    assert db.one("SELECT state FROM bonus_stock")["state"] == "used"
    assert Analytics(db).summary()["bonus_unknown"] == 1


async def test_bonus_rechecks_fresh_problem_before_send(app):
    engine, db, market, _ = app
    binding(db, module="stock")
    gifts = Loyalty(engine)
    gifts.configure(1)
    gifts.add(["gift:secret"], 0)
    await completed_purchase(engine, db, market, "one")
    gifts.tick()
    market.deals["one"] = replace(market.deals["one"], problem=True)
    await engine.flush(telegram)
    assert market.sends == 0
    assert db.one("SELECT state FROM bonus_stock")["state"] == "free"


async def test_stale_cancelled_gift_cannot_send_a_reallocated_code(app):
    engine, db, market, _ = app
    binding(db, module="stock")
    gifts = Loyalty(engine)
    gifts.configure(1)
    gifts.add(["gift-A", "gift-B"], 0)
    await completed_purchase(engine, db, market, "one")
    gifts.tick()
    old_box = db.one("SELECT id FROM outbox WHERE purpose LIKE 'bonus:%'")["id"]
    market.deals["one"] = replace(market.deals["one"], status="ROLLED_BACK")
    engine.accept(market.deals["one"])
    gifts.tick()
    await completed_purchase(engine, db, market, "other", buyer="another")
    gifts.tick()
    await completed_purchase(engine, db, market, "replacement")
    gifts.tick()
    db.execute("UPDATE outbox SET state='pending' WHERE id=?", (old_box,))
    await engine.flush(telegram)
    buyer_texts = [m.text for m in market.history if m.chat_id == "chat:buyer"]
    assert len(buyer_texts) == 1 and "gift-B" in buyer_texts[0] and "gift-A" not in buyer_texts[0]


async def test_uncertain_refunded_gift_is_not_freed_without_explicit_review(app):
    engine, db, market, _ = app
    binding(db, module="stock")
    gifts = Loyalty(engine)
    gifts.configure(1)
    gifts.add(["gift-A"], 0)
    await completed_purchase(engine, db, market, "one")
    gifts.tick()
    market.lost_send = True
    await engine.flush(telegram)
    # Hide history to simulate an unresolvable provider-side send.
    market.history.clear()
    market.deals["one"] = replace(market.deals["one"], status="ROLLED_BACK")
    engine.accept(market.deals["one"])
    db.execute("UPDATE outbox SET state='pending' WHERE purpose LIKE 'bonus:%'")
    await engine.flush(telegram)
    assert db.one("SELECT state FROM bonus_stock")["state"] == "reserved"
    claim = db.one("SELECT * FROM bonus_claims")
    assert claim["state"] == "review"
    with pytest.raises(ValueError):
        gifts.settle_review(claim["id"], False, generation=99)
    gifts.settle_review(claim["id"], False, generation=claim["generation"])
    assert db.one("SELECT state FROM bonus_stock")["state"] == "free"


async def test_bonus_recovers_crash_after_recording_successful_send(app):
    engine, db, market, _ = app
    binding(db, module="stock")
    gifts = Loyalty(engine)
    gifts.configure(1)
    gifts.add(["gift-A"], 10)
    await completed_purchase(engine, db, market, "one")
    gifts.tick()
    db.execute("UPDATE outbox SET state='sent' WHERE purpose LIKE 'bonus:%'")
    gifts.tick()
    gifts.tick()
    assert db.one("SELECT state FROM bonus_stock")["state"] == "used"
    assert db.one("SELECT count(*) n FROM expenses WHERE kind='bonus'")["n"] == 1


def test_stale_credential_rotation_cannot_overwrite_a_newer_rotation(app):
    engine, db, _, _ = app
    binding(db, module="stock", fulfillment="own_rent")
    pool = Rentals(engine)
    pool.add("item1", ["first:secret"])
    snapshot = db.one("SELECT ciphertext,order_id,state FROM own_accounts WHERE id=1")
    pool.reset(1, "second:secret")
    with pytest.raises(ValueError, match="изменился"):
        pool.reset(1, "third:secret", expected=snapshot)
