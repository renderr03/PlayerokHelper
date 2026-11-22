from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from typing import Any

import httpx

from .models import ServiceError


class KOSell:
    def __init__(self, base_url: str, api_key: str, *, transport=None):
        self.client = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-API-Key": api_key},
            timeout=25,
            follow_redirects=False,
            transport=transport,
        )
        self._lock = asyncio.Lock()
        self._not_before = 0

    async def close(self):
        await self.client.aclose()

    async def request(self, method: str, path: str, body=None, *, key=None, read=False) -> Any:
        # No automatic retries for debit operations, including rent. The engine persists
        # the request before the first attempt and owns recovery with the same key.
        attempts = 3 if read else 1
        for attempt in range(attempts):
            if self._not_before > time.time():
                raise ServiceError(
                    "kosell_rate_limit", retry_after=max(1, int(self._not_before - time.time()) + 1)
                )
            try:
                async with self._lock:
                    response = await self.client.request(
                        method,
                        path,
                        json=body,
                        headers={"Idempotency-Key": key} if key else None,
                    )
            except httpx.TransportError:
                if read and attempt + 1 < attempts:
                    await asyncio.sleep(2**attempt)
                    continue
                raise ServiceError("kosell_network", ambiguous=not read) from None
            if response.status_code == 429:
                try:
                    retry = max(1, int(response.headers.get("Retry-After", "30")))
                except ValueError:
                    retry = 30
                self._not_before = time.time() + retry
                raise ServiceError("kosell_rate_limit", retry_after=retry)
            if response.status_code >= 500:
                if read and attempt + 1 < attempts:
                    await asyncio.sleep(2**attempt)
                    continue
                raise ServiceError(f"kosell_http_{response.status_code}", ambiguous=not read)
            if not response.is_success:
                raise ServiceError(f"kosell_http_{response.status_code}")
            try:
                return json.loads(response.content, parse_float=Decimal)
            except (ValueError, TypeError):
                raise ServiceError("kosell_invalid_response", ambiguous=not read) from None

    async def balance(self):
        return await self.request("GET", "/api/v1/account/balance", read=True)

    async def products(self):
        return await self.request("GET", "/api/v1/rental/products?currency=RUB", read=True)

    async def quote(self, product: int, hours: int):
        return await self.request(
            "POST", "/api/v1/rental/calculate-price", {"product_id": product, "hours": hours}, read=True
        )

    async def rent(self, body: dict, key: str):
        return await self.request("POST", "/api/v1/rental/rent", body, key=key)

    async def active(self):
        return await self.request("GET", "/api/v1/rental/active", read=True)

    async def delivery(self, uid: str):
        return await self.request("GET", f"/api/v1/rental/delivery/{uid}", read=True)

    async def credentials(self, uid: str):
        return await self.request("GET", f"/api/v1/rental/{uid}/credentials", read=True)

    async def code(self, uid: str):
        # A code request may have provider-side limits: do not retry automatically.
        return await self.request("GET", f"/api/v1/rental/{uid}/code")

    async def extension_quote(self, uid: str, hours: int):
        return await self.request(
            "POST", "/api/v1/rental/calculate-extend-price", {"rental_uid": uid, "hours": hours}, read=True
        )

    async def extend(self, uid: str, hours: int):
        return await self.request("POST", f"/api/v1/rental/{uid}/extend", {"hours": hours, "currency": "RUB"})
