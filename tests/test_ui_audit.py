"""Whole-panel navigation with fake APIs; no live purchases or buyer messages."""

from collections import deque
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.methods import EditMessageText, SendDocument, SendMessage
from conftest import binding, order
from test_external import Supplier, configure
from test_telegram_ui import callbacks
from test_telegram_ui import panel as panel

from shopbot.external import save_provider
from shopbot.models import ServiceError, now
from shopbot.rentals import Rentals
from shopbot.shop_tools import save_template

ITEM = "40ed7272-a692-44cd-b6ce-5d3d1cb2b086"


def screens(calls):
    return [c for c in calls if isinstance(c, (SendMessage, EditMessageText, SendDocument))]


def assert_panel(result):
    text = getattr(result, "text", None) or getattr(result, "caption", None) or ""
    if isinstance(result, SendDocument) and not text:
        assert result.document.filename
        return
    assert text and "##" not in text and "**" not in text
    assert "нужна проверка" not in text.casefold() and "не проверено" not in text.casefold()
    assert "/cancel" not in text and "systemctl" not in text and ".venv/" not in text
    assert result.parse_mode == "HTML"
    if result.reply_markup:
        for row in result.reply_markup.inline_keyboard:
            for button in row:
                assert button.text
                assert not button.callback_data or 1 <= len(button.callback_data.encode()) <= 64


async def test_button_walk_configured_shop(panel, tmp_path):
    ui, session, send = panel
    supplier, _ = configure(ui.engine, "twiboost", item_id="smm")
    split = Supplier("split")

    def api(request):
        return split(request) if request.url.host == "api.split.tg" else supplier(request)

    ui.engine.external.transport = httpx.MockTransport(api)
    save_provider(ui.db, "split", "audit-private-key")
    for provider in ui.db.all("SELECT id FROM providers"):
        await ui.engine.external.check(provider["id"])
    binding(ui.db, item_id="rent", title="🎮 Аренда")
    binding(ui.db, module="stock", item_id="stock", title="📦 Аккаунты")
    binding(ui.db, module="stock", item_id="pool", title="🎮 Свой пул", fulfillment="own_rent")
    binding(ui.db, module="stock", item_id="extension", title="Продление", fulfillment="own_extend")
    Rentals(ui.engine).add("pool", ["login:audit-password"], 100)
    ui.db.add_stock("stock", ["login:audit-stock"], 100)
    save_template(ui.db, "stock", "📋 Мой шаблон")
    deal = order(ui.engine, ui.engine.market, item_id="rent")
    ui.engine.update(deal.id, stage="delivered", expires_at=now() + 3600, rental_uid="lease")
    ui.db.execute("INSERT OR IGNORE INTO chats(id) VALUES (?)", (deal.chat_id,))
    ui.db.set("quick_replies", {"Приветствие": "Здравствуйте!"})

    async def items():
        yield {**await ui.engine.market.item(ITEM), "name": "Новый товар"}

    ui.engine.market.own_items = items
    ui.updates.check = AsyncMock(return_value=None)
    # View screens and form entry buttons are walked. Paid actions, delivery,
    # deletion and confirmations have dedicated transaction tests elsewhere.
    exact = {
        "promotion",
        "templates",
        "scopes",
        "analytics:items",
        "bindings:all",
        "bind:new",
        "uncertain:list",
        "expenses:list",
        "history:confirm",
        "report:all",
        "backup",
        "connect:home",
        "connect:playerok",
        "connect:smm",
        "connect:token",
        "connect:session",
        "connect:email",
        "connect:check:kosell",
        "updates:home",
        "updates:check",
        "s:diagnostics",
        "s:update-status",
        "s:own-catalog",
        "s:loyalty",
        "s:loyalty-threshold",
        "s:bonus-upload",
        "s:bonus-instruction",
        "s:bonus-review",
        "s:quick:edit",
        "s:search",
        "s:bulk",
        "s:margin:1",
        "v6:home",
        "v6:templates",
        "v6:rules",
        "v6:rulenew",
        "v6:external",
        "v6:wallet",
        "v6:tplimport",
        "v6:lotsearch",
        "v6:lotmanual",
        "v6:ruletest",
        "alerts:home",
        "alerts:toggle",
        "connect:status",
        "ui:back",
        "ui:cancel",
        "updates:toggle",
        "s:loyalty-toggle",
        "s:cancel-bulk",
        "s:clear",
        "s:select-page",
    }
    prefixes = (
        "menu:",
        "setup:",
        "plugin:",
        "connect:key:",
        "edit:",
        "text:",
        "scope:",
        "lot:",
        "lotedit:",
        "lotflag:",
        "feature:",
        "toggle:",
        "stock:",
        "orders:",
        "attention-orders:",
        "order:",
        "reply:",
        "resume:",
        "refund:",
        "preview:lot:",
        "alerts:edit:",
        "x:home:",
        "x:check:",
        "x:flag:",
        "x:lotedit:",
        "x:new:",
        "x:toggle:",
        "x:help:",
        "x:provider:",
        "x:rate:",
        "x:low:",
        "x:catalog:",
        "x:orders:",
        "x:services:",
        "x:service-search:",
        "s:inbox:",
        "s:chat:",
        "s:reply:",
        "s:quick:choose:",
        "s:quick:pick:",
        "s:pool:",
        "s:account:",
        "s:own-upload:",
        "s:pool-target:",
        "s:margin:",
        "s:page:",
        "s:filter:",
        "s:bulk-field:",
        "s:bulk-pause:",
        "s:select:",
        "s:reset:",
        "s:bonus-review:",
        "v6:customers:",
        "v6:customerfilter:",
        "v6:customer:",
        "v6:note:",
        "v6:calendar:",
        "v6:stats:",
        "v6:chart:",
        "v6:prices:",
        "v6:price:",
        "v6:input:",
        "v6:tplsource:",
        "v6:tplsave:",
        "v6:template:",
        "v6:tplexport:",
        "v6:targets:",
        "v6:tplpreview:",
        "v6:tplselect:",
        "v6:tplclear:",
        "v6:lots:",
        "v6:picklot:",
        "v6:pay:",
        "v6:tag:",
        "v6:ruletest:",
        "reportitem:",
    )
    queue, seen, opened = deque([()]), set(), set()
    while queue:
        path = queue.popleft()
        if path:
            opened.add(path[-1])
        await send("/start")
        for action in path:
            await send(callback=action)
        result = screens(session.calls)[-1]
        assert_panel(result)
        if path:
            assert "Откройте действие заново" not in (getattr(result, "text", "") or ""), path
        for action in sorted({a for a in callbacks(result) if a} - seen):
            seen.add(action)
            if action in exact or action.startswith(prefixes):
                queue.append((*path, action))
        assert len(seen) < 1000  # prevent accidental infinite pagination
    assert len(seen) >= 150
    assert {"menu:attention", "connect:key:vingboost", "x:home:stars", "x:home:smm"} <= seen
    assert not supplier.creates and not split.creates
    assert not ui.engine.market.sends and not ui.engine.market.transitions
    assert not ui.engine.kosell.rents and not ui.engine.kosell.extensions
    assert sum(isinstance(c, SendMessage) for c in session.calls) == 1
    assert "audit-password" not in str(session.calls) and "audit-private-key" not in str(session.calls)
    # The discovered button inventory makes the scope of the audit inspectable.
    (tmp_path / "button-audit.txt").write_text(
        f"Discovered: {len(seen)}; opened: {len(opened)}\n"
        + "\n".join(("OPEN " if a in opened else "TRANSACTION/CONTEXT ") + a for a in sorted(seen))
    )


@pytest.mark.parametrize(
    "action,menu",
    [
        ("daily_report", "menu:analytics"),
        ("paused", "menu:account"),
        ("paid_enabled", "promotion"),
        ("offline_override", "menu:auto"),
    ],
)
async def test_switch_keeps_settings_and_panel(panel, action, menu):
    ui, session, send = panel
    await send(callback=menu)
    anchor = ui.panels[(ui.bot.id, 42, 42)].message_id
    before = ui.db.get(action)
    await send(callback="toggle:" + action)
    result = screens(session.calls)[-1]
    assert ui.db.get(action) is not bool(before)
    assert isinstance(result, EditMessageText) and result.message_id == anchor
    assert len(callbacks(result)) > 3


async def test_first_form_back_restores_parent_and_second_step_back_restores_prompt(panel):
    _, session, send = panel
    await send(callback="menu:analytics")
    original = screens(session.calls)[-1].text
    await send(callback="edit:report_time")
    await send(callback="ui:back")
    assert screens(session.calls)[-1].text == original
    await send(callback="bind:new")
    await send(ITEM)
    choices = screens(session.calls)[-1].text
    await send(callback="s:bindtype:kosell")
    await send(callback="ui:back")
    assert screens(session.calls)[-1].text == choices


@pytest.mark.parametrize(
    "action",
    [
        "removed:button",
        "connect:removed",
        "x:removed",
        "lot:deleted",
        "stock:deleted",
        "reply:deleted",
        "order:deleted",
    ],
)
async def test_old_buttons_always_give_a_visible_next_step(panel, action):
    _, session, send = panel
    await send("/start")
    count = len(screens(session.calls))
    await send(callback=action)
    assert len(screens(session.calls)) > count
    assert_panel(screens(session.calls)[-1])
    assert callbacks(screens(session.calls)[-1])


@pytest.mark.parametrize("mode,preset", [("stars", "split"), ("smm", "twiboost")])
async def test_external_lot_uses_own_catalog_without_uuid_or_retyping_name(panel, mode, preset):
    ui, session, send = panel
    configure(ui.engine, preset, item_id="existing")

    async def items():
        yield {**await ui.engine.market.item(ITEM), "name": "Красивый лот"}

    ui.engine.market.own_items = items
    await send(callback="x:new:" + mode)
    assert "v6:picklot:0" in callbacks(session.calls[-1])
    await send(callback="v6:picklot:0")
    assert any(c.startswith("x:choose:") for c in callbacks(session.calls[-1]))
    assert not ui.db.one("SELECT item_id FROM bindings WHERE item_id=?", (ITEM,))


@pytest.mark.parametrize("fail", [False, True])
async def test_supplier_refresh_result_survives_panel_render_and_timeout(panel, fail):
    ui, session, send = panel
    _, pid = configure(ui.engine, "twiboost")
    if fail:
        ui.engine.external.check = AsyncMock(side_effect=TimeoutError())
    await send(callback=f"x:check:{pid}")
    result = screens(session.calls)[-1]
    assert ("Данные не обновлены" if fail else "баланс обновлён") in result.text
    assert f"x:check:{pid}" in callbacks(result)
    assert_panel(result)


async def test_provider_enable_auto_refreshes_and_old_confirmation_is_invalidated(panel):
    ui, session, send = panel
    _, pid = configure(ui.engine, "twiboost")
    ui.db.execute("UPDATE providers SET enabled=0,checked_at=0,checked_revision=0")
    await send(callback=f"x:toggle:{pid}")
    confirm = next(c for c in callbacks(session.calls[-1]) if c.startswith("x:confirm:"))
    assert ui.db.one("SELECT checked_at FROM providers")["checked_at"] > 0
    await send(callback="menu:home")
    await send(callback=confirm)
    assert not ui.db.one("SELECT enabled FROM providers")["enabled"]


@pytest.mark.parametrize("accessible", [True, False])
async def test_refund_checks_real_playerok_access_instead_of_legacy_status_text(panel, accessible):
    ui, session, send = panel
    binding(ui.db)
    deal = order(ui.engine, ui.engine.market)
    ui.db.set("readiness_status", "Playerok: подключён · KOSell: подключён")
    ui.engine.kosell.balance = AsyncMock(side_effect=ServiceError("kosell_network"))
    if not accessible:
        ui.engine.market.profile = AsyncMock(side_effect=ServiceError("playerok_network"))
    refund = AsyncMock(wraps=ui.engine.market.refund)
    ui.engine.market.refund = refund
    await send(callback="refund:" + deal.id)
    assert not refund.called
    await send(callback="refund_yes:" + deal.id)
    assert refund.call_count == int(accessible)
    assert not ui.engine.kosell.balance.called
    await send(callback="refund_yes:" + deal.id)
    assert refund.call_count == int(accessible)
    assert (ui.engine.market.deals[deal.id].status == "ROLLED_BACK") is accessible


async def test_errors_have_actions_and_never_echo_provider_text(panel):
    ui, session, send = panel
    binding(ui.db)
    deal = order(ui.engine, ui.engine.market)
    ui.engine.update(deal.id, stage="review", error="provider_private-token-LEAK")
    await send(callback="order:" + deal.id)
    assert "LEAK" not in session.calls[-1].text
    assert "Обработка приостановлена" in session.calls[-1].text
    assert "Следующий шаг" in session.calls[-1].text
    await send(callback="menu:attention")
    assert "Приостановлено заказов: 1" in session.calls[-1].text
    assert "Нет незавершённых" not in session.calls[-1].text


async def test_fresh_install_all_main_sections_open_without_configuration(panel):
    ui, session, send = panel
    await send("/start")
    for action in (
        "menu:auto",
        "menu:profile",
        "menu:plugins",
        "menu:analytics",
        "menu:account",
        "menu:help",
        "menu:attention",
        "bindings:all",
        "orders:0",
        "uncertain:list",
        "expenses:list",
        "v6:home",
        "v6:templates",
        "v6:rules",
        "v6:customers:0",
        "v6:prices:0",
        "v6:calendar:7:0",
        "v6:stats:7",
        "s:own-catalog",
        "s:loyalty",
        "x:home:stars",
        "x:home:smm",
    ):
        await send(callback=action)
        assert_panel(screens(session.calls)[-1])
    assert sum(isinstance(c, SendMessage) for c in session.calls) == 1
    assert not ui.engine.market.transitions and not ui.engine.kosell.rents


async def test_orders_pagination_clamps_stale_pages_and_attention_only_shows_affected_orders(panel):
    ui, session, send = panel
    binding(ui.db)
    for i in range(12):
        deal = order(ui.engine, ui.engine.market, order_id=f"order-{i}")
        ui.engine.update(deal.id, stage="failed" if i == 0 else "delivered")
    await send(callback="orders:0")
    assert "orders:1" in callbacks(session.calls[-1])
    await send(callback="orders:999")
    assert "orders:1000" not in callbacks(session.calls[-1])
    assert "orders:0" in callbacks(session.calls[-1])
    await send(callback="attention-orders:0")
    assert "order:order-0" in callbacks(session.calls[-1])
    assert not any(c.startswith("order:") and c != "order:order-0" for c in callbacks(session.calls[-1]))
    await send(callback="menu:attention")
    assert "Приостановлено заказов: 1" in session.calls[-1].text


async def test_lost_refund_response_is_never_replayed_by_a_second_click(panel):
    ui, session, send = panel
    binding(ui.db)
    deal = order(ui.engine, ui.engine.market)
    ui.engine.market.refund = AsyncMock(side_effect=ServiceError("playerok_network", ambiguous=True))
    await send(callback="refund_yes:" + deal.id)
    await send(callback="refund_yes:" + deal.id)
    assert ui.engine.market.refund.call_count == 1
    assert ui.db.get("refund_pending:" + deal.id)
    assert ui.engine.market.deals[deal.id].status == "PAID"
    assert "Возврат уже отправлялся" in session.calls[-1].text


@pytest.mark.parametrize("preset", ["split", "twiboost", "vexboost", "jap"])
async def test_key_form_connects_each_supported_supplier_automatically_without_purchases(panel, preset):
    ui, session, send = panel
    supplier = Supplier("split" if preset == "split" else "smm")
    ui.engine.external.transport = httpx.MockTransport(supplier)
    await send(callback="connect:key:" + preset)
    await send("audit-private-api-key")
    provider = ui.db.one("SELECT * FROM providers")
    assert provider["checked_revision"] == provider["revision"]
    assert not provider["enabled"] and provider["checked_at"] > 0
    assert ui.db.decrypt(provider["token"]) == "audit-private-api-key"
    assert "audit-private-api-key" not in str(session.calls) and not supplier.creates
    if preset == "split":
        assert "услуг: 0" not in session.calls[-1].text


async def test_background_failure_notice_is_readable_and_keeps_reason_only_in_diagnostics(panel):
    ui, _, _ = panel
    binding(ui.db)
    deal = order(ui.engine, ui.engine.market)
    ui.db.set("admins", [42])
    ui.engine.fail(deal.id, "transport-private-LEAK", review=True)
    notice = ui.db.one("SELECT ciphertext FROM outbox WHERE channel='telegram' ORDER BY id DESC LIMIT 1")
    text = ui.db.decrypt(notice["ciphertext"])
    assert "LEAK" not in text and "требуется проверка" not in text
    assert ui.engine.order(deal.id)["error"] == "transport-private-LEAK"


@pytest.mark.parametrize("mode", ["stock", "kosell", "extend", "own_rent", "own_extend", "stars", "smm"])
async def test_each_fulfillment_button_enters_the_correct_wizard_without_delivery(panel, mode):
    ui, session, send = panel
    await send(callback="bind:new")
    await send(ITEM)
    assert "s:bindtype:" + mode in callbacks(session.calls[-1])
    await send(callback="s:bindtype:" + mode)
    assert_panel(screens(session.calls)[-1])
    assert "название" in session.calls[-1].text.casefold()
    assert not ui.db.all("SELECT * FROM bindings")
    assert not ui.engine.market.transitions and not ui.engine.kosell.rents


async def test_bulk_empty_selection_guides_to_catalog_and_preview_has_readable_values(panel):
    ui, session, send = panel
    binding(ui.db, module="stock")
    await send(callback="bindings:all")
    await send(callback="s:bulk")
    assert "Сначала выберите лоты" in session.calls[-1].text
    await send(callback="s:select-page")
    await send(callback="s:bulk")
    await send(callback="s:bulk-pause:1")
    assert "Пауза: включить" in session.calls[-1].text
    assert not ui.db.one("SELECT paused FROM bindings")["paused"]
    assert not ui.engine.market.transitions
