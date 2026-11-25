"""Read-only service checks and delivery previews. Never call delivery mutations here."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .models import ServiceError, money, now, rub, timestamp


def needs_kosell(db):
    return db.get("setup_source") == "kosell" or bool(
        db.one("SELECT item_id FROM bindings WHERE module IN ('kosell','extend') LIMIT 1")
    )


def binding_fingerprint(binding):
    return hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()


async def connection_snapshot(db, market, kosell, *, check_kosell=True):
    result = {
        "issues": ["visibility_restore_pending"] if db.get("visibility_test_pending") else [],
        "services": {},
    }
    for name, client, method in [("playerok", market, "profile"), ("kosell", kosell, "balance")]:
        if name == "kosell" and not check_kosell:
            continue
        configured = getattr(client, "configured", True)
        status = {"configured": configured, "ok": False}
        result["services"][name] = status
        if not configured:
            result["issues"].append(name + "_not_configured")
            continue
        try:
            value = await getattr(client, method)()
            if name == "playerok":
                if not value["id"]:
                    raise ValueError()
                result["profile"] = {k: value[k] for k in ("id", "username", "balance")}
            else:
                if rub(value["balance_rub"]) < 0:
                    raise ValueError()
                result["kosell_balance"] = {
                    "balance_rub": value["balance_rub"],
                    "balance_usd": value.get("balance_usd"),
                }
            status["ok"] = True
        except ServiceError as exc:
            result["issues"].append(exc.code)
        except (KeyError, ValueError, TypeError, ArithmeticError):
            result["issues"].append(name + "_invalid_response")
    if not market.visibility:
        result["issues"].append("visibility_contract_missing")
    else:
        proof = db.get("visibility_proof") or {}
        if proof.get("account_id") != market.user_id or proof.get("fingerprint") != market.fingerprint():
            result["issues"].append("visibility_roundtrip_not_verified")
    return result


@dataclass
class Preview:
    checked_at: float = field(default_factory=now)
    issues: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    sample: str = ""
    binding: str = ""

    def text(self, expiry):
        return "\n".join(
            [
                "🧪 Проверка без покупки",
                "Проверено: " + expiry(self.checked_at),
                "Результат: " + ("есть ограничения / нет данных" if self.issues else "проверки пройдены"),
                *self.observations,
                *["⚠️ " + issue for issue in self.issues],
                "\nПример сообщения (заменители реквизитов):",
                self.sample,
                "\nПроверка не резервирует товар, не закупает и не отправляет его. "
                "Перед реальной выдачей проверки выполняются заново.",
            ]
        )


async def preview_delivery(engine, item_id, *, rental_id=None):
    db, market, kosell = engine.db, engine.market, engine.kosell
    result = Preview()
    binding = db.one("SELECT * FROM bindings WHERE item_id=?", (item_id,))
    if not binding:
        result.issues.append("Лот не привязан.")
        return result
    result.binding = binding_fingerprint(binding)
    result.observations.append("Лот: " + binding["title"][:150])
    if db.get("paused") or binding["paused"]:
        result.issues.append("Включена ручная пауза.")
    if not 1 <= binding["hours"] <= 8760:
        result.issues.append("Некорректная длительность.")
    try:
        snapshot = await connection_snapshot(db, market, kosell, check_kosell=binding["module"] != "stock")
        result.issues.extend(snapshot["issues"])
        if snapshot["services"]["playerok"]["ok"]:
            item = await market.item(item_id)
            if (item.get("user") or {}).get("id") != market.user_id:
                result.issues.append("Лот не принадлежит подключённому магазину.")
            if item["status"] != "APPROVED":
                result.issues.append("Лот не находится в активном статусе APPROVED.")
        if binding.get("fulfillment") in {"stars", "smm"}:
            external = engine.external.binding(item_id)
            result.binding = engine.external.fingerprint(
                external, engine.external.provider(external["provider_id"])
            )
            await engine.external.preview(external, readonly=True)
            result.observations.append(
                f"Внешняя услуга: {binding['fulfillment']}, количество {external['quantity']}."
            )
            result.observations.append("Получатель, точная цена и баланс проверяются перед закупкой.")
            payload = "[подтверждённый поставщиком результат выполнения внешнего заказа]"
        elif binding.get("fulfillment") == "own_rent":
            account = db.one(
                "SELECT cost FROM own_accounts WHERE item_id=? AND state='free' ORDER BY id LIMIT 1",
                (item_id,),
            )
            if not account:
                result.issues.append("Нет свободных аккаунтов собственного пула.")
            result.observations.append("Себестоимость выдачи: " + money(account["cost"] if account else None))
            payload = "[реквизиты собственного аккаунта — скрыты]\nАренда: " + str(binding["hours"]) + " ч."
        elif binding.get("fulfillment") == "own_extend":
            target = db.one(
                "SELECT o.id,l.expires_at,l.state FROM orders o JOIN own_leases l ON l.order_id=o.id "
                "WHERE o.id=? AND o.item_id=? AND o.module='own_rent' AND o.stage='delivered' "
                "AND o.market_status!='ROLLED_BACK' AND o.problem=0",
                (rental_id, binding["pool_item"]),
            )
            if not target or target["state"] != "active" or target["expires_at"] <= now():
                result.issues.append("Выберите действующую аренду собственного пула.")
            payload = "Аренда продлена до [новый срок]."
            result.observations.append("Продление собственного пула без закупки у поставщика.")
        elif binding["module"] == "stock":
            count = db.one("SELECT count(*) n FROM stock WHERE item_id=? AND state='free'", (item_id,))["n"]
            result.observations.append(f"Свободных позиций: {count}")
            if not count:
                result.issues.append("Склад пуст.")
            payload = "[позиция со склада — скрыта]"
        else:
            payload = "Логин: [логин]\nПароль: [пароль]\nДо: [срок]\nНомер аренды: [номер]"
            if not binding["product_id"] or binding["max_cost"] <= 0:
                result.issues.append("Не настроены товар KOSell или лимит закупки.")
            if snapshot["services"]["kosell"]["ok"]:
                products = {p["id"]: p for p in await kosell.products()}
                product = products.get(binding["product_id"])
                quote = None
                if not product:
                    result.issues.append("Товар KOSell не найден.")
                elif binding["module"] == "kosell":
                    if product["available_accounts"] <= 0:
                        result.issues.append("У поставщика нет свободных аккаунтов.")
                    if not product["min_hours"] <= binding["hours"] <= product["max_hours"]:
                        result.issues.append("Длительность вне диапазона KOSell.")
                    else:
                        quote = await kosell.quote(binding["product_id"], binding["hours"])
                else:
                    target = db.one(
                        "SELECT o.*,b.product_id FROM orders o JOIN bindings b ON b.item_id=o.item_id "
                        "WHERE o.id=? AND o.module='kosell' AND o.stage='delivered' "
                        "AND o.market_status!='ROLLED_BACK' AND o.problem=0",
                        (rental_id,),
                    )
                    if not target or not target["expires_at"] or target["expires_at"] <= now():
                        result.issues.append("Выберите действующую аренду этого магазина.")
                    elif target["product_id"] != binding["product_id"]:
                        result.issues.append("Игра аренды не соответствует лоту продления.")
                    else:
                        active = next(
                            (r for r in await kosell.active() if r["rental_uid"] == target["rental_uid"]),
                            None,
                        )
                        if not active or timestamp(active["expires_at"]) <= now():
                            result.issues.append("Активная аренда не подтверждена KOSell.")
                        else:
                            quote = await kosell.extension_quote(target["rental_uid"], binding["hours"])
                    payload = "Аренда продлена до [новый срок]."
                if quote is not None:
                    cost = rub(quote["total_rub"])
                    if cost < 0:
                        raise ValueError()
                    balance = rub(snapshot["kosell_balance"]["balance_rub"])
                    result.observations += [
                        "Расчёт KOSell: " + money(cost),
                        "Баланс: " + money(balance),
                        "Лимит закупки: " + money(binding["max_cost"]),
                    ]
                    if cost > binding["max_cost"]:
                        result.issues.append("Стоимость выше лимита закупки.")
                    if balance < cost:
                        result.issues.append("Недостаточно средств KOSell.")
        result.sample = "Заказ [номер заказа]\n" + payload
        if binding["instruction"]:
            result.sample += "\n\n" + binding["instruction"]
    except ServiceError as exc:
        result.issues.append("Нет актуальных данных: " + exc.code)
    except (KeyError, ValueError, TypeError, ArithmeticError):
        result.issues.append("Нет актуальных данных: изменился формат ответа сервиса.")
    return result
