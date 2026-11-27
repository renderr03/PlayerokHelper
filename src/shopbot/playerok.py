from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from importlib.resources import files
from pathlib import Path

import httpx

from .models import Deal, Message, ServiceError, rub, timestamp
from .playerok_transport import PlayerokTransport

CONTRACT_NAMES = {
    "viewer": "viewer",
    "deal": "deal",
    "deals": "deals",
    "chat": "chat",
    "userChats": "user-chats",
    "chatMessages": "chat-messages",
    "createChatMessage": "create-chat-message",
    "item": "item",
    "items": "items",
    "itemPriorityStatuses": "item-priority-statuses",
    "publishItem": "publish-item",
    "increaseItemPriorityStatus": "increase-item-priority-status",
    "updateDeal": "update-deal",
}

# Routes documented by th1ks/PlayerokAPI, methods/items.py and common/endpoints.py.
# Runtime readiness still requires an observed roundtrip on this seller's account.
DEFAULT_VISIBILITY = {
    "hide": {
        "transport": "rest",
        "path": "/rest-api/public/item/{item_id}/discontinue",
        "hidden_statuses": ["DISCONTINUED"],
    },
    "restore": {
        "transport": "rest",
        "path": "/rest-api/public/item/{item_id}/republish",
        "hidden_statuses": ["DISCONTINUED"],
    },
}


class Playerok:
    """Unofficial GraphQL adapter. Mutations are never retried by the transport.

    Reversible visibility uses documented REST routes. A live roundtrip proof for
    this account and contract is required by the production readiness gate.
    """

    configured = True

    def __init__(self, session: dict, visibility_file: Path | None = None, *, transport=None):
        if not session.get("cookie") or not session.get("user_agent"):
            raise ValueError("playerok.json должен содержать cookie и user_agent")
        self.client = httpx.AsyncClient(
            base_url="https://playerok.com",
            timeout=25,
            http2=True,
            follow_redirects=False,
            headers={
                "Cookie": session["cookie"],
                "User-Agent": session["user_agent"],
                "Origin": "https://playerok.com",
                "Referer": "https://playerok.com/",
                "Apollo-Require-Preflight": "true",
            },
            transport=transport if transport is not None else PlayerokTransport(),
        )
        self._requests = asyncio.Semaphore(4)
        self._not_before = 0
        self.user_id = ""
        self.visibility = (
            json.loads(visibility_file.read_text())
            if visibility_file and visibility_file.exists()
            else DEFAULT_VISIBILITY
        )
        if self.visibility:
            self.validate_visibility(self.visibility)

    @staticmethod
    def validate_visibility(contract: dict):
        # Only same-origin operations; never execute arbitrary source or delete.
        for action in ("hide", "restore"):
            part = contract[action]
            if part.get("transport") == "rest":
                expected = "/rest-api/public/item/{item_id}/" + (
                    "discontinue" if action == "hide" else "republish"
                )
                if part.get("path") != expected or part.get("hidden_statuses") != ["DISCONTINUED"]:
                    raise ValueError("Неверный контракт REST скрытия")
                continue
            doc = part["query"]
            if re.search(r"\b(remove|delete)\w*\s*\(", doc, flags=re.I):
                raise ValueError("Удаление не является временным скрытием")
            if not doc.lstrip().startswith("mutation ") or "{item_id}" not in json.dumps(part["variables"]):
                raise ValueError("Нужны mutation и переменная {item_id}")
            if not part.get("hidden_statuses"):
                raise ValueError("Укажите реальные hidden_statuses контракта")
            if set(part["hidden_statuses"]) & {"REMOVED", "SOLD", "BLOCKED", "APPROVED"}:
                raise ValueError("Статус не подходит для временного скрытия")
            if re.search(
                r"\b(publishItem|increaseItemPriorityStatus|updateDeal|requestWithdrawal)\s*\(", doc
            ):
                raise ValueError("Платные операции и изменение сделок не являются переключением видимости")

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.visibility, sort_keys=True).encode()).hexdigest()

    async def close(self):
        await self.client.aclose()

    async def graphql(self, operation: str, variables=None, *, query=None, mutation=False, fresh=False):
        async with self._requests:
            if time.time() < self._not_before:
                raise ServiceError(
                    "playerok_rate_limit", retry_after=max(1, int(self._not_before - time.time()) + 1)
                )
            return await self._graphql(operation, variables, query=query, mutation=mutation, fresh=fresh)

    async def _graphql(self, operation: str, variables=None, *, query=None, mutation=False, fresh=False):
        if query is None:
            name = CONTRACT_NAMES[operation]
            query = files("shopbot").joinpath(f"contracts/{name}.graphql").read_text()
        payload = {"operationName": operation, "query": query, "variables": variables or {}}
        extensions = {
            "persistedQuery": {"version": 1, "sha256Hash": hashlib.sha256(query.encode()).hexdigest()}
        }
        try:
            if mutation or fresh:
                response = await self.client.post("/graphql", json=payload)
            else:
                response = await self.client.get(
                    "/graphql",
                    params={
                        "operationName": operation,
                        "variables": json.dumps(variables or {}),
                        "extensions": json.dumps(extensions),
                    },
                )
                # Playerok also returns a structured APQ cache miss with HTTP
                # 400/500. Register only this read query, exactly once. Other
                # server errors, auth failures and mutations are never retried.
                if response.status_code in {200, 400, 500}:
                    try:
                        initial = response.json()
                    except ValueError:
                        initial = None
                    errors = initial.get("errors") if isinstance(initial, dict) else None
                    missing = (
                        isinstance(errors, list)
                        and bool(errors)
                        and all(
                            isinstance(error, dict)
                            and (
                                (
                                    isinstance(error.get("extensions"), dict)
                                    and error["extensions"].get("code") == "PERSISTED_QUERY_NOT_FOUND"
                                )
                                or error.get("message") == "PersistedQueryNotFound"
                            )
                            for error in errors
                        )
                    )
                    if missing:
                        response = await self.client.post(
                            "/graphql", json={**payload, "extensions": extensions}
                        )
        except httpx.TransportError:
            raise ServiceError("playerok_network", ambiguous=mutation) from None
        if response.status_code == 429:
            try:
                delay = max(1, min(3600, int(response.headers.get("Retry-After", "30"))))
            except ValueError:
                delay = 30
            self._not_before = time.time() + delay
            raise ServiceError("playerok_rate_limit", retry_after=delay)
        if response.status_code == 403:
            from .access import security_page

            if security_page(response):
                raise ServiceError("playerok_security_check")
        if not response.is_success:
            raise ServiceError(
                f"playerok_http_{response.status_code}", ambiguous=mutation and response.status_code >= 500
            )
        try:
            body = response.json()
        except ValueError:
            raise ServiceError("playerok_invalid_response", ambiguous=mutation) from None
        if not isinstance(body, dict):
            raise ServiceError("playerok_invalid_response", ambiguous=mutation)
        if body.get("errors") or not isinstance(body.get("data"), dict):
            errors = body.get("errors")
            codes = (
                {
                    error["extensions"]["code"]
                    for error in errors
                    if isinstance(error, dict)
                    and isinstance(error.get("extensions"), dict)
                    and isinstance(error["extensions"].get("code"), str)
                }
                if isinstance(errors, list)
                else set()
            )
            code = "playerok_graphql_error"
            if codes.intersection({"UNAUTHENTICATED", "UNAUTHORIZED"}):
                code = "playerok_auth_required"
            elif "FORBIDDEN" in codes:
                code = "playerok_access_denied"
            elif "GRAPHQL_VALIDATION_FAILED" in codes:
                code = "playerok_query_rejected"
            raise ServiceError(code, ambiguous=mutation)
        return body["data"]

    async def profile(self):
        profile = (await self.graphql("viewer"))["viewer"]
        if not profile or not profile.get("id"):
            raise ServiceError("playerok_auth_required")
        self.user_id = profile["id"]
        return profile

    async def pages(self, operation, variables, field):
        cursor = None
        seen = set()
        while True:
            variables["pagination"] = {"first": 24, "after": cursor}
            page = (await self.graphql(operation, variables))[field]
            yield [edge["node"] for edge in page["edges"]]
            info = page["pageInfo"]
            if not info["hasNextPage"]:
                break
            cursor = info["endCursor"]
            if not cursor or cursor in seen:
                raise ServiceError("playerok_pagination_loop")
            seen.add(cursor)

    async def recent_deal_ids(self, statuses=None):
        # Always paginate: a reconnect must not lose the 25th paid order.
        async for rows in self.pages(
            "deals",
            {
                "filter": {"userId": self.user_id, "status": statuses},
                "showForbiddenImage": False,
            },
            "deals",
        ):
            for row in rows:
                if (row.get("item", {}).get("user") or {}).get("id") == self.user_id:
                    yield row["id"]

    async def deal(self, deal_id: str) -> Deal:
        d = (
            await self.graphql(
                "deal", {"id": deal_id, "hasSupportAccess": False, "showForbiddenImage": False}
            )
        )["deal"]
        item = d["item"]
        if (item.get("user") or {}).get("id") != self.user_id:
            raise ServiceError("playerok_not_seller")
        buyer = d["user"]
        if buyer["id"] == self.user_id:
            buyer = item.get("buyer") or {}
        if not buyer.get("id") or not d.get("chat", {}).get("id"):
            raise ServiceError("playerok_missing_buyer_or_chat")
        testimonial = d.get("testimonial") or {}
        return Deal(
            id=d["id"],
            item_id=item["id"],
            buyer_id=buyer["id"],
            buyer_name=buyer.get("username", ""),
            chat_id=d["chat"]["id"],
            status=d["status"],
            created_at=timestamp(d["createdAt"]),
            price=rub(item.get("rawPrice", item["price"])),
            title=item["name"],
            game=(item.get("game") or {}).get("name", ""),
            category=(item.get("category") or {}).get("name", ""),
            problem=bool(d.get("hasProblem")),
            rating=testimonial.get("rating"),
            completed_at=timestamp(d["completedAt"]) if d.get("completedAt") else None,
        )

    async def messages(self, chat_id: str, since: float = 0) -> list[Message]:
        result = []
        async for rows in self.pages(
            "chatMessages",
            {
                "filter": {"chatId": chat_id},
                "hasSupportAccess": False,
                "showForbiddenImage": False,
            },
            "chatMessages",
        ):
            for d in rows:
                if d.get("deletedAt"):
                    continue
                created = timestamp(d["createdAt"])
                if created < since:
                    continue
                result.append(
                    Message(
                        d["id"],
                        chat_id,
                        (d.get("user") or {}).get("id", ""),
                        d.get("text") or "",
                        created,
                        bool(d.get("event") or d.get("isAutoResponse")),
                        str((d.get("user") or {}).get("username") or "")[:80],
                        bool(d.get("isAutoResponse")),
                    )
                )
            if rows and all(timestamp(d["createdAt"]) < since for d in rows):
                break
        return sorted(result, key=lambda m: (m.created_at, m.id))

    async def chats(self):
        async for rows in self.pages(
            "userChats",
            {
                "filter": {"userId": self.user_id, "type": "PM"},
                "hasSupportAccess": False,
            },
            "chats",
        ):
            for row in rows:
                yield row

    async def send_message(self, chat_id: str, text: str) -> str:
        data = await self.graphql(
            "createChatMessage",
            {
                "input": {"chatId": chat_id, "imagesIds": [], "text": text},
                "showForbiddenImage": False,
            },
            mutation=True,
        )
        return data["createChatMessage"]["id"]

    async def mark_sent(self, deal_id: str):
        await self.graphql(
            "updateDeal",
            {"input": {"id": deal_id, "status": "SENT"}, "showForbiddenImage": False},
            mutation=True,
        )

    async def refund(self, deal_id: str):
        await self.graphql(
            "updateDeal",
            {"input": {"id": deal_id, "status": "ROLLED_BACK"}, "showForbiddenImage": False},
            mutation=True,
        )

    async def item(self, item_id: str):
        return (
            await self.graphql(
                "item", {"id": item_id, "hasSupportAccess": False, "showForbiddenImage": False}
            )
        )["item"]

    async def visibility_item(self, item_id: str):
        # Read through POST to avoid a cached APQ GET after a visibility change.
        return (
            await self.graphql(
                "item",
                {"id": item_id, "hasSupportAccess": False, "showForbiddenImage": False},
                fresh=True,
            )
        )["item"]

    async def item_by_slug(self, slug: str):
        return (
            await self.graphql("item", {"slug": slug, "hasSupportAccess": False, "showForbiddenImage": False})
        )["item"]

    async def tiers(self, item: dict):
        return (
            await self.graphql(
                "itemPriorityStatuses",
                {
                    "itemId": item["id"],
                    "price": item.get("rawPrice", item["price"]),
                },
            )
        )["itemPriorityStatuses"]

    async def listing_action(self, item_id: str, tier_id: str, kind: str):
        operation = "publishItem" if kind == "publish" else "increaseItemPriorityStatus"
        return (
            await self.graphql(
                operation,
                {
                    "input": {
                        "itemId": item_id,
                        "priorityStatuses": [tier_id],
                        "transactionProviderId": "LOCAL",
                    },
                    "showForbiddenImage": False,
                },
                mutation=True,
            )
        )[operation]

    async def set_visible(self, item_id: str, visible: bool):
        if not self.visibility:
            raise ServiceError("visibility_contract_missing")
        part = self.visibility["restore" if visible else "hide"]

        def substitute(value):
            if isinstance(value, str):
                return value.replace("{item_id}", item_id)
            if isinstance(value, dict):
                return {k: substitute(v) for k, v in value.items()}
            if isinstance(value, list):
                return [substitute(v) for v in value]
            return value

        if part.get("transport") == "rest":
            import uuid

            safe_id = str(uuid.UUID(item_id))
            try:
                response = await self.client.post(part["path"].replace("{item_id}", safe_id))
            except httpx.TransportError:
                raise ServiceError("playerok_visibility_network", ambiguous=True) from None
            if not response.is_success:
                raise ServiceError(
                    f"playerok_visibility_http_{response.status_code}", ambiguous=response.status_code >= 500
                )
            if response.content:
                try:
                    result = response.json()
                except ValueError:
                    raise ServiceError("playerok_visibility_invalid_response", ambiguous=True) from None
                if isinstance(result, dict) and result.get("success") is False:
                    raise ServiceError("playerok_visibility_failed", ambiguous=True)
        else:
            await self.graphql(
                part["operation"], substitute(part["variables"]), query=part["query"], mutation=True
            )
        for attempt in range(3):
            try:
                result = await self.visibility_item(item_id)
            except ServiceError as error:
                # The mutation was sent already; a failed verification read
                # says nothing about whether it was applied.
                raise ServiceError(error.code, ambiguous=True, retry_after=error.retry_after) from None
            hidden = result["status"] in part["hidden_statuses"]
            if (visible and result["status"] == "APPROVED") or (not visible and hidden):
                return result
            if attempt < 2:
                await asyncio.sleep(0.5)
        raise ServiceError("visibility_not_verified", ambiguous=True)
