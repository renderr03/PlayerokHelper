"""Local shop configuration, customer notes, pricing and read-only dashboards."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal

from .models import now
from .providers import PRESETS

MODES = {"kosell", "extend", "stock", "own_rent", "own_extend", "stars", "smm"}
TAGS = {"regular": "Постоянный", "help": "Нужна помощь", "manual": "Ручной ответ"}


def mode(row):
    return row.get("fulfillment") or row["module"]


def fingerprint(db, item):
    row = db.one("SELECT * FROM bindings WHERE item_id=?", (item,))
    if row:
        row = {
            k: v
            for k, v in row.items()
            if k not in {"healthy_count", "hide_reason", "hidden_by_bot", "last_bump", "last_listing_action"}
        }
    external = db.one("SELECT * FROM external_bindings WHERE item_id=?", (item,))
    return hashlib.sha256(
        json.dumps([row, external, db.get("override:" + item, {})], sort_keys=True).encode()
    ).hexdigest()


def validate_template(value):
    if not isinstance(value, dict) or set(value) - {
        "format",
        "name",
        "mode",
        "hours",
        "max_cost",
        "instruction",
        "external",
    }:
        raise ValueError("В шаблоне есть неподдерживаемые поля")
    if value.get("format") != 1 or value.get("mode") not in MODES:
        raise ValueError("Неизвестный формат шаблона")
    if not isinstance(value.get("name"), str) or not 1 <= len(value["name"]) <= 80:
        raise ValueError("Название: 1–80 символов")
    for field, low, high in [("hours", 1, 8760), ("max_cost", 0, 10**12)]:
        if type(value.get(field)) is not int or not low <= value[field] <= high:
            raise ValueError("Недопустимые сроки или лимиты")
    if value["mode"] in {"kosell", "extend"} and value["max_cost"] <= 0:
        raise ValueError("Нужен положительный лимит закупки")
    if not isinstance(value.get("instruction"), str) or len(value["instruction"]) > 1000:
        raise ValueError("Инструкция до 1000 символов")
    ext = value.get("external")
    if value["mode"] in {"stars", "smm"}:
        if not isinstance(ext, dict) or set(ext) != {"preset", "quantity", "max_native", "currency"}:
            raise ValueError("Неверные параметры внешней услуги")
        if type(ext["quantity"]) is not int or not 1 <= ext["quantity"] <= 1000000:
            raise ValueError("Количество вне диапазона")
        limit = Decimal(str(ext["max_native"]))
        if not limit.is_finite() or not 0 < limit <= Decimal("1e12"):
            raise ValueError("Неверный лимит внешней услуги")
        if not isinstance(ext["currency"], str) or not ext["currency"].isalpha() or len(ext["currency"]) > 8:
            raise ValueError("Неверная валюта")
        if ext["preset"] not in PRESETS:
            raise ValueError("Неизвестный поставщик")
    elif ext is not None:
        raise ValueError("Лишние параметры внешней услуги")
    return value


def save_template(db, item, name):
    row = db.one("SELECT * FROM bindings WHERE item_id=?", (item,))
    if not row:
        raise ValueError("Лот не найден")
    value = {
        "format": 1,
        "name": name,
        "mode": mode(row),
        "hours": row["hours"],
        "max_cost": row["max_cost"],
        "instruction": row["instruction"],
    }
    if value["mode"] in {"stars", "smm"}:
        ext = db.one(
            "SELECT e.*,p.preset FROM external_bindings e JOIN providers p ON p.id=e.provider_id WHERE item_id=?",
            (item,),
        )
        value["external"] = {k: ext[k] for k in ("preset", "quantity", "max_native", "currency")}
    return import_template(db, value)


def import_template(db, value):
    value = validate_template(value)
    if db.one("SELECT count(*) n FROM shop_templates")["n"] >= 100:
        raise ValueError("Лимит: 100 шаблонов")
    tid = uuid.uuid4().hex[:12]
    db.execute(
        "INSERT INTO shop_templates VALUES (?,?,?,?)",
        (tid, value["name"], db.encrypt(json.dumps(value)), now()),
    )
    return tid


def get_template(db, tid):
    row = db.one("SELECT payload FROM shop_templates WHERE id=?", (tid,))
    if not row:
        raise ValueError("Шаблон не найден")
    return validate_template(json.loads(db.decrypt(row["payload"])))


def compatible(db, template, item):
    row = db.one("SELECT * FROM bindings WHERE item_id=?", (item,))
    if not row or mode(row) != template["mode"]:
        return False
    if template["mode"] in {"stars", "smm"}:
        ext = db.one(
            "SELECT e.*,p.preset FROM external_bindings e JOIN providers p ON p.id=e.provider_id WHERE item_id=?",
            (item,),
        )
        return bool(
            ext
            and ext["preset"] == template["external"]["preset"]
            and ext["currency"] == template["external"]["currency"]
        )
    return True


def preview_template(db, tid, items):
    value = get_template(db, tid)
    ids = list(dict.fromkeys(items))
    if not ids or len(ids) > 200:
        raise ValueError("Выберите от 1 до 200 лотов")
    return {
        "template": tid,
        "value": value,
        "expires": now() + 300,
        "nonce": uuid.uuid4().hex[:12],
        "items": {i: fingerprint(db, i) for i in ids},
    }


def apply_template(db, preview):
    if preview["expires"] < now() or get_template(db, preview["template"]) != preview["value"]:
        raise ValueError("Предпросмотр устарел")
    value = preview["value"]
    result = []
    with db.transaction():
        for item, expected in preview["items"].items():
            if fingerprint(db, item) != expected or not compatible(db, value, item):
                result.append((item, "Пропущен: изменён или несовместим"))
                continue
            if value["mode"] in {"stars", "smm"}:
                ext = db.one("SELECT * FROM external_bindings WHERE item_id=?", (item,))
                # Respect the checked service range, never silently switch service/provider.
                catalog = db.get("provider_catalog:" + str(ext["provider_id"]), [])
                service = next((r for r in catalog if str(r["id"]) == ext["service_id"]), None)
                if value["mode"] == "smm" and (
                    not service or not service["min"] <= value["external"]["quantity"] <= service["max"]
                ):
                    result.append((item, "Пропущен: обновите каталог/проверьте количество"))
                    continue
                db.execute(
                    "UPDATE external_bindings SET quantity=?,max_native=? WHERE item_id=?",
                    (value["external"]["quantity"], value["external"]["max_native"], item),
                )
            db.execute(
                "UPDATE bindings SET hours=?,max_cost=?,instruction=? WHERE item_id=?",
                (value["hours"], value["max_cost"], value["instruction"], item),
            )
            db.audit("template_applied", item)
            result.append((item, "Применён"))
    return result


def customer(db, buyer):
    rows = db.all("SELECT * FROM orders WHERE buyer_id=? ORDER BY created_at DESC", (buyer,))
    confirmed = [r for r in rows if r["market_status"] in {"CONFIRMED", "CONFIRMED_AUTOMATICALLY"}]
    note = db.one("SELECT * FROM customer_notes WHERE buyer_id=?", (buyer,))
    return {
        "rows": rows,
        "name": rows[0]["buyer_name"] if rows else buyer,
        "spent": sum(r["price"] for r in confirmed),
        "completed": len(confirmed),
        "note": db.decrypt(note["note"]) if note and note["note"] else "",
        "tag": note["tag"] if note else "",
    }


def set_customer(db, buyer, *, note=None, tag=None):
    if not db.one("SELECT id FROM orders WHERE buyer_id=? LIMIT 1", (buyer,)):
        raise ValueError("Покупатель не найден в заказах магазина")
    old = db.one("SELECT * FROM customer_notes WHERE buyer_id=?", (buyer,)) or {"tag": "", "note": ""}
    if tag is not None and tag not in {"", *TAGS}:
        raise ValueError("Неизвестная метка")
    if note is not None and len(note) > 2000:
        raise ValueError("Заметка до 2000 символов")
    db.execute(
        "INSERT OR REPLACE INTO customer_notes VALUES (?,?,?)",
        (
            buyer,
            tag if tag is not None else old["tag"],
            db.encrypt(note) if note is not None else old["note"],
        ),
    )


def price_suggestion(cost, fee, margin, lower, upper):
    values = [Decimal(str(v)) for v in (cost, fee, margin, lower, upper)]
    if not all(v.is_finite() for v in values):
        raise ValueError("Неверные числа")
    c, f, m, lo, hi = values
    if c < 0 or f < 0 or m < 0 or f + m >= 100 or not 0 < lo <= hi:
        raise ValueError("Комиссия + маржа должны быть меньше 100%; проверьте границы")
    raw = (c / (1 - (f + m) / 100)).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    if raw > hi:
        raise ValueError("Заданная маржа недостижима в верхней границе цены")
    return max(raw, lo)


def calendar(db, start, end):
    return db.all(
        "SELECT o.id,o.title,o.buyer_name,o.module,o.expires_at,o.rental_number,l.state lease_state "
        "FROM orders o LEFT JOIN own_leases l ON l.order_id=o.id "
        "WHERE o.module IN ('kosell','own_rent') AND o.stage='delivered' AND o.problem=0 "
        "AND o.market_status!='ROLLED_BACK' AND o.expires_at>=? AND o.expires_at<? ORDER BY o.expires_at,o.id",
        (start, end),
    )


def dashboard(engine, days):
    end = now()
    start = end - days * 86400
    rows = engine.db.all(
        "SELECT * FROM orders WHERE created_at>=? AND created_at<? ORDER BY created_at", (start, end)
    )
    current = engine.db.all(
        "SELECT buyer_id, min(created_at) first_at FROM orders WHERE market_status IN ('CONFIRMED','CONFIRMED_AUTOMATICALLY') GROUP BY buyer_id"
    )
    first = {r["buyer_id"]: r["first_at"] for r in current}
    confirmed = [r for r in rows if r["market_status"] in {"CONFIRMED", "CONFIRMED_AUTOMATICALLY"}]
    buyers = {r["buyer_id"] for r in confirmed}
    repeat = {b for b in buyers if first[b] < start or sum(r["buyer_id"] == b for r in confirmed) > 1}
    totals = engine.db.all(
        "SELECT title,sum(price) total FROM orders WHERE created_at>=? AND created_at<? AND market_status IN ('CONFIRMED','CONFIRMED_AUTOMATICALLY') GROUP BY item_id ORDER BY total DESC LIMIT 5",
        (start, end),
    )
    first_day = datetime.fromtimestamp(start, engine.tz).replace(hour=0, minute=0, second=0, microsecond=0)
    last_day = datetime.fromtimestamp(end, engine.tz)
    bins = {}
    while first_day.date() <= last_day.date():
        bins[first_day.strftime("%d.%m")] = 0
        first_day += timedelta(days=1)
    hours = [0] * 24
    for r in confirmed:
        local = datetime.fromtimestamp(r["created_at"], engine.tz)
        day = local.strftime("%d.%m")
        bins[day] = bins.get(day, 0) + r["price"]
        hours[local.hour] += 1
    from .analytics import Analytics

    analytics = Analytics(engine.db)
    return {
        "summary": analytics.summary(rows, start, end),
        "previous": analytics.summary(start=start - days * 86400, end=start),
        "bins": bins,
        "hours": hours,
        "repeat": len(repeat),
        "buyers": len(buyers),
        "top": totals,
    }


def chart_png(data):
    from PIL import Image, ImageDraw, ImageFont

    picture = Image.new("RGB", (1100, 640), "#10101c")
    draw = ImageDraw.Draw(picture)
    font = ImageFont.load_default(size=22)
    draw.text((40, 28), "PlayerokHelper / Revenue RUB", fill="#e9dcff", font=font)
    pairs = list(data["bins"].items())[-31:]
    maximum = max([v for _, v in pairs] or [1]) or 1
    width = 900 / max(len(pairs), 1)
    small = ImageFont.load_default(size=15)
    for fraction in (0, 0.5, 1):
        y = 540 - 440 * fraction
        draw.line((100, y, 1040, y), fill="#333047", width=1)
        draw.text((10, y - 10), f"{maximum * fraction / 100:,.0f}", fill="#b8aacd", font=small)
    for i, (label, value) in enumerate(pairs):
        x = 110 + i * width
        height = 440 * value / maximum
        draw.rectangle((x, 540 - height, x + max(2, width - 8), 540), fill="#ac5dff")
        if len(pairs) <= 12 and value:
            draw.text((x, 530 - height), f"{value / 100:.2f}", fill="#eadcff", font=small, anchor="lb")
        if len(pairs) <= 12 or i % 3 == 0:
            draw.text((x, 555), label, fill="#ddd3ef", font=ImageFont.load_default(size=15))
    draw.text((40, 600), "Confirmed sales / RUB", fill="#c2b3d7", font=font)
    import io

    output = io.BytesIO()
    picture.save(output, format="PNG")
    return output.getvalue()
