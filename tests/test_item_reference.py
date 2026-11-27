import json

import httpx
import pytest
from test_telegram_ui import panel as panel

from shopbot.item_reference import item_reference
from shopbot.models import ServiceError
from shopbot.playerok import Playerok

ITEM_ID = "02d57c00-d5bd-4f31-8a31-ab3b2fb2af13"
SLUG = "b2451ca29882-ea-sports-fc-27-arenda-7-dney-ea-app-pc-bystraya-vydacha"
LINK = "https://playerok.com/products/" + SLUG


@pytest.mark.parametrize(
    "value,expected",
    [
        (ITEM_ID, ("id", ITEM_ID)),
        (ITEM_ID.upper(), ("id", ITEM_ID)),
        (ITEM_ID.replace("-", ""), ("id", ITEM_ID)),
        ("https://playerok.com/products/" + ITEM_ID + "-test", ("id", ITEM_ID)),
        (LINK, ("slug", SLUG)),
        (LINK + "/?utm_source=share#details", ("slug", SLUG)),
        (LINK.replace("playerok.com", "www.playerok.com"), ("slug", SLUG)),
    ],
)
def test_item_references_accept_public_slugs_and_preserve_full_ids(value, expected):
    assert item_reference(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        SLUG,
        "https://other.test/products/" + ITEM_ID,
        "https://playerok.com.other.test/products/" + SLUG,
        "https://playerok.com@other.test/products/" + SLUG,
        "https://user:password@playerok.com/products/" + SLUG,
        "https://playerok.com:8443/products/" + SLUG,
        "https://playerok.com/profile/" + ITEM_ID,
        "https://playerok.com/products/one/two",
        "https://playerok.com/products/%2Fetc%2Fpasswd",
        "https://playerok.com/products/" + "a" * 257,
        LINK + "\nsecond",
    ],
)
def test_invalid_references_cannot_fetch_other_origins_or_extract_unrelated_uuids(value):
    with pytest.raises(ValueError):
        item_reference(value)


async def test_slug_lookup_uses_existing_read_contract_and_does_not_treat_short_code_as_uuid():
    seen = []

    def handle(request):
        seen.append(request)
        assert request.url.host == "playerok.com" and request.url.path == "/graphql"
        if request.method == "GET":
            variables = json.loads(request.url.params["variables"])
            assert variables["slug"] == SLUG and "id" not in variables
            return httpx.Response(200, json={"errors": [{"message": "PersistedQueryNotFound"}]})
        payload = json.loads(request.content)
        assert payload["query"].startswith("query item(")
        assert payload["variables"]["slug"] == SLUG and "id" not in payload["variables"]
        return httpx.Response(200, json={"data": {"item": {"id": ITEM_ID, "slug": SLUG}}})

    api = Playerok({"cookie": "token=TEST", "user_agent": "test"}, transport=httpx.MockTransport(handle))
    try:
        assert (await api.item_by_slug(SLUG))["id"] == ITEM_ID
        assert len(seen) == 2  # APQ registration is a read; there are no REST mutations.
    finally:
        await api.close()


async def test_public_product_link_resolution_remains_read_only(panel):
    ui, _, _ = panel
    market = ui.engine.market
    calls = []

    async def by_slug(slug):
        calls.append(slug)
        return {**await market.item(ITEM_ID), "name": "FC 27 аренда", "slug": SLUG}

    market.item_by_slug = by_slug
    result = await ui.resolve_own_lot(LINK, active=True)
    assert calls == [SLUG] and result["id"] == ITEM_ID
    assert not market.transitions and not ui.db.get("visibility_proof")


async def test_invalid_product_link_does_not_make_requests(panel):
    ui, _, _ = panel
    market = ui.engine.market

    async def forbidden(*args):
        raise AssertionError("Invalid URL triggered an API call")

    market.profile = market.item_by_slug = forbidden
    with pytest.raises(ValueError):
        await ui.resolve_own_lot("https://playerok.com/profile/myshop")
    assert not market.transitions


@pytest.mark.parametrize("case", ["missing", "foreign", "inactive", "network", "invalid"])
async def test_public_link_lookup_failure_never_mutates_lots(panel, case):
    ui, _, _ = panel
    market = ui.engine.market

    async def by_slug(slug):
        if case == "network":
            raise ServiceError("playerok_network")
        if case == "missing":
            return None
        return {
            "id": ITEM_ID if case != "invalid" else "short-code",
            "status": "APPROVED" if case != "inactive" else "SOLD",
            "user": {"id": "seller" if case != "foreign" else "other-seller"},
        }

    market.item_by_slug = by_slug
    expected = {
        "missing": "playerok_item_not_found",
        "foreign": "lot_not_owned",
        "inactive": "lot_not_active",
        "network": "playerok_network",
        "invalid": "playerok_item_invalid",
    }[case]
    with pytest.raises(ServiceError, match=expected):
        await ui.resolve_own_lot(LINK, active=True)
    assert not market.transitions and not ui.db.get("visibility_proof")


async def test_retired_confirmation_cannot_act_on_new_binding_selection(panel):
    ui, session, send = panel
    await send(callback="bind:new")
    await send(ITEM_ID)
    await send(callback="connect:visibility:run:previous-nonce")
    assert "Тест скрытия больше не нужен" in session.calls[-1].text
    assert not ui.engine.market.transitions


@pytest.mark.parametrize("case", ["legacy", "expired", "left_form"])
async def test_legacy_visibility_buttons_are_retired_even_with_old_fsm_data(panel, case):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from shopbot.account_ui import ConnectionForm

    ui, session, send = panel
    # All forms of old callback remain harmless, including an active old form.
    confirm = "connect:visibility:run" if case == "legacy" else "connect:visibility:run:old-nonce"
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=ui.bot.id, chat_id=42, user_id=42))
    await state.set_state(ConnectionForm.visibility)
    await state.update_data(
        visibility_item=ITEM_ID,
        visibility_nonce="old-nonce",
        visibility_expires=0 if case == "expired" else 9999999999,
    )
    await send(callback="connect:home")
    message = ui.panels[(ui.bot.id, 42, 42)].as_(ui.bot)
    await ui.connection_callback(message, confirm, state)
    assert "Тест скрытия больше не нужен" in session.calls[-1].text
    assert await state.get_state() is None
    assert not ui.engine.market.transitions


async def test_binding_wizard_accepts_public_link_and_reports_ownership_failure(panel):
    ui, session, send = panel
    market = ui.engine.market
    owned = False

    async def by_slug(slug):
        return {"id": ITEM_ID, "user": {"id": "seller" if owned else "other-seller"}}

    market.item_by_slug = by_slug
    await send(callback="bind:new")
    await send(LINK)
    assert "другому продавцу" in session.calls[-1].text
    owned = True
    await send(LINK)
    assert "Выберите тип выдачи" in session.calls[-1].text
    assert not market.transitions and not ui.db.all("SELECT * FROM bindings")
