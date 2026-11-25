import json

import httpx
import pytest
from test_telegram_ui import panel as panel

from shopbot.access import probe_access, probe_text
from shopbot.models import ServiceError
from shopbot.playerok import Playerok


async def test_probe_never_sends_credentials_including_site_set_cookies():
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.host == "playerok.com"
        assert not request.headers.get("cookie") and not request.headers.get("authorization")
        if request.url.path == "/":
            return httpx.Response(
                200,
                text="<html>ok</html>",
                headers={"content-type": "text/html", "set-cookie": "token=SERVER_SECRET; Path=/; Secure"},
            )
        assert "__typename" in request.url.params["query"]
        return httpx.Response(200, json={"data": {"__typename": "Query"}, "private": "SERVER_SECRET"})

    checks = await probe_access(transport=httpx.MockTransport(handle))
    assert len(calls) == 2 and all(r.method == "GET" for r in calls)
    assert checks[1]["api_ok"]
    assert "SERVER_SECRET" not in json.dumps(checks) and "SERVER_SECRET" not in probe_text(checks)


async def test_probe_does_not_follow_redirects_or_claim_ip_block():
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": "https://not-playerok.test/path"})
        return httpx.Response(
            403, text="<html>DDoS-Guard PRIVATE_TOKEN</html>", headers={"content-type": "text/html"}
        )

    checks = await probe_access(transport=httpx.MockTransport(handle))
    assert len(calls) == 2 and all(r.url.host == "playerok.com" for r in calls)
    assert checks[1]["security"]
    text = probe_text(checks)
    assert "HTTP 403" in text and "не доказывает блокировку IP" in text
    assert "PRIVATE_TOKEN" not in text


async def test_probe_reports_network_failure_without_leaking_exception():
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("PRIVATE_DATA")

    checks = await probe_access(transport=httpx.MockTransport(handle))
    assert len(calls) == 2
    assert all(c["kind"] == "timeout" and c["status"] is None for c in checks)
    assert "PRIVATE" not in str(checks) and "PRIVATE" not in probe_text(checks)


@pytest.mark.parametrize("html", [True, False])
async def test_only_html_security_denial_gets_specific_classification(html):
    calls = []

    def handle(request):
        calls.append(request)
        if html:
            return httpx.Response(
                403,
                text="<html><title>DDoS-Guard</title>PRIVATE</html>",
                headers={"content-type": "text/html", "server": "ddos-guard"},
            )
        return httpx.Response(
            403, json={"errors": [{"message": "PRIVATE"}]}, headers={"server": "ddos-guard"}
        )

    api = Playerok(
        {"cookie": "token=test-secret", "user_agent": "test-browser"}, transport=httpx.MockTransport(handle)
    )
    try:
        with pytest.raises(ServiceError) as failure:
            await api.profile()
        assert failure.value.code == ("playerok_security_check" if html else "playerok_http_403")
        assert len(calls) == 1 and "PRIVATE" not in str(failure.value)
    finally:
        await api.close()


async def test_full_session_wizard_encrypts_pending_cookie_and_uses_exact_agent(panel):
    from conftest import FakeMarket

    ui, session, send = panel
    received = []

    class Candidate(FakeMarket):
        def __init__(self, session, visibility):
            super().__init__()
            received.append(session)

        async def close(self):
            pass

    ui.playerok_factory = Candidate
    await send(callback="connect:session")
    await send("Cookie: token=PRIVATE_COOKIE; other=example")
    assert "шаг 2 из 2" in session.calls[-1].text
    # The FSM prompt never echoes the entered session, even before profile check.
    assert "PRIVATE_COOKIE" not in session.calls[-1].text
    from aiogram.fsm.storage.base import StorageKey

    pending = await ui.router.parent_router.storage.get_data(
        StorageKey(bot_id=ui.bot.id, chat_id=42, user_id=42)
    )
    assert "PRIVATE_COOKIE" not in json.dumps(pending)
    assert ui.db.decrypt(pending["connection_cookie"]) == "token=PRIVATE_COOKIE; other=example"
    await send("User-Agent: My exact browser agent")
    assert received == [
        {"cookie": "token=PRIVATE_COOKIE; other=example", "user_agent": "My exact browser agent"}
    ]
    stored = json.loads(ui.db.decrypt(ui.db.get("playerok_session")))
    assert stored == received[0]
    assert b"PRIVATE_COOKIE" not in ui.db.path.read_bytes()
    await ui.engine.market.close()


async def test_full_session_cancel_and_group_gate_never_apply_credentials(panel):
    ui, session, send = panel
    received = []
    ui.playerok_factory = lambda *args: received.append(args)
    await send(callback="connect:session", chat_type="group")
    assert "личном чате" in session.calls[-1].text
    await send(callback="connect:session")
    await send("token=private-cookie")
    await send(callback="ui:cancel")
    await send("My real browser agent")
    assert not received and not ui.db.get("playerok_session")


async def test_probe_button_is_throttled_and_keeps_panel_message(panel):
    ui, session, send = panel
    calls = []

    async def probe():
        calls.append(True)
        return [
            {"endpoint": "site", "status": 200, "kind": "html", "security": False, "api_ok": False},
            {"endpoint": "api", "status": 403, "kind": "html", "security": True, "api_ok": False},
        ]

    ui.access_probe = probe
    await send("/start")
    anchor = ui.panels[(ui.bot.id, 42, 42)].message_id
    await send(callback="connect:probe")
    assert "HTTP 403" in session.calls[-1].text and session.calls[-1].message_id == anchor
    await send(callback="connect:probe")
    assert len(calls) == 1
    assert set(ui.db.get("connection_probe")) == {"at", "checks"}
    assert session.calls[-1].message_id == anchor


@pytest.mark.parametrize("expired", [False, True])
async def test_advanced_session_rejects_invalid_agent_or_expired_input(panel, expired):
    from aiogram.fsm.storage.base import StorageKey

    ui, session, send = panel
    calls = []
    ui.playerok_factory = lambda *args: calls.append(args)
    await send(callback="connect:session")
    await send("token=private-cookie")
    if expired:
        key = StorageKey(bot_id=ui.bot.id, chat_id=42, user_id=42)
        await ui.router.parent_router.storage.update_data(key, {"connection_expires": 0})
    await send("My browser" if expired else "Browser\r\nAuthorization: private-token")
    assert not calls and not ui.db.get("playerok_session")
    assert (
        ("истекло" in session.calls[-1].text)
        if expired
        else ("User-Agent не принят" in session.calls[-1].text)
    )


async def test_email_http_403_is_not_reported_as_wrong_code():
    from shopbot.connections import EmailLogin

    login = EmailLogin(transport=httpx.MockTransport(lambda r: httpx.Response(403, text="PRIVATE")))
    try:
        with pytest.raises(ServiceError, match="playerok_http_403"):
            await login.send("owner@example.org")
    finally:
        await login.close()


async def test_pending_full_session_is_cleared_when_navigating_away(panel):
    from aiogram.fsm.storage.base import StorageKey

    ui, _, send = panel
    await send(callback="connect:session")
    await send("token=private-cookie")
    await send(callback="menu:auto")
    data = await ui.router.parent_router.storage.get_data(
        StorageKey(bot_id=ui.bot.id, chat_id=42, user_id=42)
    )
    assert "connection_cookie" not in data


async def test_full_session_403_offers_diagnostic_without_blind_retries(panel):
    ui, session, send = panel
    calls = []

    class Candidate:
        async def profile(self):
            calls.append(True)
            raise ServiceError("playerok_http_403")

        async def close(self):
            pass

    ui.playerok_factory = lambda *args: Candidate()
    await send(callback="connect:session")
    await send("token=private-cookie")
    await send("My exact browser agent")
    assert calls == [True] and not ui.db.get("playerok_session")
    assert "HTTP 403" in session.calls[-1].text and "введённой сессии" in session.calls[-1].text
    assert any(
        b.callback_data == "connect:probe"
        for row in session.calls[-1].reply_markup.inline_keyboard
        for b in row
    )
