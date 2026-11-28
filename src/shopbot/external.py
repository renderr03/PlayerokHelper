"""External fulfilment: durable intent, buyer confirmation and reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from contextlib import asynccontextmanager
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlsplit, urlunsplit

from .alerts import Alerts
from .models import ServiceError, now
from .providers import PRESETS, ProviderClient, amount
from .ui_texts import issue_text

MODES = {"stars", "smm"}
UNSETTLED = {"creating", "processing", "review", "partial"}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def destination_for(mode, value, domains):
    value = value.strip()
    if mode == "stars":
        if value.startswith("https://t.me/"):
            value = value[len("https://t.me/") :].rstrip("/")
        value = value.removeprefix("@")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", value):
            raise ValueError("Нужен публичный @username получателя, без пробелов и ссылок на посты")
        return value.casefold()
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or host not in domains
        or not parsed.path
        or parsed.path == "/"
        or parsed.fragment
        or len(value) > 1500
        or any(c.isspace() for c in value)
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("Нужна HTTPS-ссылка на разрешённый сайт и конкретную страницу или публикацию")
    # Do not fetch customer URLs, follow redirects or send cookies to them.
    return urlunsplit(("https", host, parsed.path, parsed.query, ""))


def save_provider(db, preset, token):
    if preset not in PRESETS or not 8 <= len(token) <= 4000 or any(c.isspace() for c in token):
        raise ValueError("Проверьте поставщика и ключ")
    with db.transaction():
        row = db.one("SELECT * FROM providers WHERE preset=?", (preset,))
        if row:
            if db.one(
                "SELECT order_id FROM provider_orders WHERE provider_id=? "
                "AND state IN ('creating','processing','review','partial')",
                (row["id"],),
            ):
                raise ValueError("Сначала завершите сверку текущих заказов этого поставщика")
            db.execute(
                "UPDATE providers SET token=?,revision=revision+1,enabled=0,checked_revision=0 WHERE id=?",
                (db.encrypt(token), row["id"]),
            )
        else:
            db.execute(
                "INSERT INTO providers(preset,title,token) VALUES (?,?,?)",
                (preset, PRESETS[preset][2], db.encrypt(token)),
            )
        db.audit("provider_credentials_changed", preset)


class External:
    def __init__(self, engine):
        self.engine, self.db = engine, engine.db
        self.locks = {}
        self.api_slots = asyncio.Semaphore(4)
        self.transport = None

    @asynccontextmanager
    async def client(self, provider):
        async with self.api_slots:
            client = ProviderClient(
                provider["preset"], self.db.decrypt(provider["token"]), transport=self.transport
            )
            try:
                yield client
            finally:
                await client.close()

    def provider(self, provider_id):
        row = self.db.one("SELECT * FROM providers WHERE id=?", (provider_id,))
        if not row:
            raise ValueError("Поставщик не найден")
        return row

    def binding(self, item_id):
        row = self.db.one(
            "SELECT b.*,e.provider_id,e.service_id,e.quantity,e.payment_method,e.domains,"
            "e.max_native,e.currency FROM bindings b JOIN external_bindings e USING(item_id) "
            "WHERE item_id=?",
            (item_id,),
        )
        if not row:
            raise ValueError("Настройте привязку внешней услуги")
        row["domains"] = json.loads(row["domains"])
        return row

    def fingerprint(self, binding, provider):
        stable = {
            k: v
            for k, v in binding.items()
            if k
            in {
                "item_id",
                "fulfillment",
                "provider_id",
                "service_id",
                "quantity",
                "payment_method",
                "domains",
                "max_native",
                "currency",
                "max_cost",
                "instruction",
                "paused",
            }
        }
        return hashlib.sha256(
            encoded([stable, provider["revision"], provider["settings"]]).encode()
        ).hexdigest()

    def settings(self, provider):
        return {
            "confirm": True,
            "auto_refund": False,
            "notify": True,
            "low": {"TON": "1.5", "USD": "5", "RUB": "300"},
            "rates": {},
            **json.loads(provider["settings"]),
        }

    def rate(self, provider, unit):
        if unit == "RUB":
            return Decimal(1)
        rate = self.settings(provider)["rates"].get(unit, {})
        value = amount(rate.get("value", 0))
        if not value or now() - rate.get("at", 0) > 86400:
            raise ServiceError("provider_exchange_rate_required")
        return value

    def cost(self, value, rate):
        return int((amount(value) * rate * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))

    def enabled(self, provider):
        held = self.db.one(
            "SELECT order_id FROM provider_orders WHERE provider_id=? AND state='review' AND remote_id IS NULL AND native_amount IS NOT NULL",
            (provider["id"],),
        )
        return provider["enabled"] and provider["checked_revision"] == provider["revision"] and not held

    async def check(self, provider_id):
        provider = self.provider(provider_id)
        if self.db.get("provider_retry:" + str(provider_id), 0) > now():
            raise ServiceError("provider_check_cooldown")
        try:
            async with self.client(provider) as client:
                balances = await client.balance()
                catalog = await client.services() if client.kind == "smm" else []
        except (ServiceError, KeyError, ValueError, TypeError, ArithmeticError):
            self.db.set("provider_retry:" + str(provider_id), now() + 60)
            raise ServiceError("provider_check_failed") from None
        with self.db.transaction():
            self.db.execute(
                "UPDATE providers SET checked_at=?,checked_revision=? WHERE id=? AND revision=?",
                (now(), provider["revision"], provider_id, provider["revision"]),
            )
            self.db.set("provider_balance:" + str(provider_id), {k: str(v) for k, v in balances.items()})
            self.db.set("provider_catalog:" + str(provider_id), catalog)
        self.balance_alerts(provider, balances)
        return balances, catalog

    def balance_alerts(self, provider, balances):
        for unit, balance in balances.items():
            threshold = amount(self.settings(provider)["low"].get(unit, 0))
            Alerts(self.engine).observe(
                f"provider-balance:{provider['id']}:{unit}",
                balance <= threshold,
                f"Мало средств у {provider['title']}: {balance} {unit}, порог {threshold}.",
                f"Баланс {provider['title']} пополнен: {balance} {unit}.",
            )

    async def preview(self, binding, destination=None, *, readonly=False):
        provider = self.provider(binding["provider_id"])
        if not self.enabled(provider):
            raise ServiceError("provider_not_enabled_or_checked")
        if self.db.get("provider_retry:" + str(provider["id"]), 0) > now():
            raise ServiceError("provider_check_cooldown")
        self.rate(provider, binding["currency"])
        if amount(binding["max_native"]) <= 0 or binding["max_cost"] <= 0:
            raise ServiceError("provider_cost_limit_required")
        if destination is None:
            # Catalog checks do not invent a recipient or create purchases.
            balances = {
                k: amount(v) for k, v in self.db.get("provider_balance:" + str(provider["id"]), {}).items()
            }
            catalog = self.db.get("provider_catalog:" + str(provider["id"]), [])
            if now() - (provider["checked_at"] or 0) > 300 or not balances:
                if readonly:
                    async with self.client(provider) as client:
                        balances = await client.balance()
                        catalog = await client.services() if client.kind == "smm" else []
                else:
                    balances, catalog = await self.check(provider["id"])
            if provider["preset"] != "split":
                service = next(
                    (s for s in catalog if s["id"] == binding["service_id"]),
                    None,
                )
                if not service or not service["min"] <= binding["quantity"] <= service["max"]:
                    raise ServiceError("provider_service_or_quantity_unavailable")
                cost = amount(service["rate"]) * binding["quantity"] / 1000
                quote = {
                    "amount": str(cost),
                    "currency": binding["currency"],
                    "balance": str(balances.get(binding["currency"], 0)),
                    "label": service["name"],
                }
                self.validate_quote(binding, provider, quote)
                if not readonly:
                    self.cache_margin(binding, provider, quote)
            elif balances.get(binding["currency"], 0) <= 0:
                raise ServiceError("provider_insufficient_balance")
            return provider
        async with self.client(provider) as client:
            quote = await client.quote(binding, destination)
        self.validate_quote(binding, provider, quote)
        if not readonly:
            self.cache_margin(binding, provider, quote)
        return quote

    def validate_quote(self, binding, provider, quote):
        if quote["currency"] != binding["currency"]:
            raise ServiceError("provider_currency_mismatch")
        cost = amount(quote["amount"])
        if (
            cost > amount(binding["max_native"])
            or self.cost(cost, self.rate(provider, quote["currency"])) > binding["max_cost"]
        ):
            raise ServiceError("provider_purchase_limit_exceeded")
        if amount(quote["balance"]) < cost:
            raise ServiceError("provider_insufficient_balance")

    def cache_margin(self, binding, provider, quote):
        self.db.set(
            "external_margin:" + binding["item_id"],
            {"at": now(), "cost": self.cost(quote["amount"], self.rate(provider, quote["currency"]))},
        )

    def ensure_order(self, order, binding):
        provider = self.provider(binding["provider_id"])
        with self.db.transaction():
            self.db.execute(
                "INSERT OR IGNORE INTO provider_orders(order_id,provider_id,revision,snapshot,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (order["id"], provider["id"], provider["revision"], encoded(binding), now(), now()),
            )
            self.engine.update(order["id"], stage="external_wait")
            kind = (
                "@username получателя Stars"
                if order["module"] == "stars"
                else "ссылку на страницу или публикацию"
            )
            self.db.enqueue(
                "playerok",
                order["chat_id"],
                f"Заказ {order['id']}: отправьте {kind}. При нескольких заказах: !получатель {order['id']} данные.",
                "external-input:" + order["id"],
                order_id=order["id"],
                purpose="external_input",
            )

    async def input(self, order, value):
        binding = self.binding(order["item_id"])
        provider = self.provider(binding["provider_id"])
        destination = destination_for(order["module"], value, binding["domains"])
        before = self.fingerprint(binding, provider)
        quote = await self.preview(binding, destination)
        if before != self.fingerprint(self.binding(order["item_id"]), self.provider(provider["id"])):
            raise ValueError("Настройки изменились. Отправьте получателя ещё раз")
        token = uuid.uuid4().hex[:16]
        quote.update(at=now(), rate=str(self.rate(provider, quote["currency"])), fingerprint=before)
        with self.db.transaction():
            self.db.execute(
                "UPDATE outbox SET state='cancelled' WHERE order_id=? AND purpose='external_confirmation' AND state='pending'",
                (order["id"],),
            )
            self.db.execute(
                "UPDATE provider_orders SET state='draft',destination=?,confirmation=?,confirm_expires=?,"
                "quote=?,snapshot=?,provider_id=?,revision=?,updated_at=? WHERE order_id=? "
                "AND state IN ('waiting_input','draft','ready')",
                (
                    self.db.encrypt(destination),
                    token,
                    now() + 300,
                    encoded(quote),
                    encoded(binding),
                    provider["id"],
                    provider["revision"],
                    now(),
                    order["id"],
                ),
            )
            self.engine.update(order["id"], stage="external_confirm")
            if self.settings(provider)["confirm"]:
                text = (
                    f"Заказ {order['id']}\nПолучатель: {destination}\n{quote['label']}\nКоличество: {binding['quantity']}\n"
                    f"Для запуска отправьте: !подтвердить {token}\nПодтверждение действует 5 минут."
                )
            else:
                self.db.execute("UPDATE provider_orders SET state='ready' WHERE order_id=?", (order["id"],))
                text = f"Заказ {order['id']}: получатель {destination}, количество {binding['quantity']}. Данные приняты."
            self.db.enqueue(
                "playerok",
                order["chat_id"],
                text,
                "external-draft:" + token,
                order_id=order["id"],
                purpose="external_confirmation",
                expires_at=now() + 300,
            )

    async def message(self, message):
        text = message.text.strip()
        command, _, rest = text.partition(" ")
        if command == "!подтвердить":
            row = self.db.one(
                "SELECT p.*,o.buyer_id,o.chat_id FROM provider_orders p JOIN orders o ON o.id=p.order_id "
                "WHERE p.confirmation=?",
                (rest.strip(),),
            )
            if not row or row["buyer_id"] != message.user_id or row["chat_id"] != message.chat_id:
                return "Подтверждение не найдено. Проверьте номер из сообщения по вашему заказу."
            async with self.engine._locks.setdefault(row["order_id"], asyncio.Lock()):
                current = self.db.one("SELECT * FROM provider_orders WHERE order_id=?", (row["order_id"],))
                if current["state"] != "draft" or current["confirm_expires"] <= now():
                    return "Подтверждение устарело или уже использовано. Для изменения данных отправьте получателя заново."
                self.db.execute(
                    "UPDATE provider_orders SET state='ready' WHERE order_id=?", (row["order_id"],)
                )
                self.db.execute(
                    "UPDATE outbox SET state='cancelled' WHERE order_id=? AND purpose='external_confirmation' AND state='pending'",
                    (row["order_id"],),
                )
            return "Данные подтверждены. Заказ будет запущен после повторной проверки оплаты и лимитов."
        if command == "!заказ":
            row = self.db.one(
                "SELECT p.state,p.remote_id,p.remains,o.id FROM provider_orders p JOIN orders o ON o.id=p.order_id "
                "WHERE o.id=? AND o.buyer_id=? AND o.chat_id=?",
                (rest.strip(), message.user_id, message.chat_id),
            )
            if not row:
                return "Заказ не найден. Укажите: !заказ НОМЕР"
            return f"Заказ {row['id']}: {STATE_NAMES.get(row['state'], 'обработка приостановлена')}." + (
                f" Осталось выполнить: {row['remains']}." if row["remains"] is not None else ""
            )
        explicit = command == "!получатель"
        order_id, _, value = rest.partition(" ") if explicit else ("", "", text)
        if not explicit and (text.startswith("!") or not (text.startswith(("@", "https://")))):
            return None
        rows = self.db.all(
            "SELECT o.* FROM provider_orders p JOIN orders o ON o.id=p.order_id WHERE o.buyer_id=? "
            "AND o.chat_id=? AND p.state IN ('waiting_input','draft','ready') AND o.historical=0 "
            "AND o.problem=0 AND o.market_status='PAID'" + (" AND o.id=?" if explicit else ""),
            (message.user_id, message.chat_id, order_id) if explicit else (message.user_id, message.chat_id),
        )
        if not rows:
            return "Нет оплаченного заказа, ожидающего получателя." if explicit else None
        if len(rows) != 1:
            return "У вас несколько заказов. Укажите: !получатель НОМЕР @username_или_ссылка"
        order = rows[0]
        async with self.engine._locks.setdefault(order["id"], asyncio.Lock()):
            current = self.db.one("SELECT state FROM provider_orders WHERE order_id=?", (order["id"],))
            if current["state"] not in {"waiting_input", "draft", "ready"}:
                return "Заказ уже отправляется поставщику. Изменение получателя недоступно."
            try:
                await self.input(order, value)
            except ValueError as error:
                return str(error)
            except (ServiceError, KeyError, TypeError, ArithmeticError):
                return "Не удалось проверить получателя, цену или баланс. Продавцу доступна диагностика подключения."
        return "Данные проверены. Смотрите сообщение по вашему заказу."

    async def process(self, order, binding):
        row = self.db.one("SELECT * FROM provider_orders WHERE order_id=?", (order["id"],))
        if not row:
            self.ensure_order(order, binding)
            return
        if row["state"] != "ready":
            return
        provider = self.provider(row["provider_id"])
        async with self.locks.setdefault(provider["id"], asyncio.Lock()):
            if not self.enabled(provider) or row["confirm_expires"] <= now():
                return self.invalidate(row, "provider_or_confirmation_expired")
            quote = json.loads(row["quote"])
            if quote["fingerprint"] != self.fingerprint(binding, provider):
                return self.invalidate(row, "provider_settings_changed")
            destination = self.db.decrypt(row["destination"])
            try:
                fresh_quote = await self.preview(binding, destination)
            except (ServiceError, ValueError, KeyError, TypeError, ArithmeticError):
                return self.invalidate(row, "provider_preflight_failed")
            if fresh_quote.get("recipient_ref") != quote.get("recipient_ref"):
                return self.invalidate(row, "provider_recipient_changed")
            fresh = await self.engine.market.deal(order["id"])
            self.engine.accept(fresh)
            current = self.provider(provider["id"])
            if (
                fresh.status != "PAID"
                or fresh.problem
                or self.db.get("paused")
                or not self.db.get("live_enabled")
                or binding["paused"]
                or quote["fingerprint"] != self.fingerprint(self.binding(order["item_id"]), current)
                or not self.enabled(current)
                or row["confirm_expires"] <= now()
            ):
                return
            reference = str(uuid.uuid4())
            snapshot = {**binding, "destination": destination, "reference": reference}
            rate = self.rate(current, fresh_quote["currency"])
            cost = self.cost(fresh_quote["amount"], rate)
            with self.db.transaction():
                changed = self.db.execute(
                    "UPDATE provider_orders SET state='creating',snapshot=?,native_amount=?,currency=?,rub_rate=?,"
                    "cost_rub=?,updated_at=? WHERE order_id=? AND state='ready'",
                    (
                        self.db.encrypt(encoded(snapshot)),
                        fresh_quote["amount"],
                        fresh_quote["currency"],
                        str(rate),
                        cost,
                        now(),
                        order["id"],
                    ),
                ).rowcount
                if not changed:
                    return
                self.engine.update(order["id"], stage="external_pending")
            try:
                async with self.client(provider) as client:
                    remote = await client.create(binding, destination, reference)
            except Exception:
                # The process may crash or fail after the provider debited the account.
                self.review(order["id"], "provider_submission_uncertain")
                return
            try:
                self.db.execute(
                    "UPDATE provider_orders SET state='processing',remote_id=?,next_poll=0,updated_at=? WHERE order_id=?",
                    (remote, now(), order["id"]),
                )
            except Exception:
                self.review(order["id"], "provider_duplicate_remote_id")
                return
            if self.settings(provider)["notify"]:
                self.db.notify(
                    f"Заказ {order['id']} передан {provider['title']}. Номер: {remote}.",
                    "provider-start:" + order["id"],
                    order["id"],
                )

    def invalidate(self, row, reason):
        self.db.execute(
            "UPDATE provider_orders SET state='waiting_input',confirmation=NULL,error=? WHERE order_id=?",
            (reason, row["order_id"]),
        )
        self.engine.update(row["order_id"], stage="external_wait")
        order = self.engine.order(row["order_id"])
        self.db.notify(
            f"⏸ Закупка не запущена\n\n🧾 Заказ: {order['id']}\n{issue_text(reason)}",
            "provider-preflight:" + order["id"] + ":" + reason,
            order["id"],
        )
        self.db.enqueue(
            "playerok",
            order["chat_id"],
            "Заказ пока не передан поставщику: подтверждение устарело или изменились условия закупки. Продавец уведомлён. Для новой проверки отправьте получателя ещё раз.",
            "external-expired:" + str(uuid.uuid4()),
            order_id=order["id"],
        )

    def review(self, order_id, reason, state="review"):
        self.db.execute(
            "UPDATE provider_orders SET state=?,error=?,updated_at=? WHERE order_id=?",
            (state, reason, now(), order_id),
        )
        self.engine.fail(order_id, reason, review=True)

    async def poll(self, row):
        order = self.engine.order(row["order_id"])
        provider = self.provider(row["provider_id"])
        snapshot = json.loads(self.db.decrypt(row["snapshot"]))
        if provider["revision"] != row["revision"]:
            return self.review(order["id"], "provider_credentials_changed")
        self.db.execute("UPDATE provider_orders SET next_poll=? WHERE order_id=?", (now() + 60, order["id"]))
        async with self.client(provider) as client:
            result = await client.status(row["remote_id"], snapshot)
        if result["currency"] != row["currency"]:
            return self.review(order["id"], "provider_currency_changed")
        cost = self.cost(result["amount"], amount(row["rub_rate"]))
        self.db.execute(
            "UPDATE provider_orders SET native_amount=?,cost_rub=?,cost_known=?,remains=?,updated_at=? WHERE order_id=?",
            (result["amount"], cost, int(result["cost_known"]), result["remains"], now(), order["id"]),
        )
        if result["cost_known"]:
            self.engine.update(order["id"], cost=cost)
        if amount(result["amount"]) > amount(snapshot["max_native"]) or cost > snapshot["max_cost"]:
            self.db.execute("UPDATE bindings SET paused=1 WHERE item_id=?", (order["item_id"],))
            self.db.notify(
                f"Закупка {order['id']} превысила лимит. Лот приостановлен.",
                "provider-overlimit:" + order["id"],
                order["id"],
            )
        state = result["state"]
        if state == "completed":
            self.db.execute(
                "UPDATE provider_orders SET state='completed',error=NULL WHERE order_id=?", (order["id"],)
            )
            await self.complete(order["id"], snapshot)
        elif state == "cancelled" and result.get("zero_delivery"):
            self.db.execute("UPDATE provider_orders SET state='rejected' WHERE order_id=?", (order["id"],))
            self.engine.fail(order["id"], "provider_cancelled_without_delivery")
            if self.settings(provider)["auto_refund"]:
                await self.refund(order["id"])
        elif state in {"partial", "cancelled", "review"}:
            self.review(order["id"], "provider_" + state, "partial" if state == "partial" else "review")

    async def complete(self, order_id, snapshot):
        self.engine.accept(await self.engine.market.deal(order_id))
        order = self.engine.order(order_id)
        if order["market_status"] == "ROLLED_BACK" or order["problem"]:
            self.engine.update(order_id, delivered_at=order["delivered_at"] or now())
            self.db.notify(
                f"Поставщик завершил {order_id}, но на Playerok возврат или проблема. Проверьте сделку.",
                "provider-market-conflict:" + order_id,
                order_id,
            )
            return
        text = f"Заказ {order_id} выполнен: {snapshot['quantity']} для {snapshot['destination']}."
        if snapshot["instruction"]:
            text += "\n" + snapshot["instruction"]
        self.db.enqueue(
            "playerok",
            order["chat_id"],
            text,
            "external-done:" + order_id,
            order_id=order_id,
            purpose="external_done",
        )
        await self.engine.finish_delivery(order_id)

    async def refund(self, order_id):
        if self.db.get("paused") or not self.db.get("live_enabled"):
            return
        row = self.db.one("SELECT * FROM provider_orders WHERE order_id=?", (order_id,))
        if not row or row["state"] != "rejected" or row["cost_rub"] != 0:
            raise ValueError(
                "Автовозврат разрешён только при подтверждённом нулевом выполнении и нулевых затратах"
            )
        fresh = await self.engine.market.deal(order_id)
        self.engine.accept(fresh)
        if fresh.status == "ROLLED_BACK" or fresh.problem or fresh.status != "PAID":
            return
        key = "refund_pending:" + order_id
        if self.db.get(key):
            return
        self.db.set(key, True)
        try:
            await self.engine.market.refund(order_id)
            self.engine.accept(await self.engine.market.deal(order_id))
        except ServiceError:
            self.db.notify(
                "↩️ Playerok не вернул результат возврата. Откройте заказ: " + order_id,
                "provider-refund-review:" + order_id,
                order_id,
            )

    async def tick(self):
        if self.db.get("paused") or not self.db.get("live_enabled"):
            return
        for row in self.db.all(
            "SELECT p.* FROM provider_orders p JOIN orders o ON o.id=p.order_id "
            "WHERE p.state='completed' AND o.stage!='delivered' AND o.market_status!='ROLLED_BACK' AND o.problem=0 LIMIT 20"
        ):
            async with self.engine._locks.setdefault(row["order_id"], asyncio.Lock()):
                try:
                    await self.complete(row["order_id"], json.loads(self.db.decrypt(row["snapshot"])))
                except ServiceError:
                    pass
        for row in self.db.all(
            "SELECT * FROM provider_orders WHERE state='processing' AND next_poll<=? ORDER BY next_poll LIMIT 40",
            (now(),),
        ):
            async with self.engine._locks.setdefault(row["order_id"], asyncio.Lock()):
                current = self.db.one("SELECT * FROM provider_orders WHERE order_id=?", (row["order_id"],))
                if current["state"] != "processing":
                    continue
                try:
                    await self.poll(current)
                except (ServiceError, KeyError, ValueError, TypeError, ArithmeticError):
                    self.db.notify(
                        "Не удалось сверить внешний заказ " + row["order_id"],
                        "provider-poll:" + row["order_id"] + ":" + str(int(now() // 3600)),
                        row["order_id"],
                    )
        for row in self.db.all("SELECT order_id FROM provider_orders WHERE state='ready' LIMIT 40"):
            await self.engine.process(row["order_id"])
        for provider in self.db.all(
            "SELECT * FROM providers WHERE enabled=1 AND coalesce(checked_at,0)<?", (now() - 300,)
        ):
            try:
                await self.check(provider["id"])
            except (ServiceError, KeyError, ValueError, TypeError, ArithmeticError):
                self.db.notify(
                    "🔌 Поставщик временно недоступен. Откройте подключение: " + provider["title"],
                    f"provider-offline:{provider['id']}:{int(now() // 3600)}",
                )

    def recover(self):
        self.db.execute("UPDATE provider_actions SET state='uncertain' WHERE state='sending'")
        for row in self.db.all("SELECT order_id FROM provider_orders WHERE state='creating'"):
            self.review(row["order_id"], "provider_submission_interrupted")

    def attach(self, order_id, remote, expected=None):
        row = self.db.one("SELECT * FROM provider_orders WHERE order_id=?", (order_id,))
        if (
            not row
            or row["state"] != "review"
            or row["remote_id"]
            or not row["destination"]
            or (expected is not None and row["updated_at"] != expected)
        ):
            raise ValueError("Привязка недоступна: сначала проверьте состояние заказа")
        # Only an uncertain persisted purchase intent can be reconciled this way.
        json.loads(self.db.decrypt(row["snapshot"]))
        provider = self.provider(row["provider_id"])
        if provider["preset"] == "split":
            remote = str(uuid.UUID(remote))
        elif not remote.isdigit() or int(remote) <= 0:
            raise ValueError("Нужен числовой номер заказа панели")
        with self.db.transaction():
            self.db.execute(
                "UPDATE provider_orders SET remote_id=?,state='processing',next_poll=0,error=NULL WHERE order_id=?",
                (remote, order_id),
            )
            self.engine.update(order_id, stage="external_pending")
            self.db.audit("provider_order_manually_linked", order_id)

    def settle(self, order_id, net, refunded, expected):
        row = self.db.one("SELECT * FROM provider_orders WHERE order_id=?", (order_id,))
        if (
            not row
            or row["state"] not in {"partial", "review"}
            or row["updated_at"] != expected
            or not row["rub_rate"]
        ):
            raise ValueError("Состояние изменилось. Откройте сверку заново")
        net, refunded = amount(net), amount(refunded)
        cost = self.cost(net, amount(row["rub_rate"]))
        with self.db.transaction():
            self.db.execute(
                "UPDATE provider_orders SET state='closed',native_amount=?,supplier_refund=?,cost_rub=?,cost_known=1,updated_at=? WHERE order_id=?",
                (str(net), str(refunded), cost, now(), order_id),
            )
            self.engine.update(order_id, cost=cost, stage="failed", error="provider_manually_settled")
            self.db.audit("provider_cost_manually_settled", order_id)

    async def action(self, order_id, kind):
        row = self.db.one("SELECT * FROM provider_orders WHERE order_id=?", (order_id,))
        if not row or not row["remote_id"] or kind not in {"cancel", "refill"}:
            raise ValueError("Действие недоступно")
        provider = self.provider(row["provider_id"])
        if (
            provider["revision"] != row["revision"]
            or provider["preset"] == "split"
            or (kind == "refill" and row["state"] != "completed")
            or (kind == "cancel" and row["state"] != "processing")
        ):
            raise ValueError("Действие недоступно в текущем состоянии")
        async with self.client(provider) as client:
            snapshot = json.loads(self.db.decrypt(row["snapshot"]))
            service = next((s for s in await client.services() if s["id"] == snapshot["service_id"]), None)
            if not service or not service.get(kind):
                raise ValueError("Поставщик не поддерживает это действие для услуги")
            changed = self.db.execute(
                "INSERT OR IGNORE INTO provider_actions VALUES (?,?,?,NULL,?)",
                (order_id, kind, "sending", now()),
            ).rowcount
            if not changed:
                raise ValueError("Запрос уже отправлялся. Проверьте результат в панели поставщика")
            try:
                result = await client.cancel_or_refill(row["remote_id"], kind)
            except Exception:
                self.db.execute(
                    "UPDATE provider_actions SET state='uncertain' WHERE order_id=? AND kind=?",
                    (order_id, kind),
                )
                raise ServiceError("provider_action_uncertain") from None
            # An accepted cancel/refill request is not proof of refund or completion.
            reference = str(result.get("refill", "")) if isinstance(result, dict) else ""
            reference = reference if reference.isdigit() else ""
            self.db.execute(
                "UPDATE provider_actions SET state='requested',remote_id=? WHERE order_id=? AND kind=?",
                (reference, order_id, kind),
            )
            self.db.audit("provider_" + kind + "_requested", order_id)


STATE_NAMES = {
    "waiting_input": "ожидает получателя",
    "draft": "ожидает подтверждения",
    "ready": "готовится к запуску",
    "creating": "отправляется поставщику",
    "processing": "выполняется",
    "completed": "выполнен",
    "partial": "выполнен частично",
    "rejected": "отменён без выполнения",
    "review": "обработка приостановлена",
    "closed": "сверка завершена продавцом",
}
