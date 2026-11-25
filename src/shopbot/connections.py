"""Private-chat credentials, verified email sign-in and graceful client replacement."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from contextlib import asynccontextmanager

import httpx

from .models import ServiceError
from .playerok_transport import PlayerokTransport

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)


def session_input(value):
    value = value.strip()
    if value.lower().startswith("cookie:"):
        value = value.split(":", 1)[1].strip()
    if value.startswith("{"):
        result = json.loads(value)
        if not isinstance(result, dict):
            raise ValueError("Нужен JSON-объект с cookie и user_agent.")
        cookie, ua = result.get("cookie", ""), result.get("user_agent", USER_AGENT)
    else:
        cookie, ua = value if "=" in value else "token=" + value, USER_AGENT
    if not isinstance(cookie, str) or not isinstance(ua, str):
        raise ValueError("Нужен токен или Cookie аккаунта.")
    if (
        not 8 <= len(cookie) <= 16000
        or not 8 <= len(ua) <= 1000
        or any(ord(c) < 32 or ord(c) > 126 for c in cookie + ua)
        or not re.search(r"(?:^|;\s*)token=[^;\s]+", cookie)
    ):
        raise ValueError(
            "Проверьте токен. Для расширенного входа можно отправить JSON с cookie и user_agent."
        )
    return {"cookie": cookie, "user_agent": ua}


class ManagedClient:
    """Keep requests alive during replacement; exclusive tasks drain active calls.

    Retired clients stay open until shutdown so an iterator which yielded before
    replacement can finish with its original session. Exclusive sections don't
    hold iterators across yields, preventing nested-read deadlocks.
    """

    def __init__(self, client):
        self.current = client
        self.clients = [client]
        self.condition = asyncio.Condition()
        self.active = 0
        self.blocked = False
        self.changes = asyncio.Lock()

    @asynccontextmanager
    async def request(self):
        async with self.condition:
            await self.condition.wait_for(lambda: not self.blocked)
            self.active += 1
        try:
            yield
        finally:
            async with self.condition:
                self.active -= 1
                self.condition.notify_all()

    @asynccontextmanager
    async def exclusive(self):
        async with self.changes:
            async with self.condition:
                self.blocked = True
                try:
                    await self.condition.wait_for(lambda: self.active == 0)
                except BaseException:
                    self.blocked = False
                    self.condition.notify_all()
                    raise
            try:
                yield self.current
            finally:
                async with self.condition:
                    self.blocked = False
                    self.condition.notify_all()

    def replace(self, client):
        self.current = client
        self.clients.append(client)

    def __getattr__(self, name):
        attr = getattr(self.current, name)
        if inspect.isasyncgenfunction(attr):

            async def generate(*args, **kwargs):
                iterator = None
                try:
                    while True:
                        async with self.request():
                            if iterator is None:
                                iterator = getattr(self.current, name)(*args, **kwargs)
                            try:
                                result = await anext(iterator)
                            except StopAsyncIteration:
                                return
                        yield result
                finally:
                    if iterator is not None:
                        await iterator.aclose()

            return generate
        if inspect.iscoroutinefunction(attr):

            async def call(*args, **kwargs):
                async with self.request():
                    return await getattr(self.current, name)(*args, **kwargs)

            return call
        return attr

    async def close(self):
        for client in self.clients:
            if hasattr(client, "close"):
                await client.close()


class EmailLogin:
    """Contracts observed in Playerok's public web bundle on 2026-11-25.

    One attempt per call; no challenge solving, paid requests or redirects.
    The cookie jar from sending the code is retained for its confirmation.
    """

    def __init__(self, *, transport=None):
        self.client = httpx.AsyncClient(
            base_url="https://playerok.com",
            timeout=20,
            follow_redirects=False,
            headers={
                "User-Agent": USER_AGENT,
                "Origin": "https://playerok.com",
                "Referer": "https://playerok.com/",
                "Apollo-Require-Preflight": "true",
            },
            transport=transport if transport is not None else PlayerokTransport(),
        )

    async def mutation(self, operation, variables, query):
        try:
            async with self.client.stream(
                "POST",
                "/graphql",
                json={
                    "operationName": operation,
                    "variables": variables,
                    "query": query,
                },
            ) as response:
                if response.status_code == 403:
                    raise ServiceError("playerok_http_403")
                if response.status_code != 200:
                    raise ServiceError("playerok_login_unavailable")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 1_000_000:
                        raise ServiceError("playerok_login_unavailable")
            payload = json.loads(body)
            if not isinstance(payload, dict) or payload.get("errors"):
                raise ServiceError("playerok_login_rejected")
            return payload["data"][operation]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise ServiceError("playerok_login_unavailable") from None

    async def send(self, email):
        result = await self.mutation(
            "getEmailAuthCode",
            {"email": email},
            "mutation getEmailAuthCode($email: String!) { getEmailAuthCode(input: {email: $email}) }",
        )

        if result is None or result is False:
            raise ServiceError("playerok_login_rejected")

    async def confirm(self, email, code):
        result = await self.mutation(
            "checkEmailAuthCode",
            {"input": {"email": email, "code": code}},
            "mutation checkEmailAuthCode($input: CheckEmailAuthCodeInput!) {"
            " checkEmailAuthCode(input: $input) { id username } }",
        )
        cookies = {c.name: c.value for c in self.client.cookies.jar if c.domain.lstrip(".") == "playerok.com"}
        if not isinstance(result, dict) or not result.get("id") or not cookies.get("token"):
            raise ServiceError("playerok_login_requires_browser")
        return session_input(
            json.dumps(
                {
                    "cookie": "; ".join(k + "=" + v for k, v in cookies.items()),
                    "user_agent": USER_AGENT,
                }
            )
        )

    async def close(self):
        await self.client.aclose()
