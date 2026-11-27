# ruff: noqa: F811
import json
from decimal import Decimal

import httpx
import pytest
from conftest import binding, order
from test_telegram_ui import callbacks, panel  # noqa: F401

from shopbot import shop_tools as tools
from shopbot.analytics import Analytics
from shopbot.conversations import Conversations
from shopbot.listings import Listings
from shopbot.models import Message, ServiceError, now, utc
from shopbot.playerok import Playerok
from shopbot.scenarios import choose
from shopbot.wallets import affordable, balance_text, rate

ITEM = "02d57c00-d5bd-4f31-8a31-ab3b2fb2af13"


async def test_own_catalog_paginates_and_excludes_other_seller():
    seen = []

    def request(req):
        values = json.loads(req.url.params["variables"])
        assert values["filter"] == {"userId": "seller"}
        seen.append(values["pagination"]["after"])
        rows = [
            {"node": {"id": "foreign", "user": {"id": "other"}}},
            {"node": {"id": str(len(seen)), "user": {"id": "seller"}}},
        ]
        return httpx.Response(
            200,
            json={
                "data": {
                    "items": {
                        "edges": rows,
                        "pageInfo": {"hasNextPage": len(seen) < 2, "endCursor": "cursor"},
                    }
                }
            },
        )

    api = Playerok({"cookie": "token=secret", "user_agent": "test"}, transport=httpx.MockTransport(request))

    async def profile():
        api.user_id = "seller"
        return {"id": "seller"}

    api.profile = profile
    try:
        assert [r["id"] async for r in api.own_items()] == ["1", "2"]
        assert seen == [None, "cursor"]
    finally:
        await api.close()


async def test_button_catalog_completes_kosell_binding_without_ids(panel):
    ui, session, send = panel
    market = ui.engine.market

    async def items():
        yield {**await market.item(ITEM), "name": "FC 27"}
        yield {"id": "foreign", "name": "Other", "user": {"id": "someone"}}

    market.own_items = items
    await send(callback="bind:new")
    assert "v6:picklot:0" in callbacks(session.calls[-1])
    assert "v6:picklot:1" not in callbacks(session.calls[-1])
    await send(callback="v6:picklot:0")
    await send(callback="s:bindtype:kosell")
    assert "v6:pickgame:0" in callbacks(session.calls[-1])
    await send(callback="v6:pickgame:0")
    assert "v6:hours:1" in callbacks(session.calls[-1])
    await send(callback="v6:hours:1")
    await send("20")
    await send("-")
    row = ui.db.one("SELECT * FROM bindings")
    assert row["item_id"] == ITEM and row["product_id"] == 7 and row["title"] == "FC 27"
    assert row["max_cost"] == 2000
    assert not market.transitions and not ui.engine.kosell.rents


async def test_unavailable_catalog_keeps_link_fallback(panel):
    ui, session, send = panel
    await send(callback="bind:new")
    assert "v6:lotmanual" in callbacks(session.calls[-1])
    await send(callback="v6:lotmanual")
    await send(ITEM)
    assert "s:bindtype:stock" in callbacks(session.calls[-1])


def test_template_whitelist_and_stale_targets_preserve_paid_flags(app):
    _, db, _, _ = app
    binding(db, item_id="source", hours=24, instruction="Guide", auto_bump=1)
    binding(db, item_id="a", hours=1, paused=1)
    binding(db, item_id="b", hours=2)
    tid = tools.save_template(db, "source", "Daily")
    value = tools.get_template(db, tid)
    assert set(value) == {"format", "name", "mode", "hours", "max_cost", "instruction"}
    with pytest.raises(ValueError):
        tools.import_template(db, {**value, "token": "PRIVATE"})
    preview = tools.preview_template(db, tid, ["a", "b"])
    db.execute("UPDATE bindings SET instruction='edited' WHERE item_id='b'")
    result = tools.apply_template(db, preview)
    assert result[0][1] == "Применён" and result[1][1].startswith("Пропущен")
    a = db.one("SELECT * FROM bindings WHERE item_id='a'")
    assert a["hours"] == 24 and a["paused"] == 1 and not a["auto_bump"]
    assert db.one("SELECT hours FROM bindings WHERE item_id='b'")["hours"] == 2
    preview["expires"] = 0
    with pytest.raises(ValueError):
        tools.apply_template(db, preview)


async def test_template_confirmation_is_invalidated_on_navigation(panel):
    ui, session, send = panel
    binding(ui.db, item_id="source", hours=24)
    binding(ui.db, item_id="a", hours=1)
    tid = tools.save_template(ui.db, "source", "Daily")
    rid = ui.db.one("SELECT rowid rid FROM bindings WHERE item_id='a'")["rid"]
    await send(callback=f"v6:tplselect:{tid}:{rid}:0")
    await send(callback=f"v6:tplpreview:{tid}")
    confirm = next(c for c in callbacks(session.calls[-1]) if c and c.startswith("v6:tplapply:"))
    await send(callback="menu:home")
    await send(callback=confirm)
    assert ui.db.one("SELECT hours FROM bindings WHERE item_id='a'")["hours"] == 1
    await send(callback=f"v6:tplselect:{tid}:{rid}:0")
    await send(callback=f"v6:tplpreview:{tid}")
    confirm = next(c for c in callbacks(session.calls[-1]) if c and c.startswith("v6:tplapply:"))
    await send(callback=confirm)
    assert ui.db.one("SELECT hours FROM bindings WHERE item_id='a'")["hours"] == 24
    await send(callback=confirm)
    assert ui.db.one("SELECT count(*) n FROM audit WHERE event='template_applied'")["n"] == 1


async def test_template_json_import_is_complete_and_rejects_secrets(panel):
    ui, session, send = panel
    value = {
        "format": 1,
        "name": "Stock",
        "mode": "stock",
        "hours": 1,
        "max_cost": 0,
        "instruction": "Public instructions",
    }
    await send(callback="v6:tplimport")
    await send(json.dumps({**value, "password": "SECRET"}))
    assert not ui.db.all("SELECT * FROM shop_templates")
    assert "SECRET" not in session.calls[-1].text
    await send(json.dumps(value))
    assert ui.db.one("SELECT count(*) n FROM shop_templates")["n"] == 1
    assert "Stock" in session.calls[-1].text


async def test_customer_note_is_private_and_tag_stops_auto_response(panel):
    ui, session, send = panel
    binding(ui.db)
    order(ui.engine, ui.engine.market)
    rid = ui.db.one("SELECT rowid rid FROM orders")["rid"]
    await send(callback=f"v6:note:{rid}")
    await send("Private note: do-not-send")
    assert b"do-not-send" not in ui.db.path.read_bytes()
    await send(callback=f"v6:tag:{rid}:manual")
    ui.db.set("features", {**ui.db.get("features"), "greeting": True})
    await Conversations(ui.engine).handle(Message("customer-msg", "chat:buyer", "buyer", "Hello", now()))
    assert not ui.db.one("SELECT id FROM outbox WHERE channel='playerok'")
    await send(callback=f"v6:customer:{rid}")
    assert "do-not-send" in session.calls[-1].text


def add_rule(db, **values):
    data = {
        "name": "Guide",
        "phrase": "help",
        "condition": "any",
        "period": "any",
        "action": "reply",
        "reply": "Answer",
        "priority": 10,
        "delay": 0,
        "cooldown": 60,
        "enabled": 1,
    }
    data.update(values)
    db.execute(
        "INSERT INTO automation_rules(" + ",".join(data) + ") VALUES (" + ",".join("?" for _ in data) + ")",
        tuple(data.values()),
    )


async def test_scenarios_use_actual_order_and_dedupe_on_replay(app):
    engine, db, market, _ = app
    binding(db)
    add_rule(db, condition="delivered")
    conversations = Conversations(engine)
    message = Message("msg1", "chat:buyer", "buyer", "I paid! help", now())
    await conversations.handle(message)
    assert not db.one("SELECT id FROM outbox WHERE dedup LIKE 'scenario:%'")
    order(engine, market)
    engine.update("order1", stage="delivered")
    await conversations.handle(Message("msg2", "chat:buyer", "buyer", "help", now()))
    await conversations.handle(Message("msg2", "chat:buyer", "buyer", "help", now()))
    await conversations.handle(Message("msg3", "chat:buyer", "buyer", "help", now()))
    assert db.one("SELECT count(*) n FROM outbox WHERE dedup LIKE 'scenario:%'")["n"] == 1
    assert db.get("scenario_last:chat:buyer")["action"] == "reply"
    assert not market.sends


async def test_rule_builder_can_save_enable_and_preview(panel):
    ui, session, send = panel
    await send(callback="v6:rulenew")
    await send("Help")
    await send("start")
    await send(callback="v6:rulecondition:no_order")
    await send(callback="v6:ruleperiod:any")
    await send(callback="v6:ruleaction:reply")
    await send("Read instructions")
    await send("10")
    await send("0")
    await send("60")
    confirm = next(c for c in callbacks(session.calls[-1]) if c and c.startswith("v6:rulesave:"))
    await send(callback=confirm)
    row = ui.db.one("SELECT * FROM automation_rules")
    assert row["name"] == "Help" and not row["enabled"]
    await send(callback=f"v6:ruletoggle:{row['id']}")
    await send(callback="v6:ruletest")
    await send("How to start?")
    assert "Help" in session.calls[-1].text
    assert not ui.db.one("SELECT id FROM outbox WHERE channel='playerok'")


async def test_rule_priority_delay_and_operator_ack(app):
    engine, db, market, _ = app
    add_rule(db, name="low", priority=1)
    add_rule(db, name="operator", priority=20, action="operator", delay=30)
    assert choose(engine, "help", None)["name"] == "operator"
    await Conversations(engine).handle(Message("msg", "chat:buyer", "buyer", "help", now()))
    row = db.one("SELECT * FROM outbox WHERE dedup='scenario:2:msg'")
    assert row["purpose"] == "operator_ack" and row["not_before"] > now() + 20
    assert row["expires_at"] > row["not_before"]
    assert db.one("SELECT operator_paused FROM chats")["operator_paused"]


@pytest.mark.parametrize(
    "args,expected", [(("100", "10", "20", "1", "200"), "142.86"), (("0", "0", "20", "10", "100"), "10")]
)
def test_price_margin_rounds_up_and_respects_floor(args, expected):
    assert tools.price_suggestion(*args) == Decimal(expected)


@pytest.mark.parametrize(
    "args",
    [
        ("100", "60", "40", "1", "200"),
        ("100", "0", "20", "1", "110"),
        ("NaN", "0", "20", "1", "200"),
        ("100", "-1", "20", "1", "200"),
    ],
)
def test_price_invalid_inputs_do_not_propose_below_target(args):
    with pytest.raises(ValueError):
        tools.price_suggestion(*args)


async def test_price_ui_produces_recommendation_only(panel):
    ui, session, send = panel
    binding(ui.db)
    rid = ui.db.one("SELECT rowid rid FROM bindings")["rid"]
    await send(callback=f"v6:price:{rid}")
    for value in ["20", "10", "1", "100"]:
        await send(value)
    assert "Рекомендуемая цена: 14.29" in session.calls[-1].text
    assert not ui.engine.market.actions and not ui.engine.market.transitions
    assert not ui.engine.kosell.rents


def test_calendar_refunds_excluded_and_dashboard_matches_existing_analytics(app):
    engine, db, market, _ = app
    binding(db)
    order(engine, market)
    engine.update("order1", stage="delivered", expires_at=now() + 1000, cost=200)
    db.execute("UPDATE orders SET market_status='CONFIRMED' WHERE id='order1'")
    order(engine, market, order_id="second")
    engine.update("second", stage="delivered", expires_at=now() + 2000)
    db.execute("UPDATE orders SET market_status='ROLLED_BACK' WHERE id='second'")
    assert len(tools.calendar(db, now(), now() + 86400)) == 1
    data = tools.dashboard(engine, 7)
    assert data["summary"]["gross"] == Analytics(db).summary(start=now() - 7 * 86400)["gross"]
    assert sum(data["bins"].values()) == data["summary"]["gross"]
    assert tools.chart_png(data).startswith(b"\x89PNG")


async def test_stars_package_uses_existing_wizard_without_purchase(panel):
    ui, session, send = panel
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    state = FSMContext(MemoryStorage(), StorageKey(bot_id=ui.bot.id, chat_id=42, user_id=42))
    await state.update_data(edit="x:new_quantity", external_wizard={"mode": "stars", "provider_id": 1})
    await send(callback="v6:home")
    msg = ui.panels[(ui.bot.id, 42, 42)].as_(ui.bot)
    await ui.shop_callback(msg, "v6:pack:100", state)
    assert (await state.get_data())["external_wizard"]["quantity"] == 100
    assert "x:method:balance" in callbacks(session.calls[-1])
    assert not ui.db.all("SELECT * FROM provider_orders")


def test_wallets_remain_separate_and_preserve_usd_precision():
    assert "1.0001" in balance_text({"balance_rub": 0, "balance_usd": "1.0001"})
    assert affordable(
        {"balance_rub": 0, "balance_usd": "1.0001"}, {"total_rub": 100, "total_usd": "1.0001"}, "USD"
    )
    assert not affordable({"balance_rub": 1000, "balance_usd": 0}, {"total_rub": 100, "total_usd": 1}, "USD")
    assert rate({"total_rub": 100, "total_usd": 1}) == 100


async def test_usd_wallet_only_can_rent_and_is_not_hidden(app):
    engine, db, market, kosell = app
    binding(db)
    db.set("kosell_currency", "USD")

    async def balance():
        return {"balance_rub": 0, "balance_usd": "2.0001"}

    async def quote(product, hours):
        return {"total_rub": 10, "total_usd": "0.1001"}

    async def rent(body, key):
        kosell.rents.append((body.copy(), key))
        return {
            "rental_uid": "usd-uid",
            "currency_used": "USD",
            "price_paid": "0.1001",
            "expires_at": utc(now() + 3600),
        }

    kosell.balance = balance
    kosell.quote = quote
    kosell.rent = rent
    await Listings(engine).tick()
    assert not market.transitions
    order(engine, market)
    await engine.process("order1")
    assert engine.order("order1")["stage"] == "sending"
    assert kosell.rents[0][0]["currency"] == "USD"
    stored = db.one("SELECT * FROM kosell_charges")
    assert stored["currency"] == "USD" and stored["amount"] == "0.1001" and stored["estimated"]
    assert engine.order("order1")["cost"] == 1000
    db.set("fee_percent", 0)
    db.execute("UPDATE orders SET market_status='CONFIRMED' WHERE id='order1'")
    assert not Analytics(db).summary()["complete"]


async def test_usd_pending_request_keeps_wallet_when_setting_changes(app):
    engine, db, market, kosell = app
    binding(db)
    order(engine, market)
    db.set("kosell_currency", "USD")

    async def balance():
        return {"balance_rub": 0, "balance_usd": 10}

    async def quote(product, hours):
        return {"total_rub": 10, "total_usd": 1}

    kosell.balance = balance
    kosell.quote = quote
    await engine.prepare_rent(engine.order("order1"), db.one("SELECT * FROM bindings"))
    db.set("kosell_currency", "RUB")

    async def rent(body, key):
        assert body["currency"] == "USD"
        return {"rental_uid": "uid", "price_paid": 1, "currency_used": "USD", "expires_at": utc(now() + 3600)}

    kosell.rent = rent
    await engine.purchase(engine.order("order1"))
    assert engine.order("order1")["stage"] == "preparing"


async def test_usd_extension_uses_selected_wallet_once(app):
    engine, db, market, kosell = app
    binding(db)
    order(engine, market)
    engine.update("order1", stage="delivered", expires_at=now() + 3600, rental_uid="original")
    binding(db, module="extend", item_id="extension")
    order(engine, market, order_id="extend-order", item_id="extension")
    engine.update("extend-order", target_order_id="order1", stage="extension_ready")
    db.set("kosell_currency", "USD")

    async def balance():
        return {"balance_rub": 0, "balance_usd": 1}

    async def quote(uid, hours):
        return {"total_rub": 5, "total_usd": "0.05"}

    calls = []

    async def extend(uid, hours, currency="RUB"):
        calls.append(currency)
        return {"success": True, "currency": "USD", "price_paid": "0.05", "new_expires_at": utc(now() + 7200)}

    kosell.balance = balance
    kosell.extension_quote = quote
    kosell.extend = extend
    await engine.process("extend-order")
    assert calls == ["USD"] and engine.order("extend-order")["stage"] == "sending"
    assert db.one("SELECT amount FROM kosell_charges WHERE order_id='extend-order'")["amount"] == "0.05"


async def test_malformed_usd_result_keeps_provider_id_and_never_issues_credentials(app):
    engine, db, market, kosell = app
    binding(db)
    order(engine, market)
    db.set("kosell_currency", "USD")

    async def balance():
        return {"balance_rub": 0, "balance_usd": 10}

    async def quote(product, hours):
        return {"total_rub": 10, "total_usd": 1}

    async def rent(body, key):
        return {"rental_uid": "saved-provider-id", "price_paid": "NaN", "expires_at": utc(now() + 3600)}

    async def forbidden(uid):
        pytest.fail("Credentials requested after malformed financial response")

    kosell.balance = balance
    kosell.quote = quote
    kosell.rent = rent
    kosell.credentials = forbidden
    await engine.process("order1")
    assert engine.order("order1")["rental_uid"] == "saved-provider-id"
    assert engine.order("order1")["stage"] == "review"


async def test_usd_lost_rent_retries_same_key_and_wallet_without_second_debit(app):
    engine, db, market, kosell = app
    binding(db)
    order(engine, market)
    db.set("kosell_currency", "USD")

    async def balance():
        return {"balance_rub": 0, "balance_usd": 10}

    async def quote(product, hours):
        return {"total_rub": 10, "total_usd": 1}

    charges = {}
    calls = []

    async def rent(body, key):
        calls.append((body.copy(), key))
        if key not in charges:
            charges[key] = {
                "rental_uid": "uid",
                "price_paid": 1,
                "currency_used": "USD",
                "expires_at": utc(now() + 3600),
            }
            raise ServiceError("kosell_network", ambiguous=True)
        return charges[key]

    kosell.balance = balance
    kosell.quote = quote
    kosell.rent = rent
    await engine.process("order1")
    assert engine.order("order1")["stage"] == "rent_pending"
    db.set("kosell_currency", "RUB")
    await engine.process("order1")
    assert len(charges) == 1 and len(calls) == 2 and calls[0] == calls[1]
    assert engine.order("order1")["stage"] == "sending"


async def test_lost_usd_extension_requires_review_without_repeating(app):
    engine, db, market, kosell = app
    binding(db)
    order(engine, market)
    engine.update("order1", stage="delivered", expires_at=now() + 3600, rental_uid="uid")
    binding(db, module="extend", item_id="ext")
    order(engine, market, order_id="ext-order", item_id="ext")
    engine.update("ext-order", stage="extension_ready", target_order_id="order1")
    db.set("kosell_currency", "USD")

    async def balance():
        return {"balance_rub": 0, "balance_usd": 10}

    async def quote(uid, hours):
        return {"total_rub": 5, "total_usd": "0.05"}

    calls = []

    async def extend(uid, hours, currency="RUB"):
        calls.append(currency)
        raise ServiceError("kosell_network", ambiguous=True)

    kosell.balance = balance
    kosell.extension_quote = quote
    kosell.extend = extend
    await engine.process("ext-order")
    await engine.process("ext-order")
    assert calls == ["USD"] and engine.order("ext-order")["stage"] == "review"
    assert json.loads(engine.order("ext-order")["request_json"])["currency"] == "USD"


async def test_wallet_controls_show_both_balances_and_obey_admin_auth(panel):
    ui, session, send = panel

    async def balance():
        return {"balance_rub": "12.50", "balance_usd": "25.0001"}

    ui.engine.kosell.balance = balance
    await send(callback="v6:wallet", user=999)
    assert all(getattr(c, "text", "") == "Нет доступа" for c in session.calls)
    await send(callback="v6:wallet")
    assert "25.0001" in session.calls[-1].text and "12.50" in session.calls[-1].text
    await send(callback="v6:pay:USD")
    assert ui.db.get("kosell_currency") == "USD"
    await send(callback="v6:pay:INVALID")
    assert ui.db.get("kosell_currency") == "USD"


def test_background_listing_counters_do_not_invalidate_template_preview(app):
    _, db, _, _ = app
    binding(db, item_id="source", hours=24)
    binding(db, item_id="target")
    tid = tools.save_template(db, "source", "Template")
    preview = tools.preview_template(db, tid, ["target"])
    db.execute("UPDATE bindings SET healthy_count=10,last_bump=? WHERE item_id='target'", (now(),))
    assert tools.apply_template(db, preview) == [("target", "Применён")]


async def test_edit_disable_delete_rule_cancels_delayed_reply(panel):
    ui, session, send = panel
    add_rule(ui.db, delay=300)
    await Conversations(ui.engine).handle(Message("queued", "chat:buyer", "buyer", "help", now()))
    await send(callback="v6:rule:1")
    assert "v6:ruleedit:1" in callbacks(session.calls[-1])
    await send(callback="v6:ruleedit:1")
    for text in ["Updated", "question"]:
        await send(text)
    await send(callback="v6:rulecondition:any")
    await send(callback="v6:ruleperiod:any")
    await send(callback="v6:ruleaction:reply")
    for text in ["Updated reply", "10", "0", "60"]:
        await send(text)
    confirm = next(c for c in callbacks(session.calls[-1]) if c and c.startswith("v6:rulesave:"))
    await send(callback=confirm)
    row = ui.db.one("SELECT * FROM automation_rules")
    assert row["name"] == "Updated" and not row["enabled"]
    assert ui.db.one("SELECT state FROM outbox WHERE dedup LIKE 'scenario:%'")["state"] == "cancelled"
    await send(callback="v6:ruledelete:1")
    assert not ui.db.all("SELECT * FROM automation_rules")


async def test_read_only_rule_preview_can_use_actual_customer_order(panel):
    ui, session, send = panel
    binding(ui.db)
    order(ui.engine, ui.engine.market)
    ui.engine.update("order1", stage="delivered")
    add_rule(ui.db, condition="delivered")
    rid = ui.db.one("SELECT rowid rid FROM orders")["rid"]
    await send(callback=f"v6:customer:{rid}")
    assert f"v6:ruletest:{rid}" in callbacks(session.calls[-1])
    await send(callback=f"v6:ruletest:{rid}")
    await send("help")
    assert "сработает" in session.calls[-1].text
    assert not ui.db.one("SELECT id FROM outbox WHERE dedup LIKE 'scenario:%'")
