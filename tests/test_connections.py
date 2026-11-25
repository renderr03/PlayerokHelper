import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from shopbot.connections import USER_AGENT, EmailLogin, ManagedClient, session_input
from shopbot.models import ServiceError
from shopbot.runtime import market_from_config


def test_session_input_and_header_injection():
    assert session_input("secret-token")["cookie"] == "token=secret-token"
    assert session_input("token=secret-token; ddg=allowed")["cookie"].endswith("ddg=allowed")
    assert (
        session_input('{"cookie":"token=secret-token","user_agent":"my-browser"}')["user_agent"]
        == "my-browser"
    )
    for raw in ["token=x\r\nAuthorization: invalid", "token=x\t", '{"cookie":12}', "unrelated=cookie"]:
        with pytest.raises(ValueError):
            session_input(raw)


async def test_email_contract_retains_cookies_and_does_not_retry():
    calls = []

    async def request(req):
        payload = json.loads(req.content)
        calls.append(payload)
        assert req.url == "https://playerok.com/graphql"
        if payload["operationName"] == "getEmailAuthCode":
            assert payload["variables"] == {"email": "owner@example.org"}
            return httpx.Response(
                200,
                json={"data": {"getEmailAuthCode": True}},
                headers={"set-cookie": "challenge=temporary; Path=/; Secure; HttpOnly"},
            )
        assert payload["variables"] == {"input": {"email": "owner@example.org", "code": "123456"}}
        assert "challenge=temporary" in req.headers["cookie"]
        return httpx.Response(
            200,
            json={"data": {"checkEmailAuthCode": {"id": "seller", "username": "shop"}}},
            headers={"set-cookie": "token=email-session-token; Path=/; Secure; HttpOnly"},
        )

    login = EmailLogin(transport=httpx.MockTransport(request))
    try:
        await login.send("owner@example.org")
        session = await login.confirm("owner@example.org", "123456")
        assert session["cookie"] == "challenge=temporary; token=email-session-token"
        assert session["user_agent"] == USER_AGENT
        assert len(calls) == 2
    finally:
        await login.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, text="browser challenge"),
        httpx.Response(429, text="wait"),
        httpx.Response(200, json={"errors": [{"message": "PRIVATE DATA"}]}),
        httpx.Response(200, json={"data": {"getEmailAuthCode": False}}),
    ],
)
async def test_email_errors_are_safe_single_attempt(response):
    calls = []

    def request(req):
        calls.append(req)
        return response

    login = EmailLogin(transport=httpx.MockTransport(request))
    try:
        with pytest.raises(ServiceError) as error:
            await login.send("owner@example.org")
        assert "PRIVATE" not in str(error.value)
        assert len(calls) == 1
    finally:
        await login.close()


async def test_missing_auth_cookie_does_not_report_success():
    login = EmailLogin(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"data": {"checkEmailAuthCode": {"id": "seller"}}})
        )
    )
    try:
        with pytest.raises(ServiceError, match="playerok_login_requires_browser"):
            await login.confirm("owner@example.org", "123456")
    finally:
        await login.close()


async def test_hot_swap_waits_for_calls_and_keeps_iterators_alive():
    entered, release = asyncio.Event(), asyncio.Event()

    class Client:
        def __init__(self, name):
            self.name, self.closed = name, False

        async def operation(self):
            entered.set()
            await release.wait()
            return self.name

        async def rows(self):
            yield self.name
            assert not self.closed
            yield self.name + "2"

        async def close(self):
            self.closed = True

    old, new = Client("old"), Client("new")
    managed = ManagedClient(old)
    iterator = managed.rows()
    assert await anext(iterator) == "old"
    task = asyncio.create_task(managed.operation())
    await entered.wait()
    swapped = asyncio.Event()

    async def swap():
        async with managed.exclusive():
            managed.replace(new)
            swapped.set()

    change = asyncio.create_task(swap())
    await asyncio.sleep(0)
    assert not swapped.is_set() and not old.closed
    release.set()
    assert await task == "old"
    await change
    assert await managed.operation() == "new"
    assert await anext(iterator) == "old2"
    await iterator.aclose()
    await managed.close()
    assert old.closed and new.closed


async def test_cancelled_exclusive_does_not_freeze_client():
    started, finish = asyncio.Event(), asyncio.Event()

    class Client:
        async def call(self):
            started.set()
            await finish.wait()

    managed = ManagedClient(Client())
    task = asyncio.create_task(managed.call())
    await started.wait()

    async def exclusive():
        async with managed.exclusive():
            pytest.fail("still running")

    waiting = asyncio.create_task(exclusive())
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not managed.blocked
    finish.set()
    await task
    await asyncio.wait_for(managed.call(), 1)


async def test_saved_encrypted_session_overrides_legacy_file(app, tmp_path):
    _, db, _, _ = app
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"cookie": "token=old", "user_agent": USER_AGENT}))
    db.set("playerok_session", db.encrypt(json.dumps(session_input("new-secret-token"))))
    config = SimpleNamespace(session_file=legacy, visibility_file=tmp_path / "visibility.json")
    market = market_from_config(config, db)
    try:
        assert market.client.headers["cookie"] == "token=new-secret-token"
        assert b"new-secret-token" not in db.path.read_bytes()
    finally:
        await market.close()
