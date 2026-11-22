from __future__ import annotations

from dataclasses import replace

import pytest
from cryptography.fernet import Fernet

from shopbot.db import Database
from shopbot.engine import Engine
from shopbot.models import Deal, Message, ServiceError, now, utc


class FakeMarket:
    user_id = "seller"
    visibility = {"hide": {"hidden_statuses": ["PAUSED"]}}

    def __init__(self):
        self.deals = {}
        self.history = []
        self.items = {}
        self.sends = 0
        self.lost_send = False
        self.transitions = []
        self.tier_price = 0
        self.actions = []
        self.action_error = None

    async def profile(self):
        return {"id": self.user_id, "username": "myshop", "balance": {"value": 100}}

    def fingerprint(self):
        return "test-contract-fingerprint"

    async def deal(self, order_id):
        return self.deals[order_id]

    async def recent_deal_ids(self, statuses=None):
        for order_id in self.deals:
            if statuses is None or self.deals[order_id].status in statuses:
                yield order_id

    async def chats(self):
        for chat_id in sorted({d.chat_id for d in self.deals.values()}):
            yield {"id": chat_id}

    async def messages(self, chat_id, since=0):
        return [m for m in self.history if m.chat_id == chat_id and m.created_at >= since]

    async def send_message(self, chat_id, text):
        self.sends += 1
        message_id = str(self.sends)
        self.history.append(Message(message_id, chat_id, self.user_id, text, now()))
        if self.lost_send:
            self.lost_send = False
            raise ServiceError("lost_response", ambiguous=True)
        return message_id

    async def mark_sent(self, order_id):
        self.deals[order_id] = replace(self.deals[order_id], status="SENT")

    async def refund(self, order_id):
        self.deals[order_id] = replace(self.deals[order_id], status="ROLLED_BACK")

    async def item(self, item_id):
        return self.items.setdefault(
            item_id, {"id": item_id, "status": "APPROVED", "price": 100, "user": {"id": self.user_id}}
        ).copy()

    async def set_visible(self, item_id, visible):
        await self.item(item_id)
        self.items[item_id]["status"] = "APPROVED" if visible else "PAUSED"
        self.transitions.append((item_id, visible))
        return self.items[item_id]

    async def tiers(self, item):
        return [{"id": "tier", "price": self.tier_price}]

    async def listing_action(self, item_id, tier_id, kind):
        self.actions.append((item_id, tier_id, kind))
        if self.action_error:
            raise self.action_error
        self.items[item_id]["status"] = "APPROVED"
        return self.items[item_id]


class FakeKOSell:
    def __init__(self):
        self.rents = []
        self.cache = {}
        self.extensions = 0
        self.code_calls = 0
        self.price = 10
        self.rub_balance = 1000
        self.available = 5
        self.lost_rent = False
        self.lost_extend = False
        self.state = "ready"
        self.expiry = now() + 86400

    async def balance(self):
        return {"balance_rub": self.rub_balance, "balance_usd": 0}

    async def products(self):
        return [
            {
                "id": 7,
                "name": "Game",
                "available_accounts": self.available,
                "min_hours": 1,
                "max_hours": 100,
                "price_per_hour_rub": self.price,
            }
        ]

    async def quote(self, product, hours):
        return {"total_rub": self.price}

    async def rent(self, body, key):
        self.rents.append((body.copy(), key))
        if key not in self.cache:
            self.cache[key] = {
                "rental_uid": "uid:" + key,
                "rental_number": "RNT-1",
                "expires_at": utc(self.expiry),
                "price_rub": self.price,
                "delivery": {"state": "queued"},
            }
        if self.lost_rent:
            self.lost_rent = False
            raise ServiceError("kosell_network", ambiguous=True)
        return self.cache[key]

    async def delivery(self, uid):
        return {"state": self.state, "deadline": utc(now() + 3600), "refunded": self.state == "failed"}

    async def credentials(self, uid):
        return {"steam_login": "steam-user", "steam_password": "secret-password"}

    async def extension_quote(self, uid, hours):
        return {"total_rub": 5}

    async def extend(self, uid, hours):
        self.extensions += 1
        self.expiry += hours * 3600
        if self.lost_extend:
            raise ServiceError("kosell_network", ambiguous=True)
        return {
            "success": True,
            "rental_uid": uid,
            "new_expires_at": utc(self.expiry),
            "hours_added": hours,
            "price_paid": 5,
            "currency": "RUB",
        }

    async def active(self):
        return [{"rental_uid": "uid:playerok:rent:order1", "expires_at": utc(self.expiry)}]

    async def code(self, uid):
        self.code_calls += 1
        return {"success": True, "code": "ABCDE", "expires_in": 25}


@pytest.fixture
def app(tmp_path):
    db = Database(tmp_path / "shop.sqlite", Fernet.generate_key().decode())
    db.set("admins", [])
    db.set("cutoff", now() - 60)
    db.set("live_enabled", True)
    market, kosell = FakeMarket(), FakeKOSell()
    engine = Engine(db, market, kosell)
    yield engine, db, market, kosell
    db.close()


def binding(db, module="kosell", item_id="item1", **kwargs):
    fields = {
        "item_id": item_id,
        "module": module,
        "product_id": 7 if module != "stock" else None,
        "hours": 1,
        "max_cost": 2000,
    }
    fields.update(kwargs)
    db.execute(
        "INSERT INTO bindings(" + ",".join(fields) + ") VALUES (" + ",".join("?" * len(fields)) + ")",
        tuple(fields.values()),
    )


def order(engine, market, order_id="order1", item_id="item1", buyer="buyer", **kwargs):
    fields = dict(
        id=order_id,
        item_id=item_id,
        buyer_id=buyer,
        buyer_name=buyer,
        chat_id="chat:" + buyer,
        status="PAID",
        created_at=now(),
        price=10000,
        title="Game",
    )
    fields.update(kwargs)
    deal = Deal(**fields)
    market.deals[order_id] = deal
    engine.accept(deal)
    return deal


async def telegram(*args):
    return "telegram-id"
