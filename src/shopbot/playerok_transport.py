"""curl_cffi transport for Playerok, retaining HTTPX cookies and error handling."""

from __future__ import annotations

import asyncio
import os

import httpx
from curl_cffi.requests import AsyncSession, RequestsError


def network_error(error, request):
    # libcurl exceptions may include URLs, proxy credentials or response text.
    kind = httpx.ReadTimeout if error.code == 28 else httpx.ReadError
    return kind("Playerok transport request failed", request=request)


class CurlStream(httpx.AsyncByteStream):
    def __init__(self, response, request):
        self.response = response
        self.request = request

    async def __aiter__(self):
        try:
            async for chunk in self.response.aiter_content():
                yield chunk
        except RequestsError as error:
            raise network_error(error, self.request) from None

    async def aclose(self):
        # curl_cffi's aclose alone waits for the entire download. Cancel the
        # transfer when a bounded reader (diagnostics/login) stops early.
        task = self.response.astream_task
        if task is not None and not task.done():
            if self.response.quit_now is not None:
                self.response.quit_now.set()
            task.cancel()
        if task is None:
            await self.response.aclose()
        else:
            await asyncio.gather(task, return_exceptions=True)


class PlayerokTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        # Preserve the runtime's trusted CA and proxy configuration. Never turn
        # verification off or silently fall back to a different network route.
        ca = os.environ.get("CURL_CA_BUNDLE") or os.environ.get("REQUESTS_CA_BUNDLE")
        ca = ca or os.environ.get("SSL_CERT_FILE")
        self.session = AsyncSession(
            impersonate="chrome",
            default_headers=False,
            retry=0,
            allow_redirects=False,
            trust_env=True,
            verify=ca or True,
            discard_cookies=True,
            max_clients=4,
        )

    async def handle_async_request(self, request):
        timeouts = request.extensions.get("timeout", {})
        connect = timeouts.get("connect") or 25
        read = timeouts.get("read") or 25
        try:
            response = await self.session.request(
                request.method,
                str(request.url),
                headers=list(request.headers.raw),
                data=await request.aread(),
                timeout=(connect, read),
                stream=True,
                allow_redirects=False,
                discard_cookies=True,
            )
        except RequestsError as error:
            raise network_error(error, request) from None
        # libcurl already decoded compression and chunk framing. HTTPX must not
        # decode the bytes a second time. Keep duplicate Set-Cookie headers.
        headers = [
            (key, value)
            for key, value in response.headers.multi_items()
            if key.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
        ]
        return httpx.Response(
            response.status_code,
            headers=headers,
            stream=CurlStream(response, request),
        )

    async def aclose(self):
        await self.session.close()
