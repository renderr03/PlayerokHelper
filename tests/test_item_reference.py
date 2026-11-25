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


def confirmation(session):
    return next(
        b.callback_data
        for row in session.calls[-1].reply_markup.inline_keyboard
        for b in row
        if b.callback_data.startswith("connect:visibility:run:")
    )


async def test_public_product_link_is_resolved_before_single_use_visibility_confirmation(panel):
    ui, session, send = panel
    market = ui.engine.market
    calls = []

    async def by_slug(slug):
        calls.append(slug)
        return {**await market.item(ITEM_ID), "name": "FC 27 аренда", "slug": SLUG}

    market.item_by_slug = by_slug
    await send(callback="connect:visibility")
    anchor = ui.panels[(ui.bot.id, 42, 42)].message_id
    await send(LINK)
    assert calls == [SLUG] and not market.transitions
    assert session.calls[-1].message_id == anchor and "FC 27 аренда" in session.calls[-1].text
    confirm = confirmation(session)
    await send(callback=confirm)
    assert market.transitions == [(ITEM_ID, False), (ITEM_ID, True)]
    assert ui.db.get("visibility_proof")["item_id"] == ITEM_ID
    await send(callback=confirm)
    assert len(market.transitions) == 2


async def test_invalid_link_shows_new_feedback_and_invalidates_previous_confirmation(panel):
    ui, session, send = panel
    await send(callback="connect:visibility")
    prompt = session.calls[-1].text
    anchor = ui.panels[(ui.bot.id, 42, 42)].message_id
    await send(ITEM_ID)
    previous = confirmation(session)
    await send("https://playerok.com/profile/myshop")
    assert "Ссылка не распознана" in session.calls[-1].text
    assert session.calls[-1].text != prompt and session.calls[-1].message_id == anchor
    await send(callback=previous)
    assert "устарело" in session.calls[-1].text
    assert not ui.engine.market.transitions


@pytest.mark.parametrize("case", ["missing", "foreign", "inactive", "network", "invalid"])
async def test_public_link_failures_keep_form_and_never_mutate_lots(panel, case):
    ui, session, send = panel
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
    await send(callback="connect:visibility")
    await send(LINK)
    expected = {
        "missing": "не найден",
        "foreign": "другому продавцу",
        "inactive": "сейчас не продаётся",
        "network": "Не удалось получить товар",
        "invalid": "неполные данные",
    }[case]
    assert expected in session.calls[-1].text
    assert not market.transitions and not ui.db.get("visibility_proof")
    assert any(
        b.callback_data == "ui:cancel" for row in session.calls[-1].reply_markup.inline_keyboard for b in row
    )
    await send(callback="ui:cancel")
    assert "Ввод отменён" in session.calls[-1].text


async def test_old_confirmation_cannot_act_on_new_selection(panel):
    ui, session, send = panel
    await send(callback="connect:visibility")
    await send(ITEM_ID)
    previous = confirmation(session)
    await send("76359304-e548-4fd3-a056-a155cab30dd3")
    assert confirmation(session) != previous
    await send(callback=previous)
    assert not ui.engine.market.transitions


@pytest.mark.parametrize("case", ["legacy", "expired", "left_form"])
async def test_legacy_or_expired_visibility_button_never_changes_lot(panel, monkeypatch, case):
    from shopbot.models import now

    ui, session, send = panel
    await send(callback="connect:visibility")
    await send(ITEM_ID)
    confirm = confirmation(session)
    if case == "expired":
        later = now() + 121
        monkeypatch.setattr("shopbot.account_ui.now", lambda: later)
    elif case == "left_form":
        await send(callback="menu:auto")
    else:
        confirm = "connect:visibility:run"
    await send(callback=confirm)
    assert "устарело" in session.calls[-1].text
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
