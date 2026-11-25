from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal


def now() -> float:
    return time.time()


def timestamp(value: str | float | int | None) -> float:
    if value is None:
        raise ValueError("Отсутствует время события")
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def rub(value: object) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def money(kopeks: int | None) -> str:
    return "неизвестно" if kopeks is None else f"{Decimal(kopeks) / 100:.2f} ₽"


def utc(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


@dataclass(frozen=True)
class Deal:
    id: str
    item_id: str
    buyer_id: str
    buyer_name: str
    chat_id: str
    status: str
    created_at: float
    price: int
    title: str = ""
    game: str = ""
    category: str = ""
    problem: bool = False
    rating: int | None = None
    completed_at: float | None = None


@dataclass(frozen=True)
class Message:
    id: str
    chat_id: str
    user_id: str
    text: str
    created_at: float
    system: bool = False
    user_name: str = ""
    auto_response: bool = False


class ServiceError(Exception):
    """Safe error: never include provider response bodies or secrets."""

    def __init__(self, code: str, *, ambiguous: bool = False, retry_after: int = 0):
        super().__init__(code)
        self.code = code
        self.ambiguous = ambiguous
        self.retry_after = retry_after
