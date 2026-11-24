import asyncio
import json
from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest
from conftest import binding, order, telegram
from test_telegram_ui import panel as panel

from shopbot.analytics import Analytics
from shopbot.conversations import Conversations
from shopbot.external import destination_for, encoded, save_provider
from shopbot.models import Message, ServiceError, now
from shopbot.providers import ProviderClient


class Supplier:
    def __init__(self, kind):
        self.kind = kind
        self.creates = []
        self.requests = []
        self.orders = {}
        self.state = "processing"
        self.price = "1"
        self.balance = "100"
        self.lost = False
        self.bad = False
        self.action_lost = False
        self.actions = []
        self.currency = "TON" if kind == "split" else "RUB"

    def response(self, value):
        return httpx.Response(200, json={"ok": True, "message": value} if self.kind == "split" else value)

    def __call__(self, request):
        self.requests.append(request)
        if self.kind == "split":
            path = request.url.path
            data = json.loads(request.content) if request.content else {}
            if path == "/balance/":
                funds = int(float(self.balance) * 10**9)
                return self.response(
                    {"ton_balance": {"real": {"available": funds}}, "usdt_balance": {"available": funds}}
                )
            if path == "/recipients/stars":
                return self.response(
                    {"recipient": "resolved-user", "name": "Получатель", "photo": "not-used"}
                )
            if path == "/buy/estimate":
                return self.response({"amount": self.price, "currency": self.currency})
            if path == "/buy/stars":
                self.creates.append(data)
                remote = f"00000000-0000-4000-8000-{len(self.creates):012d}"
                self.orders[remote] = data
                self.balance = str(float(self.balance) - float(self.price))
                if self.lost:
                    raise httpx.ReadTimeout("PRIVATE_TOKEN", request=request)
                if self.bad:
                    return httpx.Response(500, text="PRIVATE_TOKEN")
                return self.response({"invoice": {"id": remote}})
            if path == "/user/invoices":
                rows = []
                for remote, snapshot in self.orders.items():
                    rows.append(
                        {
                            "id": remote,
                            "data": {
                                "type": "stars",
                                "quantity": snapshot["quantity"],
                                "username": snapshot["username"],
                            },
                            "payment": {
                                "amount": self.price,
                                "currency": self.currency,
                                "status": self.state,
                                "paid_at": "2026-11-24T00:00:00Z",
                                "completed_at": "2026-11-24T00:00:01Z" if self.state == "completed" else None,
                            },
                        }
                    )
                return self.response({"items": rows})
            raise AssertionError(path)
        data = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        action = data["action"]
        if action == "balance":
            return self.response({"balance": self.balance, "currency": self.currency})
        if action == "services":
            return self.response(
                [
                    {
                        "service": 77,
                        "type": "Default",
                        "name": "Просмотры",
                        "rate": "2",
                        "min": "10",
                        "max": "10000",
                        "cancel": True,
                        "refill": True,
                    },
                    {"service": 78, "type": "Custom Comments", "name": "Комментарии"},
                ]
            )
        if action == "add":
            self.creates.append(data)
            remote = str(100 + len(self.creates))
            self.orders[remote] = data
            self.balance = str(float(self.balance) - int(data["quantity"]) * 2 / 1000)
            if self.lost:
                raise httpx.ReadTimeout("PRIVATE_TOKEN", request=request)
            return self.response({"order": int(remote)})
        if action == "status":
            quantity = int(self.orders[data["order"]]["quantity"])
            state = {
                "processing": "In progress",
                "completed": "Completed",
                "partial": "Partial",
                "cancelled": "Canceled",
            }[self.state]
            return self.response(
                {
                    "status": state,
                    "charge": "0" if self.state == "cancelled" else self.price,
                    "currency": self.currency,
                    "remains": 0
                    if self.state == "completed"
                    else quantity // 2
                    if self.state == "partial"
                    else quantity,
                }
            )
        if action in {"cancel", "refill"}:
            self.actions.append(action)
            if self.action_lost:
                raise httpx.ReadTimeout("PRIVATE_TOKEN", request=request)
            return self.response({"refill": 12} if action == "refill" else [{"order": 101, "cancel": 1}])
        raise AssertionError(action)


def configure(engine, preset="split", item_id="item1"):
    db = engine.db
    save_provider(db, preset, "TEST_SECRET_TOKEN_DO_NOT_EXPOSE")
    pid = db.one("SELECT id FROM providers WHERE preset=?", (preset,))["id"]
    db.execute(
        "UPDATE providers SET enabled=1,checked_revision=revision,checked_at=?,settings=? WHERE id=?",
        (
            now(),
            encoded({"rates": {"TON": {"value": "250", "at": now()}, "USD": {"value": "90", "at": now()}}}),
            pid,
        ),
    )
    mode = "stars" if preset == "split" else "smm"
    binding(db, module="stock", item_id=item_id, fulfillment=mode, max_cost=50000)
    db.execute(
        "INSERT INTO external_bindings VALUES (?,?,?,?,?,?,?,?)",
        (
            item_id,
            pid,
            "" if mode == "stars" else "77",
            100 if mode == "stars" else 500,
            "balance",
            '["t.me"]',
            "2",
            "TON" if mode == "stars" else "RUB",
        ),
    )
    supplier = Supplier("split" if mode == "stars" else "smm")
    engine.external.transport = httpx.MockTransport(supplier)
    return supplier, pid


async def prepare(
    engine, market, name="order1", buyer="buyer", destination="@recipient_user", item_id="item1"
):
    deal = order(engine, market, name, buyer=buyer, item_id=item_id)
    await engine.process(name)
    message = Message("input:" + name, deal.chat_id, buyer, destination, now())
    await Conversations(engine).handle(message)
    row = engine.db.one("SELECT * FROM provider_orders WHERE order_id=?", (name,))
    assert row["state"] == "draft"
    return deal, row["confirmation"]


async def confirm(engine, deal, token):
    await Conversations(engine).handle(
        Message("confirm:" + token, deal.chat_id, deal.buyer_id, "!подтвердить " + token, now())
    )
    await engine.process(deal.id)


async def test_split_complete_flow_checks_owner_and_does_not_confuse_payment_with_delivery(app):
    engine, db, market, _ = app
    supplier, pid = configure(engine)
    deal, token = await prepare(engine, market)
    await engine.external.message(Message("attack", deal.chat_id, "other", "!подтвердить " + token, now()))
    assert db.one("SELECT state FROM provider_orders")["state"] == "draft"
    await confirm(engine, deal, token)
    await engine.process(deal.id)
    engine.recover()
    assert len(supplier.creates) == 1
    supplier.state = "paid"
    await engine.external.tick()
    assert engine.order(deal.id)["stage"] == "external_pending"
    assert db.one("SELECT state FROM provider_orders")["state"] == "processing"
    supplier.state = "completed"
    db.execute("UPDATE provider_orders SET next_poll=0")
    await engine.external.tick()
    assert engine.order(deal.id)["stage"] == "delivered" and engine.order(deal.id)["cost"] == 25000
    await engine.flush(telegram)
    assert any("выполнен" in message.text for message in market.history)
    with pytest.raises(ValueError):
        engine.external.attach(deal.id, "some-other-id")
    assert b"TEST_SECRET_TOKEN_DO_NOT_EXPOSE" not in db.path.read_bytes()
    assert b"recipient_user" not in db.path.read_bytes()


@pytest.mark.parametrize("failure", ["lost", "bad"])
async def test_uncertain_split_purchase_never_retries_and_manual_link_only_checks_existing_order(
    app, failure
):
    engine, db, market, _ = app
    supplier, pid = configure(engine)
    deal, token = await prepare(engine, market)
    setattr(supplier, failure, True)
    await confirm(engine, deal, token)
    await engine.external.tick()
    engine.recover()
    await engine.process(deal.id)
    assert len(supplier.creates) == 1 and engine.order(deal.id)["stage"] == "review"
    with pytest.raises(ValueError):
        save_provider(db, "split", "NEW_SECRET_TOKEN")
    supplier.lost = False
    supplier.bad = False
    supplier.state = "completed"
    remote = next(iter(supplier.orders))
    engine.external.attach(deal.id, remote)
    await engine.external.tick()
    assert engine.order(deal.id)["stage"] == "delivered" and len(supplier.creates) == 1


async def test_interrupted_intent_and_unknown_response_do_not_free_funds_or_auto_refund(app):
    engine, db, market, _ = app
    supplier, pid = configure(engine)
    deal, token = await prepare(engine, market)
    supplier.lost = True
    await confirm(engine, deal, token)
    db.execute("UPDATE provider_orders SET state='creating'")
    engine.recover()
    assert db.one("SELECT error FROM provider_orders")["error"] == "provider_submission_interrupted"
    assert market.deals[deal.id].status == "PAID"
    assert Analytics(db).summary()["external_pending"] == 1
    assert not Analytics(db).summary()["complete"]


async def test_smm_confirmation_rechecks_limits_and_changed_destination_invalidates_old_token(app):
    engine, db, market, _ = app
    supplier, _ = configure(engine, "twiboost")
    deal, old = await prepare(engine, market, destination="https://t.me/channel/1")
    await engine.external.message(
        Message("new", deal.chat_id, deal.buyer_id, "https://t.me/channel/2", now())
    )
    token = db.one("SELECT confirmation FROM provider_orders")["confirmation"]
    assert old != token
    await confirm(engine, deal, old)
    assert not supplier.creates
    db.execute("UPDATE bindings SET healthy_count=5")
    await confirm(engine, deal, token)
    assert len(supplier.creates) == 1 and supplier.creates[0]["link"] == "https://t.me/channel/2"
    assert supplier.creates[0]["quantity"] == "500"


async def test_expired_confirmation_or_edited_quantity_never_purchases(app):
    engine, db, market, _ = app
    supplier, _ = configure(engine)
    deal, token = await prepare(engine, market)
    db.execute("UPDATE external_bindings SET quantity=200")
    await confirm(engine, deal, token)
    assert not supplier.creates and db.one("SELECT state FROM provider_orders")["state"] == "waiting_input"
    await engine.external.message(Message("again", deal.chat_id, deal.buyer_id, "@recipient_user", now()))
    token = db.one("SELECT confirmation FROM provider_orders")["confirmation"]
    db.execute("UPDATE provider_orders SET confirm_expires=0")
    await confirm(engine, deal, token)
    assert not supplier.creates


async def test_price_increase_low_balance_and_stale_fx_stop_before_submission(app):
    engine, db, market, _ = app
    supplier, pid = configure(engine)
    deal, token = await prepare(engine, market)
    supplier.price = "3"
    await confirm(engine, deal, token)
    assert not supplier.creates
    supplier.price = "1"
    supplier.balance = "0"
    result = await engine.external.message(
        Message("again", deal.chat_id, deal.buyer_id, "@recipient_user", now())
    )
    assert "Не удалось" in result and not supplier.creates
    db.execute(
        "UPDATE providers SET settings=?",
        (encoded({"rates": {"TON": {"value": "250", "at": now() - 90000}}}),),
    )
    with pytest.raises(ServiceError, match="exchange_rate"):
        await engine.external.preview(engine.external.binding("item1"), "recipient_user")


async def test_smm_partial_keeps_actual_cost_after_market_refund_and_manual_settlement(app):
    engine, db, market, _ = app
    supplier, pid = configure(engine, "twiboost")
    deal, token = await prepare(engine, market, destination="https://t.me/channel/1")
    await confirm(engine, deal, token)
    supplier.state = "partial"
    supplier.price = "0.50"
    await engine.external.tick()
    row = db.one("SELECT * FROM provider_orders")
    assert row["state"] == "partial" and market.deals[deal.id].status == "PAID"
    engine.accept(replace(deal, status="ROLLED_BACK"))
    summary = Analytics(db).summary()
    assert summary["cost"] == 50 and summary["external_native"] == "0.50 RUB"
    with pytest.raises(ValueError):
        await engine.external.refund(deal.id)
    with pytest.raises(ValueError):
        engine.external.settle(deal.id, "0.25", "0.25", row["updated_at"] - 1)
    engine.external.settle(deal.id, "0.25", "0.25", row["updated_at"])
    assert Analytics(db).summary()["cost"] == 25
    assert db.one("SELECT supplier_refund FROM provider_orders")["supplier_refund"] == "0.25"
    assert len(supplier.creates) == 1


async def test_smm_zero_delivery_opt_in_auto_refund_is_once_and_not_supplier_refund(app):
    engine, db, market, _ = app
    supplier, pid = configure(engine, "twiboost")
    db.execute("UPDATE providers SET settings=?", (encoded({"auto_refund": True}),))
    deal, token = await prepare(engine, market, destination="https://t.me/channel/1")
    await confirm(engine, deal, token)
    calls = []
    original = market.refund

    async def refund(oid):
        calls.append(oid)
        await original(oid)

    market.refund = refund
    supplier.state = "cancelled"
    await engine.external.tick()
    await engine.external.refund(deal.id)
    assert calls == [deal.id] and market.deals[deal.id].status == "ROLLED_BACK"


async def test_provider_purchase_lock_prevents_two_orders_spending_one_balance(app):
    engine, db, market, _ = app
    supplier, pid = configure(engine, "twiboost")
    supplier.balance = "1"
    first, a = await prepare(engine, market, "first", destination="https://t.me/channel/1")
    second, b = await prepare(engine, market, "second", buyer="another", destination="https://t.me/channel/2")
    await engine.external.message(Message("a", first.chat_id, first.buyer_id, "!подтвердить " + a, now()))
    await engine.external.message(Message("b", second.chat_id, second.buyer_id, "!подтвердить " + b, now()))
    await asyncio.gather(engine.process(first.id), engine.process(second.id))
    assert len(supplier.creates) == 1
    assert {r["state"] for r in db.all("SELECT state FROM provider_orders")} == {
        "processing",
        "waiting_input",
    }


async def test_multiple_orders_in_one_chat_require_explicit_order_and_cannot_cross_buyers(app):
    engine, db, market, _ = app
    supplier, _ = configure(engine)
    a = order(engine, market, "a")
    b = order(engine, market, "b")
    await engine.process(a.id)
    await engine.process(b.id)
    reply = await engine.external.message(Message("plain", a.chat_id, a.buyer_id, "@recipient_user", now()))
    assert "несколько" in reply
    await engine.external.message(
        Message("named", a.chat_id, a.buyer_id, "!получатель a @recipient_user", now())
    )
    assert db.one("SELECT state FROM provider_orders WHERE order_id='a'")["state"] == "draft"
    assert db.one("SELECT state FROM provider_orders WHERE order_id='b'")["state"] == "waiting_input"


@pytest.mark.parametrize(
    "target",
    [
        "http://t.me/channel/1",
        "https://t.me.evil.test/channel/1",
        "https://user:pass@t.me/channel/1",
        "https://t.me/",
        "https://t.me:444/channel",
        "https://127.0.0.1/path",
        "https://t.me/channel#fragment",
    ],
)
def test_smm_link_validation_is_exact_and_never_fetches_targets(target):
    with pytest.raises(ValueError):
        destination_for("smm", target, ["t.me"])


async def test_cancel_and_refill_intents_are_persisted_and_never_repeated_after_timeout(app):
    engine, db, market, _ = app
    supplier, pid = configure(engine, "twiboost")
    deal, token = await prepare(engine, market, destination="https://t.me/channel/1")
    await confirm(engine, deal, token)
    supplier.action_lost = True
    with pytest.raises(ServiceError):
        await engine.external.action(deal.id, "cancel")
    with pytest.raises(ValueError):
        await engine.external.action(deal.id, "cancel")
    assert supplier.actions == ["cancel"]
    assert db.one("SELECT state FROM provider_actions")["state"] == "uncertain"


async def test_provider_disabled_and_buyer_problem_stop_purchase_but_existing_order_still_tracked(app):
    engine, db, market, _ = app
    supplier, pid = configure(engine)
    deal, token = await prepare(engine, market)
    market.deals[deal.id] = replace(deal, problem=True)
    await confirm(engine, deal, token)
    assert not supplier.creates
    market.deals[deal.id] = deal
    await engine.process(deal.id)
    assert len(supplier.creates) == 1
    db.execute("UPDATE providers SET enabled=0")
    supplier.state = "completed"
    await engine.external.tick()
    assert engine.order(deal.id)["stage"] == "delivered"


async def test_provider_client_rejects_redirects_without_leaking_key_or_retrying():
    calls = []

    def redirect(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.test/"})

    client = ProviderClient("split", "PRIVATE_TOKEN", transport=httpx.MockTransport(redirect))
    try:
        with pytest.raises(ServiceError) as error:
            await client.create({"quantity": 100, "payment_method": "balance"}, "username", "ref")
        assert error.value.ambiguous and "PRIVATE_TOKEN" not in str(error.value) and len(calls) == 1
    finally:
        await client.close()


async def test_telegram_stars_setup_launch_confirmation_and_lot_wizard(panel):
    ui, session, send = panel
    supplier, pid = configure(ui.engine, item_id="existing")
    ui.db.execute("UPDATE providers SET enabled=0")
    await send(callback=f"x:check:{pid}")
    await send(callback=f"x:toggle:{pid}")
    callback = session.calls[-1].reply_markup.inline_keyboard[0][0].callback_data
    await send(callback=callback, user=999)
    assert not ui.db.one("SELECT enabled FROM providers")["enabled"]
    await send(callback=callback)
    assert ui.db.one("SELECT enabled FROM providers")["enabled"]
    await send(callback="x:new:stars")
    await send("40ed7272-a692-44cd-b6ce-5d3d1cb2b086")
    await send("100 Stars")
    await send(callback=f"x:choose:{pid}")
    await send("100")
    await send(callback="x:method:balance")
    await send("2")
    await send("500")
    await send("Инструкция")
    callback = session.calls[-1].reply_markup.inline_keyboard[0][0].callback_data
    await send(callback=callback)
    row = ui.db.one("SELECT * FROM bindings WHERE title='100 Stars'")
    assert row["fulfillment"] == "stars" and row["paused"] == 1
    assert "Загрузить склад" not in str(session.calls[-1].reply_markup)
    assert not supplier.creates and "TEST_SECRET_TOKEN" not in str(session.calls)


async def test_completed_provider_order_recovers_after_crash_before_local_delivery_record(app):
    engine, db, market, _ = app
    supplier, _ = configure(engine)
    deal, token = await prepare(engine, market)
    await confirm(engine, deal, token)
    db.execute("UPDATE provider_orders SET state='completed',cost_known=1")
    engine.recover()
    await engine.external.tick()
    await engine.external.tick()
    assert engine.order(deal.id)["stage"] == "delivered" and len(supplier.creates) == 1
    assert db.one("SELECT count(*) n FROM outbox WHERE purpose='external_done'")["n"] == 1


async def test_unknown_debit_holds_other_purchases_until_reconciled(app):
    engine, db, market, _ = app
    supplier, pid = configure(engine)
    first, token = await prepare(engine, market)
    supplier.lost = True
    await confirm(engine, first, token)
    second = order(engine, market, "second", buyer="second-buyer")
    await engine.process(second.id)
    reply = await engine.external.message(
        Message("recipient2", second.chat_id, second.buyer_id, "@another_user", now())
    )
    assert "Не удалось" in reply and len(supplier.creates) == 1
    assert not engine.external.enabled(engine.external.provider(pid))


async def test_ambiguous_market_refund_never_retries_automatically(app):
    engine, db, market, _ = app
    supplier, _ = configure(engine, "twiboost")
    db.execute("UPDATE providers SET settings=?", (encoded({"auto_refund": True}),))
    deal, token = await prepare(engine, market, destination="https://t.me/channel/1")
    await confirm(engine, deal, token)
    calls = []

    async def refund(oid):
        calls.append(oid)
        raise ServiceError("lost_refund", ambiguous=True)

    market.refund = refund
    supplier.state = "cancelled"
    await engine.external.tick()
    await engine.external.refund(deal.id)
    assert calls == [deal.id] and db.get("refund_pending:" + deal.id)
    assert market.deals[deal.id].status == "PAID"


async def test_smm_ui_wizard_and_manual_settlement_require_confirmation(panel):
    ui, session, send = panel
    supplier, pid = configure(ui.engine, "twiboost")
    await send(callback=f"x:check:{pid}")
    await send(callback="x:new:smm")
    item = "40ed7272-a692-44cd-b6ce-5d3d1cb2b086"
    for value in (item, "Просмотры"):
        await send(value)
    await send(callback=f"x:choose:{pid}")
    await send(callback=f"x:service:{pid}:77")
    for value in ("500", "2", "100", "t.me", "-"):
        await send(value)
    callback = session.calls[-1].reply_markup.inline_keyboard[0][0].callback_data
    await send(callback=callback)
    assert ui.db.one("SELECT quantity FROM external_bindings WHERE item_id=?", (item,))["quantity"] == 500
    deal, token = await prepare(ui.engine, ui.engine.market, destination="https://t.me/channel/1")
    await confirm(ui.engine, deal, token)
    supplier.state = "partial"
    await ui.engine.external.tick()
    rid = ui.db.one("SELECT rowid rid FROM orders WHERE id=?", (deal.id,))["rid"]
    await send(callback=f"x:settle:{rid}")
    await send("0.50 | 0.50")
    callback = session.calls[-1].reply_markup.inline_keyboard[0][0].callback_data
    assert ui.db.one("SELECT state FROM provider_orders")["state"] == "partial"
    await send(callback=callback, user=999)
    assert ui.db.one("SELECT state FROM provider_orders")["state"] == "partial"
    await send(callback=callback)
    assert ui.db.one("SELECT state FROM provider_orders")["state"] == "closed"


def test_beta_cannot_request_stable_auto_update_and_ranks_before_final(app, tmp_path, monkeypatch):
    from shopbot.updates import Updates, version_rank

    monkeypatch.setattr("shopbot.updates.__version__", "0.5.0b1")
    _, db, _, _ = app
    (tmp_path / ".updates").mkdir()
    (tmp_path / ".updates/enabled.json").write_text('{"protocol":1}')
    db.set("update_release", {"tag": "v0.5.0", "sha": "b" * 40})
    updates = Updates(db, tmp_path)
    assert updates.available() and not updates.enabled()
    with pytest.raises(ValueError):
        updates.offer()
    assert version_rank("0.4.1") < version_rank("0.5.0b1") < version_rank("v0.5.0")


async def test_external_preview_is_read_only_and_report_contains_native_costs(app, tmp_path):
    from openpyxl import load_workbook

    engine, db, market, _ = app
    supplier, pid = configure(engine, "twiboost")
    db.set("visibility_proof", {"account_id": market.user_id, "fingerprint": market.fingerprint()})
    from shopbot.checks import preview_delivery

    db.execute("UPDATE providers SET checked_at=0")
    before = list(db.conn.iterdump())
    result = await preview_delivery(engine, "item1")
    assert not result.issues and not supplier.creates
    assert before == list(db.conn.iterdump())
    deal, token = await prepare(engine, market, destination="https://t.me/channel/1")
    await confirm(engine, deal, token)
    supplier.state = "completed"
    await engine.external.tick()
    path = Analytics(db).export(tmp_path / "report.xlsx")
    book = load_workbook(path)
    assert len(book.worksheets) == 10
    headers = [c.value for c in book["Сделки"][1]]
    assert "Возврат поставщика (справочно)" in headers and "Курс, руб." in headers
    cells = [c.value for c in book["Сделки"][2]]
    assert "RUB" in cells and "101" in cells
    book.close()
