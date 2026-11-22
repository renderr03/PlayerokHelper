from dataclasses import replace

import pytest
from conftest import binding, order, telegram
from cryptography.fernet import Fernet, InvalidToken
from openpyxl import load_workbook

from shopbot.analytics import Analytics
from shopbot.db import Database
from shopbot.models import now


async def test_profit_refunds_costs_and_excel_injection(app, tmp_path):
    e, db, market, kosell = app
    binding(db, module="stock")
    db.set("fee_percent", 10)
    db.add_stock("item1", ["first", "second"], 2000)
    one = order(e, market, "first", title='=HYPERLINK("bad")')
    two = order(e, market, "second")
    for ref in [one.id, two.id]:
        await e.process(ref)
    await e.flush(telegram)
    e.accept(replace(one, status="CONFIRMED"))
    e.accept(replace(two, status="ROLLED_BACK"))
    report = Analytics(db)
    s = report.summary()
    assert s["gross"] == 10000
    assert s["refunds"] == 10000
    assert s["cost"] == 4000  # consumed products are not refunded by the supplier
    assert s["commission"] == 1000
    assert s["profit"] == 5000
    assert s["complete"]
    path = report.export(tmp_path / "report.xlsx")
    workbook = load_workbook(path)
    assert len(workbook.sheetnames) == 10
    assert workbook["Сделки"]["C2"].value.startswith("'=")
    assert all(cell.data_type != "f" for sheet in workbook for row in sheet for cell in row)


async def test_unknown_cost_and_commission_do_not_claim_complete_profit(app):
    e, db, market, kosell = app
    binding(db, module="stock")
    db.add_stock("item1", ["first"])
    deal = order(e, market)
    await e.process(deal.id)
    await e.flush(telegram)
    e.accept(replace(deal, status="CONFIRMED"))
    summary = Analytics(db).summary()
    assert not summary["complete"]
    assert summary["unknown_costs"] == 1
    assert summary["commission"] is None


def test_backup_restores_encrypted_inventory_and_requires_original_key(app, tmp_path):
    e, db, market, kosell = app
    db.add_stock("stock", ["private-login:private-password"], 200)
    backup = db.backup(tmp_path / "backups")
    assert b"private-password" not in backup.read_bytes()
    original_key = db.cipher._signing_key + db.cipher._encryption_key
    import base64

    key = base64.urlsafe_b64encode(original_key).decode()
    restored = Database(backup, key)
    try:
        row = restored.one("SELECT * FROM stock")
        assert restored.decrypt(row["ciphertext"]) == "private-login:private-password"
    finally:
        restored.close()
    with pytest.raises(InvalidToken):
        Database(backup, Fernet.generate_key().decode())


def test_backup_rotation_retains_seven_snapshots(app, tmp_path, monkeypatch):
    e, db, market, kosell = app
    moment = now()
    for i in range(10):
        monkeypatch.setattr("shopbot.db.now", lambda i=i: moment + i * 60)
        db.backup(tmp_path / "backups")
    assert len(list((tmp_path / "backups").glob("shop-*.sqlite"))) == 7


def test_daily_report_is_queued_once_per_day_and_admin(app, tmp_path):
    from types import SimpleNamespace

    from shopbot.runtime import Runtime

    e, db, market, kosell = app
    db.set("daily_report", True)
    db.set("report_time", "00:00")
    runtime = Runtime(SimpleNamespace(data_dir=tmp_path, admins=[42, 43]), db, e, None)
    runtime.maintenance()
    runtime.maintenance()
    rows = db.all("SELECT * FROM outbox WHERE purpose='report'")
    assert len(rows) == 2
    assert {row["target"] for row in rows} == {"42", "43"}
    assert len(list((tmp_path / "backups").glob("*.sqlite"))) == 1
