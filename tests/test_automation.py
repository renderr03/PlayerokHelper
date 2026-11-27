from dataclasses import replace

from conftest import binding, order, telegram

from shopbot.conversations import Conversations
from shopbot.listings import Listings
from shopbot.models import Message, ServiceError, now
from shopbot.runtime import readiness


async def test_hides_and_restores_only_after_two_healthy_checks(app):
    e, db, market, kosell = app
    binding(db)
    manager = Listings(e)
    kosell.available = 0
    await manager.tick()
    assert market.transitions == [("item1", False)]
    kosell.available = 2
    await manager.tick()
    assert market.transitions == [("item1", False)]
    await manager.tick()
    assert market.transitions[-1] == ("item1", True)


async def test_manual_pause_survives_restock_and_does_not_restore_external_pause(app):
    e, db, market, kosell = app
    binding(db, paused=1)
    manager = Listings(e)
    await manager.tick()
    await manager.tick()
    await manager.tick()
    assert market.transitions == [("item1", False)]
    binding(db, item_id="external")
    await market.item("external")
    market.items["external"]["status"] = "PAUSED"
    await manager.tick()
    await manager.tick()
    assert ("external", True) not in market.transitions


async def test_hide_intent_survives_crash_between_remote_change_and_local_ack(app):
    e, db, market, kosell = app
    binding(db)
    kosell.available = 0
    await market.item("item1")
    market.items["item1"]["status"] = "PAUSED"
    db.set("visibility_intent:item1", "hide")
    manager = Listings(e)
    await manager.tick()
    assert db.one("SELECT hidden_by_bot FROM bindings")["hidden_by_bot"] == 1


async def test_hide_intent_recovers_when_stock_returns_during_restart(app):
    e, db, market, kosell = app
    binding(db)
    await market.item("item1")
    market.items["item1"]["status"] = "PAUSED"
    db.set("visibility_intent:item1", "hide")
    manager = Listings(e)
    await manager.tick()
    await manager.tick()
    assert market.transitions == [("item1", True)]


async def test_kosell_outage_hides_only_dependent_lots(app):
    e, db, market, kosell = app
    binding(db)
    binding(db, module="stock", item_id="stock")
    db.add_stock("stock", ["account"])

    async def unavailable():
        raise ServiceError("kosell_network")

    kosell.balance = unavailable
    await Listings(e).tick()
    assert market.transitions == [("item1", False)]


async def test_paid_actions_require_opt_in_and_both_limits(app):
    e, db, market, kosell = app
    binding(db, auto_bump=1)
    market.tier_price = 6
    manager = Listings(e)
    await manager.tick()
    assert not market.actions
    db.set("paid_enabled", True)
    db.set("action_limit", 500)
    db.set("daily_budget", 1000)
    await manager.tick()
    assert not market.actions
    db.set("action_limit", 600)
    await manager.tick()
    assert len(market.actions) == 1
    binding(db, item_id="second", auto_bump=1)
    await manager.tick()
    assert len(market.actions) == 1  # remaining daily budget is only 4 rubles


async def test_ambiguous_paid_action_keeps_budget_and_blocks_repetition(app):
    e, db, market, kosell = app
    binding(db, auto_bump=1)
    market.tier_price = 6
    market.action_error = ServiceError("lost", ambiguous=True)
    db.set("paid_enabled", True)
    db.set("action_limit", 1000)
    db.set("daily_budget", 1000)
    manager = Listings(e)
    await manager.tick()
    await manager.tick()
    assert len(market.actions) == 1
    assert db.one("SELECT state FROM expenses")["state"] == "reserved"
    assert db.one("SELECT state FROM listing_actions")["state"] == "uncertain"


async def test_readiness_does_not_require_live_test_but_still_rejects_account_change(app):
    e, db, market, kosell = app
    ready, issues = await readiness(db, market, kosell)
    assert ready and db.get("live_enabled")
    assert db.get("visibility_proof") is None
    db.set("visibility_proof", {"account_id": "irrelevant", "fingerprint": "old-contract"})
    ready, issues = await readiness(db, market, kosell)
    assert ready and db.get("live_enabled")
    market.user_id = "different-account"
    assert not (await readiness(db, market, kosell))[0]


async def test_customer_cannot_access_other_buyers_guard_code(app):
    e, db, market, kosell = app
    binding(db)
    order(e, market)
    await e.process("order1")
    await e.flush(telegram)
    conversations = Conversations(e)
    await conversations.handle(Message("intruder", "other-chat", "intruder", "!код RNT-1", now()))
    assert kosell.code_calls == 0
    await conversations.handle(Message("owner", "chat:buyer", "buyer", "!код RNT-1", now()))
    await conversations.handle(Message("owner2", "chat:buyer", "buyer", "!код RNT-1", now()))
    assert kosell.code_calls == 1


async def test_operator_pause_blocks_automatic_replies_but_acknowledges(app):
    e, db, market, kosell = app
    features = db.get("features")
    features["greeting"] = True
    db.set("features", features)
    conversations = Conversations(e)
    await conversations.handle(Message("m1", "chat", "buyer", "!оператор", now()))
    await conversations.handle(Message("m2", "chat", "buyer", "Здравствуйте", now()))
    await e.flush(telegram)
    assert market.sends == 1
    assert "Передали" in market.history[0].text


async def test_greeting_and_rule_have_persistent_dedup_and_priority(app):
    e, db, market, kosell = app
    features = db.get("features")
    features["greeting"] = True
    db.set("features", features)
    db.set(
        "rules",
        [
            {"phrase": "игра", "reply": "общий", "priority": 1},
            {"phrase": "игра steam", "reply": "точный", "priority": 1},
        ],
    )
    message = Message("m", "chat", "buyer", "Нужна игра Steam", now())
    await Conversations(e).handle(message)
    await Conversations(e).handle(message)
    await e.flush(telegram)
    assert market.sends == 2
    assert [m.text for m in market.history][-1] == "точный"


async def test_customer_reply_cancels_pending_reminders(app):
    e, db, market, kosell = app
    binding(db, module="stock")
    db.add_stock("item1", ["secret"])
    order(e, market)
    await e.process("order1")
    await e.flush(telegram)
    assert db.one("SELECT state FROM jobs WHERE kind='confirm_reminder'")["state"] == "pending"
    await Conversations(e).handle(Message("m", "chat:buyer", "buyer", "Спасибо", now()))
    assert db.one("SELECT state FROM jobs WHERE kind='confirm_reminder'")["state"] == "cancelled"


async def test_reminder_rechecks_current_market_status(app):
    e, db, market, kosell = app
    binding(db, module="stock")
    db.add_stock("item1", ["secret"])
    deal = order(e, market)
    await e.process("order1")
    await e.flush(telegram)
    features = db.get("features")
    features["confirm_reminder"] = True
    db.set("features", features)
    db.execute("UPDATE jobs SET due_at=0")
    market.deals[deal.id] = replace(deal, status="CONFIRMED")
    await e.run_jobs()
    assert db.one("SELECT state FROM jobs WHERE kind='confirm_reminder'")["state"] == "cancelled"


async def test_selecting_after_extension_payment_never_selects_another_game(app):
    e, db, market, kosell = app
    binding(db)
    order(e, market)
    await e.process("order1")
    await e.flush(telegram)
    binding(db, module="extend", item_id="ext", product_id=99)
    order(e, market, "extension", "ext")
    await e.process("extension")
    await Conversations(e).handle(Message("selection", "chat:buyer", "buyer", "!продлить RNT-1", now()))
    assert kosell.extensions == 0
    assert e.order("extension")["stage"] == "wait_selection"


async def test_problem_acknowledgement_is_allowed_while_delivery_is_blocked(app):
    e, db, market, kosell = app
    binding(db, module="stock")
    db.add_stock("item1", ["account-secret"])
    deal = order(e, market)
    await e.process(deal.id)
    features = db.get("features")
    features["problem"] = True
    db.set("features", features)
    market.deals[deal.id] = replace(deal, problem=True)
    e.accept(market.deals[deal.id])
    await e.flush(telegram)
    assert market.sends == 1
    assert "проблему" in market.history[0].text
    assert "account-secret" not in market.history[0].text


async def test_expired_guard_code_is_not_sent_from_delayed_queue(app):
    e, db, market, kosell = app
    db.enqueue("playerok", "chat", "EXPIRED-CODE", "guard-test", expires_at=now() - 1)
    await e.flush(telegram)
    assert market.sends == 0
    assert db.one("SELECT state FROM outbox")["state"] == "cancelled"


async def test_queued_reminder_is_cancelled_when_customer_replies(app):
    e, db, market, kosell = app
    binding(db)
    deal = order(e, market)
    db.enqueue("playerok", deal.chat_id, "reminder", "reminder", purpose="confirm_reminder", order_id=deal.id)
    await Conversations(e).handle(Message("reply", deal.chat_id, "buyer", "Спасибо", now()))
    assert db.one("SELECT state FROM outbox WHERE dedup='reminder'")["state"] == "cancelled"
