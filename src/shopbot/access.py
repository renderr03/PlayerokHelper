"""Credential-free connectivity evidence, without challenge solving or retries."""

from __future__ import annotations

import json

import httpx

from .connections import USER_AGENT
from .playerok_transport import PlayerokTransport

SECURITY_MARKERS = (b"ddos-guard", b"checking your browser", b"challenge-platform", b"cf-chl-", b"captcha")


def security_page(response, body=None):
    if response.status_code != 403:
        return False
    content = response.content[:32768] if body is None else body
    header = response.headers.get("server", "").lower()
    content_type = response.headers.get("content-type", "").lower()
    html = "html" in content_type or content.lstrip().lower().startswith((b"<!doctype", b"<html"))
    return html and ("ddos-guard" in header or any(marker in content.lower() for marker in SECURITY_MARKERS))


async def probe_access(*, transport=None):
    checks = []
    async with httpx.AsyncClient(
        base_url="https://playerok.com",
        timeout=httpx.Timeout(8, connect=5),
        follow_redirects=False,
        transport=transport if transport is not None else PlayerokTransport(),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/html;q=0.9",
            "Apollo-Require-Preflight": "true",
        },
    ) as client:
        for endpoint, path, params in [
            ("site", "/", None),
            (
                "api",
                "/graphql",
                {"operationName": "ServerConnectivity", "query": "query ServerConnectivity { __typename }"},
            ),
        ]:
            try:
                client.cookies.clear()
                async with client.stream("GET", path, params=params) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk[: 32768 - len(body)])
                        if len(body) >= 32768:
                            break
                    raw = bytes(body)
                    content_type = response.headers.get("content-type", "").lower()
                    html = "html" in content_type or raw.lstrip().lower().startswith((b"<!doctype", b"<html"))
                    kind = "html" if html else "json" if "json" in content_type else "other"
                    try:
                        data = json.loads(raw)
                    except (ValueError, TypeError):
                        data = None
                    api_ok = (
                        endpoint == "api"
                        and response.is_success
                        and isinstance(data, dict)
                        and isinstance(data.get("data"), dict)
                        and isinstance(data["data"].get("__typename"), str)
                    )
                    checks.append(
                        {
                            "endpoint": endpoint,
                            "status": response.status_code,
                            "kind": kind,
                            "security": security_page(response, raw),
                            "api_ok": bool(api_ok),
                        }
                    )
            except httpx.HTTPError as error:
                checks.append(
                    {
                        "endpoint": endpoint,
                        "status": None,
                        "kind": "timeout" if isinstance(error, httpx.TimeoutException) else "network",
                        "security": False,
                        "api_ok": False,
                    }
                )
    return checks


def probe_text(checks):
    lines = ["Проверка доступа с сервера", "Запросы без токена, Cookie и входа в аккаунт.", ""]
    by_endpoint = {check["endpoint"]: check for check in checks}
    for check in checks:
        label = "Сайт Playerok" if check["endpoint"] == "site" else "API Playerok"
        if check["status"] is None:
            detail = "нет ответа вовремя" if check["kind"] == "timeout" else "ошибка соединения"
        else:
            detail = "HTTP " + str(check["status"])
            detail += {"html": " · HTML", "json": " · JSON", "other": ""}[check["kind"]]
            if check["security"]:
                detail += " · признаки страницы проверки/защиты"
        lines.append(label + ": " + detail)
    api = by_endpoint.get("api", {})
    if api.get("api_ok"):
        lines.append(
            "\nAPI ответил на анонимный запрос. Это не проверяет вашу сессию. Для входа может потребоваться полный Cookie и User-Agent вашей обычной сессии с подходящей сетью/IP."
        )
    elif api.get("status") == 403:
        lines.append(
            "\nAPI вернул 403 на анонимный запрос. Это не определяет действительность токена и не доказывает блокировку IP. Проверьте обычный доступ с сети сервера; при необходимости уточните у Playerok разрешённый способ подключения."
        )
    elif api.get("status") is None:
        lines.append(
            "\nБез ответа API проверить аккаунт нельзя. Сначала восстановите доступ сервера к площадке."
        )
    else:
        lines.append(
            "\nНормальный ответ API не подтверждён. Этот тест не определяет действительность токена и не гарантирует подключение аккаунта."
        )
    return "\n".join(lines)
