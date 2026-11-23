"""Own-account leases. Expiry requires the owner's credential rotation before reuse."""

from __future__ import annotations

from .models import ServiceError, now


class Rentals:
    def __init__(self, engine):
        self.engine, self.db = engine, engine.db

    def add(self, item_id, lines, cost=None):
        binding = self.db.one("SELECT fulfillment FROM bindings WHERE item_id=?", (item_id,))
        if not binding or binding["fulfillment"] != "own_rent":
            raise ValueError("Нужен лот собственной аренды")
        if not 1 <= len(lines) <= 1000 or any(not 1 <= len(line.strip()) <= 2500 for line in lines):
            raise ValueError("От 1 до 1000 строк, до 2500 символов каждая")
        if cost is not None and cost < 0:
            raise ValueError("Отрицательная себестоимость")
        with self.db.transaction():
            for line in lines:
                self.db.execute(
                    "INSERT INTO own_accounts(item_id,label,ciphertext,cost) VALUES (?,?,?,?)",
                    (item_id, "Аккаунт", self.db.encrypt(line.strip()), cost),
                )

    def deliver(self, order, binding):
        with self.db.transaction():
            lease = self.db.one("SELECT * FROM own_leases WHERE order_id=?", (order["id"],))
            if not lease:
                account = self.db.one(
                    "SELECT * FROM own_accounts WHERE item_id=? AND state='free' ORDER BY id LIMIT 1",
                    (order["item_id"],),
                )
                if not account:
                    raise ServiceError("own_pool_empty")
                expiry = now() + binding["hours"] * 3600
                self.db.execute(
                    "UPDATE own_accounts SET state='reserved',order_id=?,expires_at=? WHERE id=?",
                    (order["id"], expiry, account["id"]),
                )
                self.db.execute(
                    "INSERT INTO own_leases(order_id,account_id,ciphertext,expires_at) VALUES (?,?,?,?)",
                    (order["id"], account["id"], account["ciphertext"], expiry),
                )
                lease = self.db.one("SELECT * FROM own_leases WHERE order_id=?", (order["id"],))
                self.engine.update(order["id"], expires_at=expiry, rental_number="OWN-" + order["id"])
            account = self.db.one("SELECT cost FROM own_accounts WHERE id=?", (lease["account_id"],))
            text = (
                self.db.decrypt(lease["ciphertext"])
                + "\nАренда до: "
                + self.engine.expiry(lease["expires_at"])
                + "\nНомер аренды: "
                + order["id"]
                + "\n!срок <номер>, !продлить <номер>, !оператор"
            )
            if binding["instruction"]:
                text += "\n\n" + binding["instruction"]
            self.engine.queue_delivery_without_transaction(order, text, account["cost"])

    def extend(self, order, binding):
        with self.db.transaction():
            old = self.db.one("SELECT * FROM own_extensions WHERE order_id=?", (order["id"],))
            target = self.engine.owned_rental(order["buyer_id"], order["target_order_id"])
            if not target or target["module"] != "own_rent" or target["item_id"] != binding["pool_item"]:
                raise ServiceError("own_extension_mismatch")
            lease = self.db.one("SELECT * FROM own_leases WHERE order_id=?", (target["id"],))
            if not lease or lease["state"] != "active" or lease["expires_at"] <= now():
                raise ServiceError("own_rental_expired")
            if not old:
                expiry = lease["expires_at"] + binding["hours"] * 3600
                self.db.execute("INSERT INTO own_extensions VALUES (?,?)", (order["id"], target["id"]))
                self.db.execute("UPDATE own_leases SET expires_at=? WHERE order_id=?", (expiry, target["id"]))
                self.db.execute(
                    "UPDATE own_accounts SET expires_at=? WHERE id=?", (expiry, lease["account_id"])
                )
                self.engine.update(target["id"], expires_at=expiry)
            else:
                expiry = lease["expires_at"]
            self.engine.update(order["id"], expires_at=expiry)
            self.engine.queue_delivery_without_transaction(
                order, "Аренда продлена до " + self.engine.expiry(expiry), 0
            )

    def delivered(self, order_id):
        lease = self.db.one("SELECT * FROM own_leases WHERE order_id=?", (order_id,))
        if lease and lease["state"] == "reserved" and lease["expires_at"] > now():
            self.db.execute("UPDATE own_leases SET state='active' WHERE order_id=?", (order_id,))
            self.db.execute("UPDATE own_accounts SET state='active' WHERE order_id=?", (order_id,))

    def tick(self):
        rows = self.db.all(
            "SELECT a.*,o.buyer_id,o.chat_id,o.market_status FROM own_accounts a "
            "JOIN orders o ON o.id=a.order_id WHERE a.state IN ('reserved','active')"
        )
        for row in rows:
            order_id = row["order_id"]
            if row["expires_at"] <= now() or row["market_status"] == "ROLLED_BACK":
                with self.db.transaction():
                    self.db.execute("UPDATE own_accounts SET state='reset' WHERE id=?", (row["id"],))
                    self.db.execute("UPDATE own_leases SET state='reset' WHERE order_id=?", (order_id,))
                    self.db.execute(
                        "UPDATE outbox SET state='cancelled' WHERE order_id=? AND purpose='delivery' AND state='pending'",
                        (order_id,),
                    )
                    self.db.notify(
                        f"Собственный аккаунт #{row['id']}: аренда {order_id} завершена. "
                        "Смените доступ и подтвердите готовность к новой выдаче.",
                        "own-reset:" + order_id,
                        order_id,
                    )
                    if row["market_status"] != "ROLLED_BACK":
                        self.db.enqueue(
                            "playerok",
                            row["chat_id"],
                            "Срок аренды завершён. По вопросам напишите !оператор.",
                            "own-expired:" + order_id,
                            order_id=order_id,
                            purpose="own_expired",
                        )
            elif row["state"] == "active" and row["expires_at"] - now() <= 900:
                self.db.enqueue(
                    "playerok",
                    row["chat_id"],
                    "До окончания аренды осталось не более 15 минут. Продление: !продлить " + order_id,
                    f"own-remind:{order_id}:{int(row['expires_at'])}",
                    order_id=order_id,
                    expires_at=row["expires_at"],
                )

    def reset(self, account_id, new_credentials, *, expected=None):
        if not 1 <= len(new_credentials.strip()) <= 2500:
            raise ValueError("Нужны новые реквизиты до 2500 символов")
        with self.db.transaction():
            account = self.db.one("SELECT * FROM own_accounts WHERE id=?", (account_id,))
            if not account or account["state"] not in {"reset", "free"}:
                raise ValueError("Аккаунт ещё используется")
            if expected is not None and any(account[key] != value for key, value in expected.items()):
                raise ValueError("Аккаунт изменился после предпросмотра")
            pending = self.db.one(
                "SELECT id FROM outbox WHERE order_id=? AND purpose='delivery' "
                "AND state IN ('pending','sending','uncertain')",
                (account["order_id"],),
            )
            if pending:
                raise ValueError("Сначала сверить неопределённую выдачу")
            if self.db.decrypt(account["ciphertext"]) == new_credentials.strip():
                raise ValueError("Реквизиты должны быть изменены")
            self.db.execute("UPDATE own_leases SET state='closed' WHERE order_id=?", (account["order_id"],))
            self.db.execute(
                "UPDATE own_accounts SET ciphertext=?,state='free',order_id=NULL,expires_at=NULL WHERE id=?",
                (self.db.encrypt(new_credentials.strip()), account_id),
            )
            self.db.audit("own_account_rotated", str(account_id))
