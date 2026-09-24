"""Tests for the Telegram bot's "Mark Listed" (READY -> ACTIVE) flow."""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agents.telegram_bot as tb
from agents.telegram_bot import PROTECTED_COLS

COL = {"status": "A", "platform": "E", "title": "C", "ebay_listing_url": "Q"}
URL = "https://www.ebay.com/itm/123456789012"


def _row(status="READY", title="Gold Bar"):
    row = [""] * 17
    row[0], row[2] = status, title
    return row


def _buttons(markup):
    return [b.callback_data for r in markup.inline_keyboard for b in r]


def _query_update():
    """Callback-query update whose edit_message_text records the rendered screen."""
    query = SimpleNamespace(edit_message_text=AsyncMock())
    return SimpleNamespace(callback_query=query, message=None), query


def _text_update(text):
    msg = SimpleNamespace(text=text, reply_text=AsyncMock())
    return SimpleNamespace(callback_query=None, message=msg, effective_chat=SimpleNamespace(id=1)), msg


def _ctx(user_data=None):
    return SimpleNamespace(user_data=user_data if user_data is not None else {}, bot_data={"chat_id": 1})


@pytest.fixture
def sheet(monkeypatch):
    """Stub the sheet read and capture safe_write_row calls."""
    state = {"rows": [_row()], "writes": []}
    monkeypatch.setattr(tb, "_read_product_rows", lambda: (COL, "svc", "Product Tracker", 4, state["rows"]))
    monkeypatch.setattr(
        tb, "safe_write_row",
        lambda service, sheet_name, row_num, pairs: state["writes"].append((row_num, pairs)),
    )
    return state


def _screen(query):
    args, kwargs = query.edit_message_text.call_args
    return args[0], kwargs["reply_markup"]


# ── button placement ─────────────────────────────────────────────────────────

def test_mark_listed_button_only_for_ready():
    assert "listed:start:7" in _buttons(tb._search_action_kb(7, "READY"))
    for status in ("SCORED", "ACTIVE", "APPROVED", "PAUSED_OOS", ""):
        assert "listed:start:7" not in _buttons(tb._search_action_kb(7, status))


# ── input validation ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    (URL, URL),
    ("  " + URL + " ", URL),
    ("123456789012", "https://www.ebay.com/itm/123456789012"),
    ("  123456789012\n", "https://www.ebay.com/itm/123456789012"),
    ("123456789", "https://www.ebay.com/itm/123456789"),
    ("12345678901234", "https://www.ebay.com/itm/12345678901234"),
    ("123456789012345", None),
    ("12345678", None),
    ("http://ebay.co.uk/itm/1", "http://ebay.co.uk/itm/1"),
    ("=HYPERLINK(\"http://evil\")", None),
    ("https://evil.com/ebay.com", None),
    ("12345", None),
    ("hello", None),
    ("", None),
])
def test_parse_listing_input(text, expected):
    assert tb._parse_listing_input(text) == expected


# ── sheet write + confirmation ───────────────────────────────────────────────

def test_confirm_with_url_writes_status_and_url_then_shows_home(sheet):
    update, query = _query_update()
    ctx = _ctx({"pending_listed": {"row_num": 4, "title": "Gold Bar", "value": URL}})

    asyncio.run(tb.cb_listed_confirm(update, ctx, "4"))

    assert sheet["writes"] == [(4, [("A", "ACTIVE"), ("E", "eBay"), ("Q", URL)])]
    text, markup = _screen(query)
    assert "Marked ACTIVE — monitoring" in text
    assert "menu:root" in _buttons(markup)
    assert "pending_listed" not in ctx.user_data


def test_typed_bare_item_id_reaches_sheet_as_full_url(sheet):
    update, msg = _text_update("123456789012")
    ctx = _ctx({"awaiting_listing": {"row_num": 4, "title": "Gold Bar"}})
    asyncio.run(tb.on_text(update, ctx))
    assert URL in msg.reply_text.call_args.args[0]  # confirm screen shows the URL

    cb_update, _ = _query_update()
    asyncio.run(tb.cb_listed_confirm(cb_update, ctx, "4"))
    assert sheet["writes"] == [(4, [("A", "ACTIVE"), ("E", "eBay"), ("Q", URL)])]


def test_skip_then_confirm_writes_status_only(sheet):
    update, query = _query_update()
    ctx = _ctx({"awaiting_listing": {"row_num": 4, "title": "Gold Bar"}})

    asyncio.run(tb.cb_listed_skip(update, ctx, "4"))
    assert sheet["writes"] == []  # nothing written until Confirm
    assert "listed:confirm:4" in _buttons(_screen(query)[1])

    asyncio.run(tb.cb_listed_confirm(update, ctx, "4"))
    assert sheet["writes"] == [(4, [("A", "ACTIVE"), ("E", "eBay")])]  # platform set even with no ID
    assert "Marked ACTIVE — monitoring" in _screen(query)[0]


def test_write_never_touches_a_formula_column(sheet):
    update, _ = _query_update()
    ctx = _ctx({"pending_listed": {"row_num": 4, "title": "Gold Bar", "value": URL}})
    asyncio.run(tb.cb_listed_confirm(update, ctx, "4"))
    cols = {col.upper() for _, pairs in sheet["writes"] for col, _ in pairs}
    assert cols and not (cols & PROTECTED_COLS)


def test_double_tap_confirm_writes_once(sheet):
    update, query = _query_update()
    ctx = _ctx({"pending_listed": {"row_num": 4, "title": "Gold Bar", "value": ""}})
    asyncio.run(tb.cb_listed_confirm(update, ctx, "4"))
    asyncio.run(tb.cb_listed_confirm(update, ctx, "4"))
    assert len(sheet["writes"]) == 1
    assert "expired" in _screen(query)[0]


# ── stale-row guards ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("rows", [
    [_row(status="ACTIVE")],          # already moved
    [_row(title="Different Item")],   # row shifted under us
    [],                               # row deleted
])
def test_confirm_refuses_when_row_changed(sheet, rows):
    sheet["rows"] = rows
    update, query = _query_update()
    ctx = _ctx({"pending_listed": {"row_num": 4, "title": "Gold Bar", "value": URL}})
    asyncio.run(tb.cb_listed_confirm(update, ctx, "4"))
    assert sheet["writes"] == []
    assert "nothing was written" in _screen(query)[0]


def test_start_refuses_non_ready_row(sheet):
    sheet["rows"] = [_row(status="SCORED")]
    update, query = _query_update()
    ctx = _ctx()
    asyncio.run(tb.cb_listed_start(update, ctx, "4"))
    assert "awaiting_listing" not in ctx.user_data
    assert "no longer READY" in _screen(query)[0]


# ── prompt + free-text routing ───────────────────────────────────────────────

def test_start_prompts_and_arms_text_capture(sheet):
    update, query = _query_update()
    ctx = _ctx({"awaiting_search": True})
    asyncio.run(tb.cb_listed_start(update, ctx, "4"))
    assert ctx.user_data["awaiting_listing"] == {"row_num": 4, "title": "Gold Bar"}
    assert ctx.user_data["awaiting_search"] is False
    assert {"listed:skip:4", "listed:cancel:4"} <= set(_buttons(_screen(query)[1]))


def test_on_text_valid_url_shows_confirm_without_writing(sheet):
    update, msg = _text_update(URL)
    ctx = _ctx({"awaiting_listing": {"row_num": 4, "title": "Gold Bar"}})
    asyncio.run(tb.on_text(update, ctx))
    assert sheet["writes"] == []
    assert ctx.user_data["pending_listed"] == {"row_num": 4, "title": "Gold Bar", "value": URL}
    assert "awaiting_listing" not in ctx.user_data
    text = msg.reply_text.call_args.args[0]
    assert "READY → ACTIVE" in text and URL in text
    assert "listed:confirm:4" in _buttons(msg.reply_text.call_args.kwargs["reply_markup"])


def test_on_text_invalid_input_reprompts_and_keeps_waiting(sheet):
    update, msg = _text_update("not an id")
    ctx = _ctx({"awaiting_listing": {"row_num": 4, "title": "Gold Bar"}})
    asyncio.run(tb.on_text(update, ctx))
    assert sheet["writes"] == []
    assert ctx.user_data["awaiting_listing"]["row_num"] == 4
    assert "listed:skip:4" in _buttons(msg.reply_text.call_args.kwargs["reply_markup"])


def test_cancel_clears_state_and_returns_home(sheet):
    update, query = _query_update()
    ctx = _ctx({"awaiting_listing": {"row_num": 4, "title": "x"}, "pending_listed": {"row_num": 4}})
    asyncio.run(tb.cb_listed_cancel(update, ctx, "4"))
    assert "awaiting_listing" not in ctx.user_data and "pending_listed" not in ctx.user_data
    assert "menu:root" in _buttons(_screen(query)[1])


def test_home_menu_clears_listing_state():
    update, query = _query_update()
    ctx = _ctx({"awaiting_listing": {"row_num": 4, "title": "x"}, "pending_listed": {"row_num": 4}})
    asyncio.run(tb.cb_menu_root(update, ctx, None))
    assert "awaiting_listing" not in ctx.user_data and "pending_listed" not in ctx.user_data


def test_routes_registered():
    for action in ("start", "skip", "confirm", "cancel"):
        assert ("listed", action) in tb._CALLBACK_ROUTES
