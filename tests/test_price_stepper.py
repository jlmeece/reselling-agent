"""Tap-stepper price editor for ACTIVE listings (agents/telegram_bot.py, 🎚️ Adjust)."""
import asyncio
import html
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agents.telegram_bot as tb

ITEM_ID = "123456789012"
ITEM_2 = "223456789012"
ITEM_3 = "323456789012"
URL = f"https://www.ebay.com/itm/{ITEM_ID}"
COL = {"status": "A", "title": "C", "costco_cost": "G", "ebay_price": "H", "net_profit": "I",
       "ebay_listing_url": "Q", "fee_rate": "AB", "ship_cost": "AD", "ad_cost": "AE",
       "buy_cost": "BB", "lowest_active": "BF"}


def _p(price="49.99", cost="30", fee="0.1325", lowest="45.00", title="Energy Shot"):
    return {"title": title, "status": "ACTIVE", "costco_cost": cost, "ebay_price": price,
            "fee_rate": fee, "ship_cost": "", "ad_cost": "", "buy_cost": "",
            "lowest_active": lowest, "ebay_listing_url": URL}


def _labels(kb):
    return [b.text for r in kb.inline_keyboard for b in r]


def _datas(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]


def _sheet_row(status, title, net, url, cost="30", price="49.99"):
    row = [""] * 58
    row[0], row[2], row[6], row[7], row[8], row[16], row[27] = status, title, cost, price, net, url, "0.1325"
    return row


# ── _parse_step_arg ──────────────────────────────────────────────────────────

def test_parse_step_arg_valid():
    assert tb._parse_step_arg(f"{ITEM_ID}:4999") == (ITEM_ID, 4999, None)
    assert tb._parse_step_arg(f"{ITEM_ID}:4999:-25") == (ITEM_ID, 4999, -25)


@pytest.mark.parametrize("arg", [None, "", ITEM_ID, f"{ITEM_ID}:abc", f"{ITEM_ID}:10:x",
                                 "notanid:4999", f"{ITEM_ID}:1:2:3", f"{ITEM_ID}:4.5"])
def test_parse_step_arg_rejects_bad(arg):
    assert tb._parse_step_arg(arg) is None


# ── _stepper_screen ──────────────────────────────────────────────────────────

def test_screen_shows_reference_lines_and_buttons():
    text, kb = tb._stepper_screen(dict(_p(), row_num=7), 4999, ITEM_ID)
    assert "Energy Shot" in text and "New price: <b>$49.99</b>" in text and "eBay now: $49.99" in text
    assert "⚡ cheapest competitor: $45.00" in text
    assert "🛑 break-even: $" in text and "Net: $" in text and "% margin" in text
    assert "below break-even" not in text and "over the cheapest" not in text
    assert kb.inline_keyboard[0][0].text == "−$5" and kb.inline_keyboard[0][-1].text == "+$5"
    assert [b.callback_data for b in kb.inline_keyboard[0]] == [
        f"reprice:nudge:{ITEM_ID}:4999:{d}" for d in (-500, -100, -25, 25, 100, 500)]
    assert [b.text for b in kb.inline_keyboard[0]] == ["−$5", "−$1", "−$0.25", "+$0.25", "+$1", "+$5"]
    assert any(t.startswith("⚡ cheapest: $") for t in _labels(kb))
    assert "✓ Apply $49.99" in _labels(kb)
    assert f"reprice:stepapply:{ITEM_ID}:4999" in _datas(kb)
    assert not any(d.startswith("reprice:nav:") for d in _datas(kb))      # editor has no prev/next
    assert "activelist:pick:7" in _datas(kb) and "⬅️ Back to card" in _labels(kb)
    assert "activelist:show" in _datas(kb)
    assert all(len(d.encode()) <= 64 for d in _datas(kb))


def test_screen_without_row_num_has_no_back_to_card():
    _text, kb = tb._stepper_screen(_p(), 4999, ITEM_ID)
    assert not any(d.startswith("activelist:pick:") for d in _datas(kb))
    assert "activelist:show" in _datas(kb)


def test_screen_below_break_even_warns_and_relabels_apply():
    text, kb = tb._stepper_screen(_p(), 2000, ITEM_ID)
    assert "⚠️ below break-even — this price loses money" in text
    assert "⚠️ Apply below break-even" in _labels(kb)
    assert not any(t.startswith("✓ Apply") for t in _labels(kb))


def test_screen_over_ceiling_note():
    text, _ = tb._stepper_screen(_p(lowest="45.00"), 6600, ITEM_ID)
    assert "&gt;$20 over the cheapest" in text
    text, _ = tb._stepper_screen(_p(lowest="45.00"), 6500, ITEM_ID)   # exactly $20 over = fine
    assert "over the cheapest" not in text


def test_screen_omits_missing_inputs():
    text, kb = tb._stepper_screen(_p(price="", cost="", lowest=""), 4999, ITEM_ID)
    assert "eBay now" not in text and "cheapest competitor" not in text and "break-even:" not in text
    assert "⚠️ cost or fee rate missing — margin unknown" in text and "Net:" not in text
    assert not any(t.startswith("⚡ cheapest") for t in _labels(kb))     # no competitive target
    assert "✓ Apply $49.99" in _labels(kb)                                # unknown net = plain Apply


# ── _active_stepper_order ────────────────────────────────────────────────────

def test_active_stepper_order_walks_every_live_listing_net_desc():
    rows = [
        _sheet_row("ACTIVE", "Low", "2.00", f"https://www.ebay.com/itm/{ITEM_2}"),
        _sheet_row("ACTIVE", "No id", "50.00", ""),
        _sheet_row("AUDIT_REVIEW", "Audit", "40.00", f"https://www.ebay.com/itm/{ITEM_3}"),
        _sheet_row("ACTIVE", "High", "9.00", URL),
        _sheet_row("PAUSED_MARGIN", "Paused", "5.00", "https://www.ebay.com/itm/423456789012"),
        _sheet_row("ENDED", "Ended", "99.00", "https://www.ebay.com/itm/523456789012"),
        _sheet_row("PAUSED_OOS", "Oos", "98.00", "https://www.ebay.com/itm/623456789012"),
    ]
    order = tb._active_stepper_order((COL, "svc", "Product Tracker", 4, rows))
    assert order == [(6, ITEM_3, "Audit"), (7, ITEM_ID, "High"),
                     (8, "423456789012", "Paused"), (4, ITEM_2, "Low")]


def test_active_stepper_order_sheet_error_is_empty(monkeypatch):
    monkeypatch.setattr(tb, "_read_product_rows", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert tb._active_stepper_order() == []


# ── handlers ─────────────────────────────────────────────────────────────────

@pytest.fixture
def sheet(monkeypatch):
    st = {"rows": [_sheet_row("ACTIVE", "Energy Shot", "5.00", URL)]}
    monkeypatch.setattr(tb, "_read_product_rows", lambda: (COL, "svc", "Product Tracker", 4, st["rows"]))
    return st


def _tap(action, arg, ctx=None):
    query = SimpleNamespace(edit_message_text=AsyncMock(), answer=AsyncMock())
    update = SimpleNamespace(callback_query=query, message=None)
    ctx = ctx or SimpleNamespace(user_data={}, bot_data={"chat_id": 1})
    asyncio.run(action(update, ctx, arg))
    return query


def _screens(monkeypatch):
    seen = []
    real = tb._stepper_screen
    monkeypatch.setattr(tb, "_stepper_screen", lambda p, cents, iid: seen.append((cents, iid)) or real(p, cents, iid))
    return seen


def test_stepper_opens_at_current_price(sheet, monkeypatch):
    seen = _screens(monkeypatch)
    q = _tap(tb.cb_reprice_stepper, ITEM_ID)
    assert seen == [(4999, ITEM_ID)]
    assert "Adjust price" in q.edit_message_text.call_args.args[0]


def test_nudge_adds_delta(sheet, monkeypatch):
    seen = _screens(monkeypatch)
    _tap(tb.cb_reprice_nudge, f"{ITEM_ID}:4999:-25")
    _tap(tb.cb_reprice_nudge, f"{ITEM_ID}:4999:500")
    assert seen == [(4974, ITEM_ID), (5499, ITEM_ID)]


def test_nudge_clamps_at_one_cent(sheet, monkeypatch):
    seen = _screens(monkeypatch)
    _tap(tb.cb_reprice_nudge, f"{ITEM_ID}:100:-500")
    assert seen == [(1, ITEM_ID)]


def test_nudge_never_writes(sheet, monkeypatch):
    monkeypatch.setattr(tb, "_reprice_apply", AsyncMock(side_effect=AssertionError("wrote")))
    monkeypatch.setattr(tb, "safe_write_row", lambda *a, **k: pytest.fail("wrote"))
    _tap(tb.cb_reprice_nudge, f"{ITEM_ID}:4999:100")


def test_nudge_refuses_non_active_row(sheet, monkeypatch):
    sheet["rows"] = [_sheet_row("ENDED", "Energy Shot", "5.00", URL)]
    seen = _screens(monkeypatch)
    q = _tap(tb.cb_reprice_nudge, f"{ITEM_ID}:4999:100")
    call = q.edit_message_text.call_args
    # escaped text is only correct when Telegram parses it as HTML (else "isn&#x27;t" shows)
    assert seen == [] and call.kwargs.get("parse_mode") == "HTML"
    assert "isn't live on eBay" in html.unescape(call.args[0])


@pytest.mark.parametrize("status", ["AUDIT_REVIEW", "PAUSED_MARGIN", "WATCH"])
def test_live_non_active_row_passes_the_gate(sheet, monkeypatch, status):
    sheet["rows"] = [_sheet_row(status, "Energy Shot", "5.00", URL, price="41.48")]
    p, err = tb._price_row_for_item(ITEM_ID, sheet["rows"], COL, 4)
    assert err is None and p["row_num"] == 4
    seen = _screens(monkeypatch)
    q = _tap(tb.cb_reprice_stepper, ITEM_ID)
    assert seen == [(4148, ITEM_ID)] and "Adjust price" in q.edit_message_text.call_args.args[0]


@pytest.mark.parametrize("status", ["ENDED", "PAUSED_OOS"])
def test_not_live_statuses_still_refused(sheet, status):
    sheet["rows"] = [_sheet_row(status, "Energy Shot", "5.00", URL)]
    p, err = tb._price_row_for_item(ITEM_ID, sheet["rows"], COL, 4)
    assert p is None and "isn't live on eBay" in err


def test_row_without_item_id_is_not_live(sheet):
    sheet["rows"] = [_sheet_row("ACTIVE", "Energy Shot", "5.00", "not a url")]
    p, err = tb._price_row_for_item(ITEM_ID, sheet["rows"], COL, 4)
    assert p is None and "not in col Q" in err


def test_stepapply_calls_manual_executor(sheet, monkeypatch):
    apply = AsyncMock(return_value=("✓ Repriced to $47.49", None))
    monkeypatch.setattr(tb, "_reprice_apply", apply)
    q = _tap(tb.cb_reprice_stepapply, f"{ITEM_ID}:4749")
    apply.assert_awaited_once_with(ITEM_ID, 47.49, manual=True)
    assert q.edit_message_text.call_args_list[0].args[0].startswith("⏳")
    assert "✓ Repriced" in q.edit_message_text.call_args.args[0]


def test_stepapply_double_tap_ignored(sheet, monkeypatch):
    apply = AsyncMock()
    monkeypatch.setattr(tb, "_reprice_apply", apply)
    ctx = SimpleNamespace(user_data={}, bot_data={"chat_id": 1, "reprice_inflight": {ITEM_ID}})
    _tap(tb.cb_reprice_stepapply, f"{ITEM_ID}:4749", ctx)
    apply.assert_not_awaited()


def test_cheapest_sets_competitive_target(sheet, monkeypatch):
    sheet["rows"][0][57] = "60.00"                      # col BF lowest_active
    seen = _screens(monkeypatch)
    q = _tap(tb.cb_reprice_cheapest, f"{ITEM_ID}:4999")
    assert seen == [(5999, ITEM_ID)]                    # $0.01 under the cheapest
    q.answer.assert_awaited()


def test_cheapest_without_target_toasts_and_keeps_screen(sheet, monkeypatch):
    seen = _screens(monkeypatch)
    q = _tap(tb.cb_reprice_cheapest, f"{ITEM_ID}:4999")   # no col BF
    assert seen == [] and q.edit_message_text.await_count == 0
    assert "competitively" in q.answer.call_args.args[0]


def test_nav_walks_active_order(sheet, monkeypatch):
    sheet["rows"] = [_sheet_row("ACTIVE", "Second", "2.00", f"https://www.ebay.com/itm/{ITEM_2}"),
                     _sheet_row("ACTIVE", "First", "9.00", URL)]
    seen = _screens(monkeypatch)
    _tap(tb.cb_reprice_nav, f"{ITEM_ID}:next")
    assert seen == [(4999, ITEM_2)]
    q = _tap(tb.cb_reprice_nav, f"{ITEM_ID}:prev")
    assert seen == [(4999, ITEM_2)] and q.answer.call_args.args[0] == "no more that way"


def test_on_callback_lets_toast_handlers_answer_once(monkeypatch):
    handler = AsyncMock()
    monkeypatch.setitem(tb._CALLBACK_ROUTES, ("reprice", "nav"), handler)
    monkeypatch.setattr(tb, "_authorized", lambda update, chat_id: True)
    query = SimpleNamespace(data=f"reprice:nav:{ITEM_ID}:next", answer=AsyncMock())
    update = SimpleNamespace(callback_query=query)
    asyncio.run(tb.on_callback(update, SimpleNamespace(bot_data={"chat_id": 1})))
    query.answer.assert_not_awaited()
    handler.assert_awaited_once()


# ── wiring ───────────────────────────────────────────────────────────────────

def test_active_card_has_adjust_not_set_price():
    kb = tb._active_action_kb(7, item_id=ITEM_ID)
    assert "🎚️ Adjust" in _labels(kb) and f"reprice:stepper:{ITEM_ID}" in _datas(kb)
    assert not any(d.startswith("reprice:set:") for d in _datas(kb))
    assert not any(d.startswith("reprice:compete:") for d in _datas(kb))   # ⚡ lives in the editor


def _apply_ok(monkeypatch):
    monkeypatch.setattr(tb, "_reprice_apply", AsyncMock(return_value=("✓ Repriced", None)))


def test_stepapply_offers_next_item_card_for_a_middle_item(sheet, monkeypatch):
    sheet["rows"] = [_sheet_row("ACTIVE", "First", "9.00", f"https://www.ebay.com/itm/{ITEM_2}"),   # row 4
                     _sheet_row("ACTIVE", "Middle", "5.00", URL),                                    # row 5
                     _sheet_row("ACTIVE", "Last", "1.00", f"https://www.ebay.com/itm/{ITEM_3}")]     # row 6
    _apply_ok(monkeypatch)
    kb = _tap(tb.cb_reprice_stepapply, f"{ITEM_ID}:4749").edit_message_text.call_args.kwargs["reply_markup"]
    assert "Next item →" in _labels(kb) and "activelist:pick:6" in _datas(kb)
    assert "activelist:pick:5" in _datas(kb)                 # ⬅️ Back to card
    assert "activelist:show" in _datas(kb)
    assert not any(d.startswith("reprice:stepper:") for d in _datas(kb))


def test_stepapply_last_item_has_no_next(sheet, monkeypatch):
    sheet["rows"] = [_sheet_row("ACTIVE", "First", "9.00", f"https://www.ebay.com/itm/{ITEM_2}"),
                     _sheet_row("ACTIVE", "Last", "1.00", URL)]
    _apply_ok(monkeypatch)
    kb = _tap(tb.cb_reprice_stepapply, f"{ITEM_ID}:4749").edit_message_text.call_args.kwargs["reply_markup"]
    assert "Next item →" not in _labels(kb)
    assert "activelist:pick:5" in _datas(kb) and "activelist:show" in _datas(kb)


def test_stepapply_sheet_down_still_offers_active_list(sheet, monkeypatch):
    _apply_ok(monkeypatch)
    monkeypatch.setattr(tb, "_read_product_rows", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    kb = _tap(tb.cb_reprice_stepapply, f"{ITEM_ID}:4749").edit_message_text.call_args.kwargs["reply_markup"]
    assert _datas(kb) == ["activelist:show"]


def test_stepper_routes_registered():
    for action in ("stepper", "nudge", "cheapest", "stepapply", "nav"):
        assert ("reprice", action) in tb._CALLBACK_ROUTES
    for action in ("set", "setcancel"):                  # old Set Price messages still work
        assert ("reprice", action) in tb._CALLBACK_ROUTES


# ── step tiers + Competitive back button ────────────────────────────────────

def test_step_sizes_tiers():
    assert tb._step_sizes(9999) == tb._STEP_SIZES == (-500, -100, -25, 25, 100, 500)
    assert tb._step_sizes(10000) == tb._STEP_SIZES_BIG == (-5000, -1000, -100, 100, 1000, 5000)
    assert tb._step_sizes(60000) == tb._STEP_SIZES_BIG


@pytest.mark.parametrize("cents", [15000, 60000])
def test_screen_big_ticket_uses_big_steps(cents):
    _, kb = tb._stepper_screen(_p(price=f"{cents / 100:.2f}", lowest=""), cents, ITEM_ID)
    assert [b.text for b in kb.inline_keyboard[0]] == ["−$50", "−$10", "−$1", "+$1", "+$10", "+$50"]
    assert [b.callback_data for b in kb.inline_keyboard[0]] == [
        f"reprice:nudge:{ITEM_ID}:{cents}:{d}" for d in (-5000, -1000, -100, 100, 1000, 5000)]


def test_nudge_with_big_step(sheet, monkeypatch):
    seen = _screens(monkeypatch)
    _tap(tb.cb_reprice_nudge, f"{ITEM_ID}:43899:-5000")
    assert seen == [(38899, ITEM_ID)]


def test_compete_without_comps_offers_back_to_item(sheet):
    q = _tap(tb.cb_reprice_compete, ITEM_ID)                 # no col BF -> no competitive target
    text = q.edit_message_text.call_args.args[0]
    kb = q.edit_message_text.call_args.kwargs["reply_markup"]
    assert "competitively" in text
    assert [(b.text, b.callback_data) for r in kb.inline_keyboard for b in r] == [
        ("⬅️ Back to item", "activelist:pick:4"), ("📊 Dashboard", "menu:dashboard")]


def test_nav_works_from_a_non_active_live_listing(sheet, monkeypatch):
    sheet["rows"] = [_sheet_row("AUDIT_REVIEW", "First", "9.00", URL),
                     _sheet_row("PAUSED_MARGIN", "Second", "2.00", f"https://www.ebay.com/itm/{ITEM_2}")]
    seen = _screens(monkeypatch)
    _tap(tb.cb_reprice_nav, f"{ITEM_ID}:next")
    assert seen == [(4999, ITEM_2)]


def test_stepapply_next_item_follows_views_sort(sheet, monkeypatch):
    """Browsing by views: Next item → is the next-most-viewed listing, not the net neighbour."""
    from tools import ebay_analytics as ea
    ea._write_json(ea.VIEWS_PATH, {"fetched_at": "2026-10-21T00:00:00+00:00", "days": 30,
                                   "views": {ITEM_ID: 50, ITEM_2: 30, ITEM_3: 10}})
    sheet["rows"] = [_sheet_row("ACTIVE", "First", "9.00", f"https://www.ebay.com/itm/{ITEM_2}"),   # row 4, 30 views
                     _sheet_row("ACTIVE", "Middle", "5.00", URL),                                    # row 5, 50 views
                     _sheet_row("ACTIVE", "Last", "1.00", f"https://www.ebay.com/itm/{ITEM_3}")]     # row 6, 10 views
    _apply_ok(monkeypatch)
    ctx = SimpleNamespace(user_data={"activelist_sort": "views"}, bot_data={"chat_id": 1})
    kb = _tap(tb.cb_reprice_stepapply, f"{ITEM_ID}:4749", ctx).edit_message_text.call_args.kwargs["reply_markup"]
    nxt = [b.callback_data for r in kb.inline_keyboard for b in r if b.text == "Next item →"]
    assert nxt == ["activelist:pick:4:views"]                 # net order would be row 6 (Last)
    assert "activelist:pick:5:views" in _datas(kb)            # ⬅️ Back to card keeps the sort
    assert "activelist:show:views" in _datas(kb)

    # tap it: the card opened is "First", and its own Next continues in views order (-> Last)
    import yaml
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "config", "col_map.yaml")) as f:
        full_col = yaml.safe_load(f)["columns"]
    assert all(full_col[k] == v for k, v in COL.items())       # same sheet layout
    monkeypatch.setattr(tb, "_read_product_rows", lambda: (full_col, "svc", "Product Tracker", 4, sheet["rows"]))
    monkeypatch.setattr(tb, "get_pending", lambda item_id: None)
    monkeypatch.setattr(tb, "fetch_item_price", lambda item_id: {})
    _route, _, arg = nxt[0].partition("activelist:pick:")
    q = _tap(tb.cb_activelist_pick, arg, ctx)
    assert "First" in q.edit_message_text.call_args.args[0]
    card = q.edit_message_text.call_args.kwargs["reply_markup"]
    assert [(b.text, b.callback_data) for r in card.inline_keyboard for b in r
            if b.text in ("⬅️ Prev", "Next ➡️")] == [("⬅️ Prev", "activelist:pick:5:views"),
                                                    ("Next ➡️", "activelist:pick:6:views")]
