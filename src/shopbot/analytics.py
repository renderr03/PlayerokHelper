from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from .db import Database
from .models import money, now


def safe_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


class Analytics:
    def __init__(self, db: Database):
        self.db = db

    def orders(self, start=0, end=None, item_id=None, module=None):
        sql = "SELECT * FROM orders WHERE created_at>=? AND created_at<?"
        args = [start, end if end is not None else now() + 1]
        if item_id:
            sql += " AND item_id=?"
            args.append(item_id)
        if module:
            sql += " AND module=?"
            args.append(module)
        return self.db.all(sql + " ORDER BY created_at", args)

    def summary(self, rows=None, start=0, end=None, *, item_id=None, module=None):
        rows = self.orders(start, end, item_id, module) if rows is None else rows
        confirmed = [r for r in rows if r["market_status"] in {"CONFIRMED", "CONFIRMED_AUTOMATICALLY"}]
        external = {p["order_id"]: p for p in self.db.all("SELECT * FROM provider_orders")}
        gross = sum(r["price"] for r in confirmed)
        refunded = sum(r["price"] for r in rows if r["market_status"] == "ROLLED_BACK")
        incurred = [
            r
            for r in rows
            if r["rental_uid"]
            or r["delivered_at"]
            or (r["module"] in {"stock", "own_rent"} and r["stage"] == "sending")
            or external.get(r["id"], {}).get("cost_known")
        ]
        costs = sum(r["cost"] or 0 for r in incurred)
        unknown_ids = {r["id"] for r in confirmed + incurred if r["cost"] is None}
        unknown_ids.update(
            r["id"] for r in rows if r["stage"] == "review" and r["module"] == "extend" and r["cost"] is None
        )
        unknown = len(unknown_ids)
        fee_percent = self.db.get("fee_percent")
        commission = (
            int(Decimal(gross) * Decimal(str(fee_percent)) / 100) if fee_percent is not None else None
        )
        expenses = self.db.all(
            "SELECT * FROM expenses WHERE created_at>=? AND created_at<? AND state IN ('paid','reserved')",
            (start, end or now() + 1),
        )
        if item_id:
            expenses = [e for e in expenses if e["item_id"] == item_id]
        if module:
            bindings = {
                b["item_id"]
                for b in self.db.all(
                    "SELECT item_id FROM bindings WHERE coalesce(nullif(fulfillment,''),module)=?", (module,)
                )
            }
            expenses = [e for e in expenses if e["item_id"] in bindings]
        promotion = sum(e["amount"] for e in expenses if e["state"] == "paid" and e["kind"] != "bonus")
        bonus_cost = sum(e["amount"] for e in expenses if e["state"] == "paid" and e["kind"] == "bonus")
        order_ids = {r["id"] for r in rows}
        gifts = [g for g in self.db.all("SELECT * FROM bonus_claims") if g["order_id"] in order_ids]
        bonus_unknown = sum(g["state"] == "sent" and g["cost"] is None for g in gifts)
        bonus_uncertain = sum(
            bool(
                self.db.one(
                    "SELECT id FROM outbox WHERE purpose=? AND state IN ('sending','uncertain')",
                    ("bonus:" + str(g["id"]),),
                )
            )
            for g in gifts
            if g["state"] in {"queued", "review"}
        )
        reserved = sum(e["amount"] for e in expenses if e["state"] == "reserved")
        selected_external = [external[r["id"]] for r in rows if r["id"] in external]
        external_pending = sum(
            p["state"] in {"creating", "processing", "review", "partial"} for p in selected_external
        )
        native = {}
        for purchase in selected_external:
            if purchase["cost_known"] and purchase["currency"]:
                unit = purchase["currency"]
                native[unit] = native.get(unit, Decimal(0)) + Decimal(purchase["native_amount"])
        estimated_currency = any(
            self.db.one("SELECT order_id FROM kosell_charges WHERE order_id=? AND estimated=1", (r["id"],))
            for r in rows
        )
        kosell_native = {}
        for r in rows:
            debit = self.db.one("SELECT currency,amount FROM kosell_charges WHERE order_id=?", (r["id"],))
            if debit:
                kosell_native[debit["currency"]] = kosell_native.get(debit["currency"], Decimal(0)) + Decimal(
                    debit["amount"]
                )
        complete = (
            not estimated_currency
            and unknown == 0
            and commission is not None
            and reserved == 0
            and bonus_unknown == 0
            and bonus_uncertain == 0
            and external_pending == 0
        )
        return {
            "orders": len(rows),
            "confirmed": len(confirmed),
            "gross": gross,
            "refunds": refunded,
            "cost": costs,
            "commission": commission,
            "promotion": promotion,
            "bonus_cost": bonus_cost,
            "bonus_unknown": bonus_unknown,
            "bonus_uncertain": bonus_uncertain,
            "reserved": reserved,
            "external_pending": external_pending,
            "kosell_native": ", ".join(f"{v} {k}" for k, v in sorted(kosell_native.items())) or "—",
            "kosell_estimated": estimated_currency,
            "external_native": ", ".join(f"{v} {k}" for k, v in sorted(native.items())) or "—",
            "unknown_costs": unknown,
            "profit": gross - costs - promotion - bonus_cost - (commission or 0),
            "complete": complete,
        }

    def text(self, **filters):
        s = self.summary(**filters)
        return (
            "📈 Аналитика магазина\n\n"
            f"🛍 Заказов: {s['orders']}, подтверждено: {s['confirmed']}\n"
            f"💰 Выручка подтверждённых заказов: {money(s['gross'])}\n↩️ Возвраты: {money(s['refunds'])}\n\n"
            f"📦 Учтённая себестоимость: {money(s['cost'])}\nКомиссия: {money(s['commission'])}\n"
            f"Продвижение: {money(s['promotion'])}, резерв: {money(s['reserved'])}\n"
            f"Бонусы: {money(s['bonus_cost'])}; стоимость ещё не добавлена: {s['bonus_unknown']}; без результата доставки: {s['bonus_uncertain']}\n"
            f"KOSell: {s['kosell_native']}. "
            + ("Рублёвый эквивалент USD оценочный.\n" if s["kosell_estimated"] else "\n")
            + f"Внешние закупки: {s['external_native']}; в процессе / на сверке: {s['external_pending']}\n"
            f"\n{'✅ Прибыль' if s['complete'] else '🟡 Предварительный результат'}: {money(s['profit'])}\n"
            f"Ожидают учёта расходов: {s['unknown_costs']}\n"
            + (
                "Расчёт полный."
                if s["complete"]
                else "Для полного расчёта добавьте комиссию и расходы, завершите текущие закупки."
            )
        )

    def export(self, path: Path, start=0, end=None, item_id=None, module=None):
        rows = self.orders(start, end, item_id, module)
        summary = self.summary(rows, start, end, item_id=item_id, module=module)
        external = {p["order_id"]: p for p in self.db.all("SELECT * FROM provider_orders")}
        book = Workbook()
        book.remove(book.active)

        def sheet(name, headers, data):
            ws = book.create_sheet(name)
            ws.append(headers)
            for values in data:
                ws.append([safe_cell(v) for v in values])
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for cell in ws[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="284B63")
            for column in ws.columns:
                ws.column_dimensions[column[0].column_letter].width = min(
                    55, max(16, max(len(str(c.value or "")) for c in column) + 2)
                )

        sheet("Сводка", ["Показатель", "Значение"], summary.items())
        sheet(
            "Сделки",
            [
                "ID",
                "Дата UTC",
                "Товар",
                "Покупатель ID",
                "Статус",
                "Этап",
                "Цена, коп.",
                "Себестоимость, коп.",
                "Модуль",
                "Проблема",
                "Заказ поставщика",
                "Статус поставщика",
                "Затраты в валюте",
                "Валюта",
                "Курс, руб.",
                "Возврат поставщика (справочно)",
            ],
            (
                [
                    r["id"],
                    datetime.fromtimestamp(r["created_at"], timezone.utc).isoformat(),
                    r["title"],
                    r["buyer_id"],
                    r["market_status"],
                    r["stage"],
                    r["price"],
                    r["cost"],
                    r["module"],
                    bool(r["problem"]),
                    *[
                        external.get(r["id"], {}).get(key)
                        for key in (
                            "remote_id",
                            "state",
                            "native_amount",
                            "currency",
                            "rub_rate",
                            "supplier_refund",
                        )
                    ],
                ]
                for r in rows
            ),
        )

        def grouped(field):
            groups = defaultdict(list)
            for row in rows:
                groups[row[field] or "неизвестно"].append(row)
            result = []
            for key, group in groups.items():
                confirmed = [
                    r for r in group if r["market_status"] in {"CONFIRMED", "CONFIRMED_AUTOMATICALLY"}
                ]
                result.append(
                    [
                        key,
                        len(group),
                        sum(r["price"] for r in confirmed),
                        sum(r["cost"] or 0 for r in group),
                        sum(r["cost"] is None for r in confirmed),
                    ]
                )
            return result

        for title, field in [
            ("Товары", "item_id"),
            ("Игры", "game"),
            ("Категории", "category"),
            ("Покупатели", "buyer_id"),
        ]:
            sheet(
                title,
                [
                    "Группа",
                    "Заказов",
                    "Выручка, коп.",
                    "Известные затраты, коп.",
                    "Неизвестная себестоимость",
                ],
                grouped(field),
            )
        sheet(
            "Аренды",
            ["Заказ", "Номер аренды", "Игра", "До UTC", "Закупка, коп."],
            (
                [r["id"], r["rental_number"], r["title"], self._date(r["expires_at"]), r["cost"]]
                for r in rows
                if r["module"] in {"kosell", "own_rent"}
            ),
        )
        sheet(
            "Продления",
            ["Заказ", "Исходный заказ", "Этап", "Закупка, коп.", "До UTC"],
            (
                [r["id"], r["target_order_id"], r["stage"], r["cost"], self._date(r["expires_at"])]
                for r in rows
                if r["module"] in {"extend", "own_extend"}
            ),
        )
        expense_rows = self.db.all(
            "SELECT * FROM expenses WHERE created_at>=? AND created_at<?", (start, end or now() + 1)
        )
        if item_id:
            expense_rows = [e for e in expense_rows if e["item_id"] == item_id]
        if module:
            ids = {r["item_id"] for r in rows}
            expense_rows = [e for e in expense_rows if e["item_id"] in ids]
        sheet(
            "Расходы",
            ["Операция", "Лот", "Вид", "Сумма, коп.", "Статус", "День"],
            ([r["id"], r["item_id"], r["kind"], r["amount"], r["state"], r["day"]] for r in expense_rows),
        )
        sheet(
            "Модули",
            ["Модуль", "Заказов", "Выручка, коп.", "Известные затраты, коп.", "Неизвестная себестоимость"],
            grouped("module"),
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        book.save(path)
        path.chmod(0o600)
        return path

    @staticmethod
    def _date(value):
        return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else ""
