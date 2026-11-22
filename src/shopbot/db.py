from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .models import now

DEFAULTS = {
    "paused": False,
    "fee_percent": None,
    "paid_enabled": False,
    "action_limit": 0,
    "daily_budget": 0,
    "work_start": 9,
    "work_end": 23,
    "offline_override": False,
    "daily_report": False,
    "report_time": "23:59",
    "stock_low": 3,
    "rental_low": 2,
    "alerts_enabled": True,
    "update_notifications": True,
    "features": {
        k: False
        for k in (
            "greeting",
            "offline",
            "ignore",
            "confirm_reminder",
            "review_reminder",
            "problem",
            "review",
            "seller_confirmed",
            "buyer_confirmed",
            "auto_confirm",
        )
    },
    "templates": {
        "greeting": "Здравствуйте! Команды: !помощь, !аренды, !оператор.",
        "offline": "Сейчас продавец не в сети. Автовыдача и команды аренды продолжают работать.",
        "ignore": "Если остались вопросы по заказу, напишите нам или используйте !оператор.",
        "confirm_reminder": "Заказ {order} выдан. Если всё работает, подтвердите получение на Playerok.",
        "review_reminder": "Спасибо за покупку {title}! Будем рады вашему честному отзыву.",
        "problem": "Видим проблему по заказу {order}. Передали её продавцу.",
        "review": "Спасибо за отзыв по заказу {order}. Если нужна помощь, напишите !оператор.",
        "seller_confirmed": "Выполнение заказа {order} отмечено продавцом.",
        "buyer_confirmed": "Спасибо за подтверждение заказа {order}!",
    },
    "rules": [],
}


class Database:
    def __init__(self, path: Path, key: str):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self.cipher = Fernet(key.encode())
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(files("shopbot").joinpath("schema.sql").read_text())
        if "expires_at" not in {r[1] for r in self.conn.execute("PRAGMA table_info(outbox)")}:
            self.conn.execute("ALTER TABLE outbox ADD COLUMN expires_at REAL")
        os.chmod(path, 0o600)
        for k, v in DEFAULTS.items():
            self.conn.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (k, json.dumps(v)))
        check = self.get("encryption_check")
        if check:
            try:
                if self.decrypt(check) != "shopbot-key-check-v1":
                    raise ValueError("Ключ шифрования не соответствует базе")
            except (InvalidToken, ValueError):
                self.conn.close()
                raise
        else:
            self.set("encryption_check", self.encrypt("shopbot-key-check-v1"))

    def close(self):
        self.conn.close()

    def encrypt(self, text: str) -> str:
        return self.cipher.encrypt(text.encode()).decode()

    def decrypt(self, text: str) -> str:
        return self.cipher.decrypt(text.encode()).decode()

    def execute(self, sql: str, args=()):
        return self.conn.execute(sql, args)

    def one(self, sql: str, args=()) -> dict | None:
        row = self.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, args=()) -> list[dict]:
        return [dict(r) for r in self.execute(sql, args).fetchall()]

    @contextmanager
    def transaction(self):
        self.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.execute("ROLLBACK")
            raise
        else:
            self.execute("COMMIT")

    def get(self, key: str, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value):
        self.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value, default=str)))

    def setting(self, key: str, item_id: str | None = None):
        overrides = self.get("override:" + (item_id or ""), {})
        return overrides.get(key, self.get(key))

    def feature(self, name: str, item_id: str | None = None) -> bool:
        overrides = self.get("override:" + (item_id or ""), {})
        return bool(overrides.get("features", {}).get(name, self.get("features")[name]))

    def audit(self, event: str, reference: str):
        self.execute(
            "INSERT INTO audit(event,reference,created_at) VALUES (?,?,?)", (event, reference, now())
        )

    def notify(self, text: str, dedup: str, order_id=None, *, purpose="message"):
        for admin in self.get("admins", []):
            self.enqueue(
                "telegram", str(admin), text, f"admin:{admin}:{dedup}", order_id=order_id, purpose=purpose
            )

    def enqueue(
        self,
        channel: str,
        target: str,
        text: str,
        dedup: str,
        *,
        order_id=None,
        purpose="message",
        not_before=0,
        expires_at=None,
    ):
        self.execute(
            "INSERT OR IGNORE INTO outbox(dedup,channel,target,ciphertext,created_at,order_id,purpose,not_before,expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (dedup, channel, target, self.encrypt(text), now(), order_id, purpose, not_before, expires_at),
        )

    def add_stock(self, item_id: str, lines: list[str], cost: int | None = None) -> int:
        clean = [line.strip() for line in lines if line.strip()]
        if not clean or len(clean) > 10000 or any(len(x) > 2500 for x in clean):
            raise ValueError("От 1 до 10000 непустых строк, не более 2500 символов на позицию")
        if cost is not None and cost < 0:
            raise ValueError("Себестоимость не может быть отрицательной")
        with self.transaction():
            self.conn.executemany(
                "INSERT INTO stock(item_id,ciphertext,cost,created_at) VALUES (?,?,?,?)",
                [(item_id, self.encrypt(x), cost, now()) for x in clean],
            )
        return len(clean)

    def reserve_stock(self, order_id: str, item_id: str) -> dict | None:
        with self.transaction():
            row = self.one("SELECT * FROM stock WHERE order_id=?", (order_id,))
            if row:
                return row
            row = self.one(
                "SELECT * FROM stock WHERE item_id=? AND state='free' ORDER BY id LIMIT 1", (item_id,)
            )
            if row:
                self.execute("UPDATE stock SET state='reserved',order_id=? WHERE id=?", (order_id, row["id"]))
            return row

    def backup(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"shop-{int(now())}.sqlite"
        target = sqlite3.connect(path)
        try:
            self.conn.backup(target)
        finally:
            target.close()
        os.chmod(path, 0o600)
        for old in sorted(directory.glob("shop-*.sqlite"), reverse=True)[7:]:
            old.unlink()
        return path
