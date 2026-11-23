"""Opt-in purchase rewards, backed by a separate encrypted gift warehouse."""

from __future__ import annotations

import uuid
from datetime import datetime

from .models import now

CONFIRMED_SQL = "('CONFIRMED','CONFIRMED_AUTOMATICALLY')"


class Loyalty:
    def __init__(self, engine):
        self.engine, self.db = engine, engine.db

    def configure(self, every):
        if not 1 <= every <= 10000:
            raise ValueError("Порог от 1 до 10000")
        self.db.set("loyalty_every", every)
        self.db.set("loyalty_since", now())
        self.db.set("loyalty_campaign", uuid.uuid4().hex)
        self.db.set("loyalty_enabled", True)

    def add(self, lines, cost=None):
        if not 1 <= len(lines) <= 10000 or any(not 1 <= len(x.strip()) <= 2500 for x in lines):
            raise ValueError("Нужны строки бонусного склада")
        if cost is not None and cost < 0:
            raise ValueError("Отрицательная себестоимость")
        with self.db.transaction():
            for text in lines:
                self.db.execute(
                    "INSERT INTO bonus_stock(ciphertext,cost) VALUES (?,?)",
                    (self.db.encrypt(text.strip()), cost),
                )
            self.db.set("bonus_stock_generation", self.db.get("bonus_stock_generation", 0) + 1)

    def count(self, buyer_id, since=None):
        return self.db.one(
            "SELECT count(*) n FROM orders WHERE buyer_id=? AND historical=0 "
            "AND stage='delivered' AND problem=0 AND market_status IN "
            + CONFIRMED_SQL
            + " AND created_at>=?",
            (buyer_id, self.db.get("loyalty_since") if since is None else since),
        )["n"]

    def eligible(self, claim):
        return bool(
            claim
            and self.db.get("loyalty_enabled")
            and claim["campaign"] == self.db.get("loyalty_campaign")
            and self.count(claim["buyer_id"], claim["since"]) >= claim["milestone"] * claim["threshold"]
        )

    def tick(self):
        for box in self.db.all(
            "SELECT b.purpose FROM outbox b JOIN bonus_claims c ON b.purpose='bonus:'||c.id "
            "WHERE b.state='sent' AND c.state!='sent' AND b.dedup='bonus:'||c.id||':'||c.generation"
        ):
            self.sent(box["purpose"])
        for claim in self.db.all(
            "SELECT c.* FROM bonus_claims c JOIN outbox b ON b.purpose='bonus:'||c.id "
            "WHERE c.state='queued' AND b.state='cancelled' AND b.dedup='bonus:'||c.id||':'||c.generation"
        ):
            self.cancel(claim)
        if not self.db.get("loyalty_enabled"):
            return
        campaign = self.db.get("loyalty_campaign")
        since, threshold = self.db.get("loyalty_since"), self.db.get("loyalty_every")
        buyers = self.db.all(
            "SELECT buyer_id FROM orders WHERE created_at>=? AND historical=0 GROUP BY buyer_id", (since,)
        )
        for buyer in buyers:
            buyer_id = buyer["buyer_id"]
            count = self.count(buyer_id)
            last = self.db.one(
                "SELECT id,chat_id FROM orders WHERE buyer_id=? AND historical=0 AND stage='delivered' "
                "AND problem=0 AND market_status IN "
                + CONFIRMED_SQL
                + " AND created_at>=? ORDER BY created_at DESC LIMIT 1",
                (buyer_id, since),
            )
            if not last:
                continue
            for milestone in range(1, count // threshold + 1):
                with self.db.transaction():
                    self.db.execute(
                        "INSERT OR IGNORE INTO bonus_claims(campaign,buyer_id,milestone,threshold,since,order_id,created_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (campaign, buyer_id, milestone, threshold, since, last["id"], now()),
                    )
                    claim = self.db.one(
                        "SELECT * FROM bonus_claims WHERE campaign=? AND buyer_id=? AND milestone=?",
                        (campaign, buyer_id, milestone),
                    )
                    if claim["state"] != "waiting":
                        continue
                    stock = self.db.one("SELECT * FROM bonus_stock WHERE state='free' ORDER BY id LIMIT 1")
                    if not stock:
                        self.db.notify(
                            "Бонусный склад пуст: ожидается подарок покупателю.",
                            "bonus-empty:" + campaign + ":" + str(self.db.get("bonus_stock_generation", 0)),
                        )
                        continue
                    self.db.execute("UPDATE bonus_stock SET state='reserved' WHERE id=?", (stock["id"],))
                    self.db.execute(
                        "UPDATE bonus_claims SET state='queued',stock_id=?,cost=?,order_id=? WHERE id=?",
                        (stock["id"], stock["cost"], last["id"], claim["id"]),
                    )
                    claim["order_id"] = last["id"]
                    order = self.engine.order(claim["order_id"])
                    text = f"🎁 Подарок за {threshold} завершённых покупок:\n" + self.db.decrypt(
                        stock["ciphertext"]
                    )
                    instruction = self.db.get("bonus_instruction")
                    if instruction:
                        text += "\n\n" + instruction
                    self.db.enqueue(
                        "playerok",
                        order["chat_id"],
                        text,
                        f"bonus:{claim['id']}:{claim['generation']}",
                        order_id=order["id"],
                        purpose="bonus:" + str(claim["id"]),
                    )

    def validate_send(self, outbox):
        claim = self.db.one("SELECT * FROM bonus_claims WHERE id=?", (int(outbox["purpose"].split(":")[1]),))
        if not claim or outbox["dedup"] != f"bonus:{claim['id']}:{claim['generation']}":
            self.db.execute(
                "UPDATE outbox SET state='cancelled' WHERE id=? AND state='pending'", (outbox["id"],)
            )
            return False
        if claim["state"] in {"queued", "review"} and self.eligible(claim):
            self.db.execute("UPDATE bonus_claims SET state='queued' WHERE id=?", (claim["id"],))
            return True
        if claim["state"] in {"queued", "review"}:
            with self.db.transaction():
                self.db.execute(
                    "UPDATE outbox SET state='cancelled' WHERE id=? AND state='pending'", (outbox["id"],)
                )
            self.cancel(claim)
        return False

    def cancel(self, claim):
        if claim["exposed"]:
            with self.db.transaction():
                self.db.execute("UPDATE bonus_claims SET state='review' WHERE id=?", (claim["id"],))
                self.db.execute(
                    "UPDATE outbox SET state='uncertain' WHERE dedup=? AND state!='sent'",
                    (f"bonus:{claim['id']}:{claim['generation']}",),
                )
                self.db.notify(
                    "Проверьте отправку подарка: данные могли быть переданы покупателю. Бонус удерживается до сверки.",
                    "bonus-review:" + str(claim["id"]),
                    claim["order_id"],
                )
            return
        with self.db.transaction():
            self.db.execute(
                "UPDATE bonus_stock SET state='free' WHERE id=? AND state='reserved'", (claim["stock_id"],)
            )
            self.db.execute(
                "UPDATE bonus_claims SET state='waiting',stock_id=NULL,cost=NULL,generation=generation+1 WHERE id=?",
                (claim["id"],),
            )

    def expose(self, outbox):
        self.db.execute(
            "UPDATE bonus_claims SET exposed=1 WHERE id=?", (int(outbox["purpose"].split(":")[1]),)
        )

    def settle_review(self, claim_id, received, *, generation=None):
        claim = self.db.one("SELECT * FROM bonus_claims WHERE id=?", (claim_id,))
        if not claim or claim["state"] not in {"review", "queued"} or not claim["exposed"]:
            raise ValueError("Сверка уже выполнена")
        if generation is not None and claim["generation"] != generation:
            raise ValueError("Подарок изменился после предпросмотра")
        box = self.db.one(
            "SELECT * FROM outbox WHERE dedup=?", (f"bonus:{claim['id']}:{claim['generation']}",)
        )
        if not box or box["state"] not in {"uncertain", "failed"}:
            raise ValueError("Отправка ещё выполняется или уже подтверждена")
        if received:
            self.db.execute(
                "UPDATE outbox SET state='sent',remote_id='manual-confirmed' WHERE id=?", (box["id"],)
            )
            self.sent(box["purpose"])
        else:
            with self.db.transaction():
                self.db.execute("UPDATE outbox SET state='cancelled' WHERE id=?", (box["id"],))
                self.db.execute("UPDATE bonus_stock SET state='free' WHERE id=?", (claim["stock_id"],))
                self.db.execute(
                    "UPDATE bonus_claims SET state='waiting',stock_id=NULL,cost=NULL,exposed=0,generation=generation+1 WHERE id=?",
                    (claim_id,),
                )
        self.db.audit("bonus_delivery_manually_verified", str(claim_id))

    def sent(self, purpose):
        claim = self.db.one("SELECT * FROM bonus_claims WHERE id=?", (int(purpose.split(":")[1]),))
        if claim and claim["state"] != "sent":
            with self.db.transaction():
                self.db.execute("UPDATE bonus_claims SET state='sent' WHERE id=?", (claim["id"],))
                self.db.execute("UPDATE bonus_stock SET state='used' WHERE id=?", (claim["stock_id"],))
                order = self.engine.order(claim["order_id"])
                if claim["cost"] is not None:
                    self.db.execute(
                        "INSERT OR IGNORE INTO expenses VALUES (?,?,?,?,?,?,?,?)",
                        (
                            "bonus:" + str(claim["id"]),
                            order["id"],
                            order["item_id"],
                            "bonus",
                            claim["cost"],
                            "paid",
                            datetime.now(self.engine.tz).date().isoformat(),
                            now(),
                        ),
                    )
