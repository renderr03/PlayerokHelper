import asyncio
import io
import json
import os
import zipfile
from types import SimpleNamespace

import pytest
from conftest import binding, order
from test_telegram_ui import panel as panel

from shopbot.checks import preview_delivery
from shopbot.conversations import Conversations
from shopbot.maintenance import diagnostics, prune_updates, require_update_space
from shopbot.models import Message, now
from shopbot.runtime import Runtime


async def test_catalog_reaches_beyond_40_and_searches_russian(panel):
    ui, session, send = panel
    for i in range(205):
        binding(ui.db, module="stock", item_id=f"item{i:04}", title=f"Игра {i:04}")
    await send(callback="bindings:all")
    assert "найдено 205" in session.calls[-1].text
    await send(callback="s:page:10")
    labels = [b.text for row in session.calls[-1].reply_markup.inline_keyboard for b in row]
    assert any("0204" in label for label in labels)
    await send(callback="s:search")
    await send("ИГРА 0204")
    assert "найдено 1" in session.calls[-1].text
    assert "0204" in session.calls[-1].reply_markup.inline_keyboard[0][0].text
    await send(callback="analytics:items")
    assert any(
        b.callback_data.startswith("reportitem:")
        for row in session.calls[-1].reply_markup.inline_keyboard
        for b in row
    )


async def test_bulk_preview_skips_changed_lots_and_cannot_be_replayed(panel):
    ui, session, send = panel
    binding(ui.db, module="stock", item_id="A", instruction="before")
    binding(ui.db, module="stock", item_id="B", instruction="before")
    await send(callback="bindings:all")
    await send(callback="s:select-page")
    await send(callback="s:bulk-field:instruction")
    await send("after")
    callback = session.calls[-1].reply_markup.inline_keyboard[0][0].callback_data
    ui.db.execute("UPDATE bindings SET instruction='modified elsewhere' WHERE item_id='B'")
    await send(callback=callback)
    assert ui.db.one("SELECT instruction FROM bindings WHERE item_id='A'")["instruction"] == "after"
    assert (
        ui.db.one("SELECT instruction FROM bindings WHERE item_id='B'")["instruction"] == "modified elsewhere"
    )
    assert "пропущено: 1" in session.calls[-1].text
    report = next(
        call.document
        for call in session.calls
        if "Результат по каждому выбранному лоту." in (getattr(call, "caption", None) or "")
    )
    assert "A: сохранено" in report.data.decode() and "B: пропущен" in report.data.decode()
    await send(callback=callback)
    assert "устарел" in session.calls[-1].text
    await send(callback="s:select-page", user=999)
    assert not ui.db.all("SELECT * FROM expenses")


async def test_inbox_local_read_marks_and_quick_reply_preview(panel):
    ui, session, send = panel
    binding(ui.db, module="stock")
    order(ui.engine, ui.engine.market)
    conversations = Conversations(ui.engine)
    await conversations.handle(Message("incoming", "chat:buyer", "buyer", "Нужна помощь", now()))
    await send("/inbox")
    assert "1 чатов" in session.calls[-1].text
    rid = ui.db.one("SELECT rowid rid FROM chats WHERE id='chat:buyer'")["rid"]
    await send(callback="s:chat:" + str(rid))
    assert "Нужна помощь" in session.calls[-1].text
    await send("/inbox")
    assert "0 чатов" in session.calls[-1].text
    ui.db.set("quick_replies", {"Помощь": "Сейчас проверим заказ"})
    await send(callback=f"s:quick:choose:{rid}")
    await send(callback="s:quick:use:0")
    callback = session.calls[-1].reply_markup.inline_keyboard[0][0].callback_data
    assert not ui.db.all("SELECT * FROM outbox WHERE purpose='manual_reply'")
    await send(callback=callback)
    assert ui.db.one("SELECT target FROM outbox WHERE purpose='manual_reply'")["target"] == "chat:buyer"
    await ui.engine.flush(lambda *args: asyncio.sleep(0, result="id"))
    assert ui.db.one("SELECT last_seller_at FROM chats")["last_seller_at"] > 0


async def test_own_pool_wizard_upload_preview_and_rotation_confirmation(panel):
    ui, session, send = panel
    item_id = "40ed7272-a692-44cd-b6ce-5d3d1cb2b086"
    await send("/bind")
    await send(item_id)
    await send(callback="s:bindtype:own_rent")
    for value in ["Моя аренда", "1", "Инструкция"]:
        await send(value)
    row = ui.db.one("SELECT rowid rid,* FROM bindings")
    assert row["fulfillment"] == "own_rent" and row["module"] == "stock"
    await send(callback=f"s:own-upload:{row['rid']}")
    await send("cost=10\nlogin:private-password")
    assert ui.db.one("SELECT count(*) n FROM own_accounts")["n"] == 1
    ui.db.set(
        "visibility_proof",
        {"account_id": ui.engine.market.user_id, "fingerprint": ui.engine.market.fingerprint()},
    )
    before = list(ui.db.conn.iterdump())
    preview = await preview_delivery(ui.engine, item_id)
    assert not preview.issues
    assert "private-password" not in preview.text(ui.engine.expiry)
    assert before == list(ui.db.conn.iterdump())
    await send(callback="s:reset:1")
    await send("login:changed-password")
    callback = session.calls[-1].reply_markup.inline_keyboard[0][0].callback_data
    assert "changed-password" not in session.calls[-1].text
    assert (
        ui.db.decrypt(ui.db.one("SELECT ciphertext FROM own_accounts")["ciphertext"])
        == "login:private-password"
    )
    await send(callback=callback)
    assert (
        ui.db.decrypt(ui.db.one("SELECT ciphertext FROM own_accounts")["ciphertext"])
        == "login:changed-password"
    )


async def test_bonus_configuration_is_opt_in_and_upload_is_separate(panel):
    ui, session, send = panel
    assert not ui.db.get("loyalty_enabled")
    await send(callback="s:loyalty-toggle")
    assert ui.db.get("loyalty_enabled")
    await send(callback="s:bonus-upload")
    await send("cost=0\ngift:private-gift")
    assert ui.db.one("SELECT count(*) n FROM bonus_stock")["n"] == 1
    assert not ui.db.all("SELECT * FROM stock")
    await send(callback="s:loyalty-threshold")
    await send("3")
    assert ui.db.get("loyalty_every") == 3


async def test_margin_estimate_and_alerts_never_change_prices_or_purchase(app):
    from shopbot.listings import Listings

    engine, db, market, kosell = app
    binding(db, margin_min=8500)
    db.set("admins", [42])
    db.set("fee_percent", 10)
    listings = Listings(engine)
    await listings.tick()
    assert db.get("margin:item1")["margin"] == 8000
    assert db.one("SELECT count(*) n FROM outbox WHERE dedup LIKE '%alert:margin:%'")["n"] == 1
    await listings.tick()
    assert db.one("SELECT count(*) n FROM outbox WHERE dedup LIKE '%alert:margin:%'")["n"] == 1
    kosell.price = 5
    await listings.tick()
    assert db.one("SELECT count(*) n FROM outbox WHERE dedup LIKE '%alert:margin:%'")["n"] == 2
    db.set("fee_percent", None)
    await listings.tick()
    assert not db.get("margin:item1")["known"]
    assert not kosell.rents and not market.actions
    assert market.items["item1"]["price"] == 100


def test_diagnostic_archive_does_not_copy_user_data_or_secrets(app, tmp_path):
    _, db, _, _ = app
    secret = "TOP_PRIVATE_TOKEN_OR_CHAT"
    db.set("profile", {"username": secret})
    db.set("unrelated", secret)
    db.set(
        "connection_probe",
        {
            "at": 1795617151,
            "cookie": secret,
            "checks": [
                {"endpoint": "site", "status": 403, "kind": "html", "security": True, "body": secret},
                {"endpoint": "api", "status": 403, "kind": "html", "security": True, "headers": secret},
            ],
        },
    )
    db.add_stock("secret", [secret])
    (tmp_path / ".env").write_text(secret)
    archive = zipfile.ZipFile(io.BytesIO(diagnostics(db, tmp_path)))
    assert archive.namelist() == ["diagnostics.json"]
    data = archive.read("diagnostics.json")
    assert secret.encode() not in data
    payload = json.loads(data)
    assert "version" in payload
    assert payload["connection"]["transport"] == "curl_cffi"
    assert payload["connection"]["packages"]["curl_cffi"] == "0.17.0"
    assert payload["connection"]["probe"]["checks"][1]["status"] == 403


def test_update_retention_keeps_three_backups_active_and_previous_environment(tmp_path):
    folder = tmp_path / ".updates"
    ids = [f"{i:032x}" for i in range(5)]
    for i, nonce in enumerate(ids):
        backup = folder / "backups" / nonce
        backup.mkdir(parents=True)
        (backup / "shop.sqlite").write_text("backup")
        os.utime(backup, (i + 1, i + 1))
        (folder / "releases" / nonce / ".venv").mkdir(parents=True)
        if i != 4:
            (folder / ("env-before-" + nonce)).mkdir()
    (tmp_path / ".venv").symlink_to(folder / "releases" / ids[4] / ".venv")
    (folder / ("env-before-" + ids[4])).symlink_to(folder / "releases" / ids[1] / ".venv")
    (folder / "prepared.json").write_text(
        json.dumps({"phase": "finished", "status": "success", "request": {"nonce": ids[4]}})
    )
    assert prune_updates(tmp_path) > 0
    assert len(list((folder / "backups").iterdir())) == 3
    assert (folder / "releases" / ids[4] / ".venv").exists()
    assert (folder / "releases" / ids[1] / ".venv").exists()
    assert not (folder / "releases" / ids[0]).exists()
    (folder / "request.json").write_text("{}")
    assert prune_updates(tmp_path) == 0


def test_low_disk_space_rejects_update_before_request(tmp_path, monkeypatch):
    monkeypatch.setattr("shopbot.maintenance.shutil.disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(ValueError, match="места"):
        require_update_space(tmp_path, tmp_path / "shop.sqlite")


async def test_slow_listing_monitor_does_not_block_order_schedule(app, tmp_path):
    engine, db, _, _ = app
    runtime = Runtime(SimpleNamespace(project_dir=tmp_path, updates_enabled=False), db, engine, None)
    seen = asyncio.Event()
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(60)

    async def fast():
        seen.set()

    tasks = [
        asyncio.create_task(runtime.scheduled("listings", slow, 60)),
        asyncio.create_task(runtime.scheduled("orders", fast, 0.01)),
    ]
    try:
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(seen.wait(), 1)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_v03_database_upgrades_preserving_stock_key_and_existing_settings(tmp_path):
    import sqlite3
    from importlib.resources import files

    from cryptography.fernet import Fernet

    from shopbot.db import Database

    key = Fernet.generate_key()
    cipher = Fernet(key)
    path = tmp_path / "legacy.sqlite"
    old_schema = (
        files("shopbot")
        .joinpath("schema.sql")
        .read_text()
        .split("CREATE TABLE IF NOT EXISTS own_accounts")[0]
    )
    db = sqlite3.connect(path)
    db.executescript(old_schema)
    db.execute("INSERT INTO settings VALUES ('paused','true')")
    db.execute(
        "INSERT INTO settings VALUES (?,?)",
        ("encryption_check", json.dumps(cipher.encrypt(b"shopbot-key-check-v1").decode())),
    )
    db.execute("INSERT INTO bindings(item_id,module,title) VALUES ('old','stock','Прежний лот')")
    db.execute(
        "INSERT INTO stock(item_id,ciphertext,created_at) VALUES (?,?,?)",
        ("old", cipher.encrypt(b"old-secret").decode(), now()),
    )
    db.commit()
    db.close()
    upgraded = Database(path, key.decode())
    try:
        assert upgraded.get("paused") is True and upgraded.get("loyalty_enabled") is False
        assert upgraded.one("SELECT fulfillment FROM bindings")["fulfillment"] == ""
        assert upgraded.decrypt(upgraded.one("SELECT ciphertext FROM stock")["ciphertext"]) == "old-secret"
    finally:
        upgraded.close()


async def test_telegram_install_check_sanitizes_errors(monkeypatch):
    from unittest.mock import AsyncMock

    from shopbot.cli import telegram_check
    from shopbot.models import ServiceError

    fake = SimpleNamespace(
        get_me=AsyncMock(side_effect=RuntimeError("PRIVATE_TOKEN")),
        session=SimpleNamespace(close=AsyncMock()),
    )
    monkeypatch.setattr("aiogram.Bot", lambda token: fake)
    with pytest.raises(ServiceError) as error:
        await telegram_check(SimpleNamespace(token="fake"))
    assert "PRIVATE_TOKEN" not in str(error.value)
    fake.session.close.assert_awaited_once()


async def test_playerok_cooldown_blocks_requests_without_repeating_a_mutation():
    import httpx

    from shopbot.models import ServiceError
    from shopbot.playerok import Playerok

    calls = []

    def limited(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "60"})

    market = Playerok({"cookie": "fake", "user_agent": "test"}, transport=httpx.MockTransport(limited))
    try:
        with pytest.raises(ServiceError):
            await market.profile()
        with pytest.raises(ServiceError):
            await market.mark_sent("order")
        assert len(calls) == 1
    finally:
        await market.close()


def test_workflow_diagnostics_preserve_phases_and_exclude_secrets(app, tmp_path):
    _, db, _, _ = app
    secret = "PRIVATE-SESSION-AND-KEY"
    db.set(
        "visibility_test_pending",
        {"item_id": secret, "account_id": secret, "phase": "restore_requested", "cookie": secret},
    )
    db.set("visibility_last_status", "DISCONTINUED")
    db.set("kosell_last_check", {"ok": False, "reason": "timeout", "at": 1795760198, "token": secret})
    archive = zipfile.ZipFile(io.BytesIO(diagnostics(db, tmp_path)))
    raw = archive.read("diagnostics.json")
    assert secret.encode() not in raw
    workflows = json.loads(raw)["workflows"]
    assert workflows["visibility"] == {
        "pending": True,
        "last_error": None,
        "phase": "restore_requested",
        "last_status": "DISCONTINUED",
    }
    assert workflows["kosell_check"]["reason"] == "timeout"
