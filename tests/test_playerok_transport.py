import asyncio
import gzip
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from curl_cffi.requests import RequestsError
from curl_cffi.requests.headers import Headers

from shopbot.connections import USER_AGENT, EmailLogin
from shopbot.models import ServiceError
from shopbot.playerok import Playerok
from shopbot.playerok_transport import CurlStream, PlayerokTransport


@pytest.fixture
def endpoint(monkeypatch):
    # These integration requests stay on loopback, including when CI has a proxy.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append((self.command, self.path, dict(self.headers), b""))
            if self.path == "/slow":
                self.send_response(200)
                self.send_header("Content-Length", str(4096 * 512))
                self.end_headers()
                try:
                    for _ in range(512):
                        self.wfile.write(b"x" * 4096)
                        self.wfile.flush()
                        time.sleep(0.002)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/must-not-follow")
                self.end_headers()
                return
            raw = gzip.compress(json.dumps({"ok": True}).encode())
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Set-Cookie", "one=1; Path=/")
            self.send_header("Set-Cookie", "two=2; Path=/")
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append((self.command, self.path, dict(self.headers), body))
            self.send_response(204)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_native_curl_preserves_cookie_ua_post_and_decodes_gzip_once(endpoint):
    url, calls = endpoint
    transport = PlayerokTransport()
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get(
            url + "/?read=1", headers={"Cookie": "token=PRIVATE", "User-Agent": "own-UA"}
        )
        assert response.json() == {"ok": True}
        assert len(response.headers.get_list("set-cookie")) == 2
        assert calls[0][2]["Cookie"] == "token=PRIVATE"
        assert calls[0][2]["User-Agent"] == "own-UA"
        assert len(transport.session.cookies.jar) == 0
        await client.post(url + "/write", json={"value": 7})
        assert calls[1][0] == "POST" and json.loads(calls[1][3]) == {"value": 7}
        assert "one=1" in calls[1][2]["Cookie"] and "two=2" in calls[1][2]["Cookie"]
    assert transport.session._closed


async def test_native_curl_never_follows_redirect(endpoint):
    url, calls = endpoint
    async with httpx.AsyncClient(transport=PlayerokTransport(), follow_redirects=False) as client:
        response = await client.get(url + "/redirect", headers={"Cookie": "token=PRIVATE"})
        assert response.status_code == 302
    assert len(calls) == 1 and calls[0][1] == "/redirect"


async def test_native_bounded_stream_can_close_early_and_reuse_client(endpoint):
    url, calls = endpoint
    transport = PlayerokTransport()
    async with httpx.AsyncClient(transport=transport) as client:
        async with asyncio.timeout(3):
            async with client.stream("GET", url + "/slow") as response:
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received >= 32768:
                        break
                assert received < 4096 * 512
            assert (await client.get(url + "/after-close")).json() == {"ok": True}
    assert len(calls) == 2


@pytest.mark.parametrize("mutation", [False, True])
@pytest.mark.parametrize("code", [7, 28, 60])
async def test_curl_network_error_is_sanitized_not_retried_and_mutation_is_uncertain(code, mutation):
    transport = PlayerokTransport()
    transport.session.request = AsyncMock(
        side_effect=RequestsError("PRIVATE_TOKEN and proxy password", code=code)
    )
    api = Playerok({"cookie": "token=PRIVATE", "user_agent": USER_AGENT}, transport=transport)
    try:
        with pytest.raises(ServiceError) as failure:
            await api.graphql("viewer", mutation=mutation)
        assert failure.value.code == "playerok_network"
        assert failure.value.ambiguous is mutation
        assert "PRIVATE" not in str(failure.value)
        assert transport.session.request.await_count == 1
    finally:
        await api.close()


async def test_stream_failure_is_normalized_and_early_close_cancels_transfer():
    async def broken_body():
        yield b"first"
        raise RequestsError("PRIVATE_RESPONSE", code=28)

    task = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    response = SimpleNamespace(
        aiter_content=broken_body, astream_task=task, quit_now=asyncio.Event(), aclose=AsyncMock()
    )
    stream = CurlStream(response, httpx.Request("GET", "https://playerok.com/"))
    with pytest.raises(httpx.ReadTimeout) as failure:
        async for _ in stream:
            pass
    assert "PRIVATE" not in str(failure.value)
    await stream.aclose()
    assert task.cancelled() and response.quit_now.is_set()


@pytest.mark.parametrize("status", [403, 429, 500])
async def test_curl_http_failures_retain_existing_rules_and_no_transport_retry(status):
    transport = PlayerokTransport()

    async def body():
        yield b"<html>DDoS-Guard PRIVATE</html>"

    response = SimpleNamespace(
        status_code=status,
        headers=Headers({"Content-Type": "text/html", "Retry-After": "60"}),
        aiter_content=body,
        astream_task=None,
        aclose=AsyncMock(),
    )
    transport.session.request = AsyncMock(return_value=response)
    api = Playerok({"cookie": "token=PRIVATE", "user_agent": USER_AGENT}, transport=transport)
    try:
        with pytest.raises(ServiceError) as failure:
            await api.graphql("viewer", mutation=True)
        expected = {403: "playerok_security_check", 429: "playerok_rate_limit", 500: "playerok_http_500"}
        assert failure.value.code == expected[status]
        assert transport.session.request.await_count == 1
        kwargs = transport.session.request.call_args.kwargs
        assert kwargs["allow_redirects"] is False and kwargs["discard_cookies"] is True
        assert response.aclose.await_count == 1
    finally:
        await api.close()


async def test_default_playerok_and_email_clients_use_curl_with_runtime_ca(monkeypatch, tmp_path):
    ca = str(tmp_path / "runtime-ca.pem")
    monkeypatch.setenv("SSL_CERT_FILE", ca)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    api = Playerok({"cookie": "token=PRIVATE", "user_agent": USER_AGENT})
    login = EmailLogin()
    try:
        for client in (api.client, login.client):
            transport = client._transport
            assert isinstance(transport, PlayerokTransport)
            assert transport.session.impersonate == "chrome"
            assert transport.session.retry.count == 0
            assert transport.session.verify == ca
            assert transport.session.trust_env is True
            assert transport.session.default_headers is False
            assert not client.follow_redirects
    finally:
        await api.close()
        await login.close()
