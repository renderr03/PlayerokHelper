import json

import httpx
import pytest

from shopbot.kosell import KOSell
from shopbot.models import ServiceError
from shopbot.playerok import Playerok


async def test_kosell_rent_uses_auth_and_idempotency_headers_without_retrying():
    seen = []

    async def handle(request):
        seen.append(request)
        raise httpx.ReadTimeout("lost response")

    api = KOSell("https://example.test", "fake-key", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ServiceError) as failure:
            await api.rent({"product_id": 7, "duration_hours": 1, "currency": "RUB"}, "order-key")
        assert failure.value.ambiguous
        assert len(seen) == 1
        assert seen[0].headers["X-API-Key"] == "fake-key"
        assert seen[0].headers["Idempotency-Key"] == "order-key"
    finally:
        await api.close()


async def test_extension_debit_never_has_transport_retries():
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(503, text="internal diagnostic that must not leak")

    api = KOSell("https://example.test", "key", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ServiceError) as failure:
            await api.extend("uid", 2)
        assert failure.value.ambiguous
        assert "diagnostic" not in str(failure.value)
        assert len(requests) == 1
        assert json.loads(requests[0].content) == {"hours": 2, "currency": "RUB"}
    finally:
        await api.close()


async def test_rate_limit_respects_provider_retry_after():
    api = KOSell(
        "https://example.test",
        "key",
        transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "73"})),
    )
    try:
        with pytest.raises(ServiceError) as failure:
            await api.code("uid")
        assert failure.value.retry_after == 73
    finally:
        await api.close()


async def test_playerok_paginates_more_than_24_deals():
    requests = []

    def handle(request):
        variables = json.loads(request.url.params["variables"])
        requests.append(variables["pagination"])
        page = variables["pagination"]["after"]
        edges = [
            {"node": {"id": str(i), "item": {"user": {"id": "seller"}}}}
            for i in (range(24) if page is None else range(24, 31))
        ]
        return httpx.Response(
            200,
            json={
                "data": {
                    "deals": {"edges": edges, "pageInfo": {"hasNextPage": page is None, "endCursor": "next"}}
                }
            },
        )

    api = Playerok({"cookie": "token=fake", "user_agent": "test"}, transport=httpx.MockTransport(handle))
    api.user_id = "seller"
    try:
        assert len([ref async for ref in api.recent_deal_ids()]) == 31
        assert requests[1]["after"] == "next"
    finally:
        await api.close()


async def test_playerok_mutation_graphql_error_is_ambiguous_and_not_retried():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"errors": [{"message": "resolver failed after commit"}]})

    api = Playerok({"cookie": "token=fake", "user_agent": "test"}, transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ServiceError) as failure:
            await api.mark_sent("order")
        assert failure.value.ambiguous
        assert len(requests) == 1
    finally:
        await api.close()


def test_visibility_configuration_rejects_deletion_as_hiding():
    part = {
        "query": "mutation removeItem($id: UUID!){removeItem(id:$id){id}}",
        "variables": {"id": "{item_id}"},
        "hidden_statuses": ["REMOVED"],
    }
    with pytest.raises(ValueError, match="Удаление"):
        Playerok.validate_visibility({"hide": part, "restore": part})


async def test_builtin_visibility_uses_documented_rest_routes_and_verifies_status():
    item_id = "02d57c00-d5bd-4f31-8a31-ab3b2fb2af13"
    state = "APPROVED"
    paths = []

    def handle(request):
        nonlocal state
        if request.method == "POST":
            paths.append(request.url.path)
            state = "DISCONTINUED" if request.url.path.endswith("/discontinue") else "APPROVED"
            return httpx.Response(204)
        return httpx.Response(200, json={"data": {"item": {"id": item_id, "status": state}}})

    api = Playerok({"cookie": "token=fake", "user_agent": "test"}, transport=httpx.MockTransport(handle))
    try:
        await api.set_visible(item_id, False)
        await api.set_visible(item_id, True)
        assert paths == [
            f"/rest-api/public/item/{item_id}/discontinue",
            f"/rest-api/public/item/{item_id}/republish",
        ]
    finally:
        await api.close()


@pytest.mark.parametrize("status", [200, 400, 500])
@pytest.mark.parametrize(
    "error", [{"message": "PersistedQueryNotFound"}, {"extensions": {"code": "PERSISTED_QUERY_NOT_FOUND"}}]
)
async def test_large_read_queries_register_with_post_only_after_apq_miss(status, error):
    seen = []

    def handle(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(status, json={"errors": [error]})
        return httpx.Response(200, json={"data": {"viewer": {"id": "seller"}}})

    api = Playerok({"cookie": "token=fake", "user_agent": "test"}, transport=httpx.MockTransport(handle))
    try:
        await api.profile()
        assert [r.method for r in seen] == ["GET", "POST"]
        assert "query" not in seen[0].url.params
        assert json.loads(seen[1].content)["query"].startswith("query viewer")
    finally:
        await api.close()


@pytest.mark.parametrize(
    "status,body",
    [
        (500, {"errors": [{"message": "internal private diagnostic"}]}),
        (
            500,
            {"errors": [{"message": "PersistedQueryNotFound"}, {"extensions": {"code": "UNAUTHENTICATED"}}]},
        ),
        (500, {"errors": [{"extensions": ["invalid-shape"]}]}),
        (401, {"errors": [{"message": "PersistedQueryNotFound"}]}),
        (403, {"errors": [{"message": "PersistedQueryNotFound"}]}),
        (429, {"errors": [{"message": "PersistedQueryNotFound"}]}),
    ],
)
async def test_playerok_does_not_retry_other_errors_or_auth_denials(status, body):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json=body)

    api = Playerok(
        {"cookie": "token=private-test-token", "user_agent": "test"}, transport=httpx.MockTransport(handle)
    )
    try:
        with pytest.raises(ServiceError) as failure:
            await api.profile()
        assert len(calls) == 1
        assert "private" not in str(failure.value)
    finally:
        await api.close()


async def test_mutation_never_registers_or_repeats_after_apq_500():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(500, json={"errors": [{"extensions": {"code": "PERSISTED_QUERY_NOT_FOUND"}}]})

    api = Playerok(
        {"cookie": "token=fake-token", "user_agent": "test"}, transport=httpx.MockTransport(handle)
    )
    try:
        with pytest.raises(ServiceError) as failure:
            await api.mark_sent("order")
        assert failure.value.ambiguous
        assert len(calls) == 1 and calls[0].method == "POST"
    finally:
        await api.close()


@pytest.mark.parametrize(
    "code,expected",
    [
        ("UNAUTHENTICATED", "playerok_auth_required"),
        ("FORBIDDEN", "playerok_access_denied"),
        ("GRAPHQL_VALIDATION_FAILED", "playerok_query_rejected"),
        ("UNRECOGNIZED", "playerok_graphql_error"),
    ],
)
async def test_playerok_classifies_safe_graphql_failures(code, expected):
    api = Playerok(
        {"cookie": "token=fake-token", "user_agent": "test"},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"errors": [{"message": "PRIVATE_TOKEN", "extensions": {"code": code}}]}
            )
        ),
    )
    try:
        with pytest.raises(ServiceError) as failure:
            await api.profile()
        assert failure.value.code == expected and "PRIVATE_TOKEN" not in str(failure.value)
    finally:
        await api.close()
