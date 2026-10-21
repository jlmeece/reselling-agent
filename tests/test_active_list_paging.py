"""Active Listings screen: every live listing (any sheet status) + 30-per-page pagination."""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agents.telegram_bot as tb

COL = {"status": "A", "title": "C", "category": "D", "costco_cost": "G", "ebay_price": "H",
       "net_profit": "I", "net_margin": "J", "ebay_listing_url": "Q", "units_sold": "U",
       "sku": "AA", "actual_net": "BC"}
_EMPTY_LOG = {"sales": 0}


def _row(status, title, net, item_id):
    row = [""] * 55
    row[0], row[2], row[7], row[8] = status, title, "50.00", net
    row[16] = f"https://www.ebay.com/itm/{item_id}" if item_id else ""
    return row


def _show(monkeypatch, rows, arg=None):
    monkeypatch.setattr(tb, "_read_product_rows", lambda: (COL, "svc", "Tab", 4, rows))
    monkeypatch.setattr(tb, "_load_sales_summary", lambda: _EMPTY_LOG)
    q = SimpleNamespace(edit_message_text=AsyncMock())
    update = SimpleNamespace(callback_query=q, message=None)
    asyncio.run(tb.cb_activelist_show(update, SimpleNamespace(user_data={}, bot_data={}), arg))
    text = q.edit_message_text.call_args.args[0]
    kb = q.edit_message_text.call_args.kwargs.get("reply_markup")
    return text, kb


def _buttons(kb):
    return [b for r in kb.inline_keyboard for b in r]


def _picks(kb):
    return [b for b in _buttons(kb) if b.callback_data.startswith("activelist:pick:")]


def _many(n):
    # net desc order == index order: item i has net (1000 - i)
    return [_row("ACTIVE", f"Item {i:02d}", f"${1000 - i:.2f}", f"1{i:011d}") for i in range(1, n + 1)]


def test_live_non_active_rows_appear_and_not_live_stay_out(monkeypatch):
    rows = [_row("ACTIVE", "Active one", "$5.00", "100000000001"),
            _row("PAUSED_MARGIN", "Paused margin", "$4.00", "100000000002"),
            _row("AUDIT_REVIEW", "Audit review", "$3.00", "100000000003"),
            _row("ENDED", "Ended", "$2.00", "100000000004"),
            _row("PAUSED_OOS", "Paused oos", "$1.00", "100000000005"),
            _row("ACTIVE", "No col Q", "$9.00", "")]
    text, kb = _show(monkeypatch, rows)
    titles = [b.text for b in _picks(kb)]
    assert titles == ["1. Active one", "2. Paused margin", "3. Audit review"]
    assert "🟢 Active Listings (3)" in text and "Page " not in text      # one page = no page line
    datas = [b.callback_data for b in _buttons(kb)]
    assert not any(d.startswith("activelist:show:2") for d in datas)
    assert "activelist:show:1" in datas                                  # Refresh = current page


def test_page_one_shows_30_with_next_only(monkeypatch):
    text, kb = _show(monkeypatch, _many(45))
    picks = _picks(kb)
    assert len(picks) == 30
    assert picks[0].text.startswith("1. Item 01") and picks[-1].text.startswith("30. Item 30")
    datas = [b.callback_data for b in _buttons(kb)]
    assert "activelist:show:2" in datas and "activelist:show:0" not in datas
    assert not any(b.text.startswith("⬅️") for b in _buttons(kb))
    assert "🟢 Active Listings (45)" in text and "Page 1 of 2" in text   # summary = full set


def test_page_two_numbers_globally_with_prev_only(monkeypatch):
    text, kb = _show(monkeypatch, _many(45), "2")
    picks = _picks(kb)
    assert len(picks) == 15
    assert picks[0].text.startswith("31. Item 31") and picks[-1].text.startswith("45. Item 45")
    datas = [b.callback_data for b in _buttons(kb)]
    assert "activelist:show:1" in datas and "activelist:show:3" not in datas
    assert any(b.text.startswith("⬅️") for b in _buttons(kb))
    assert not any("➡️" in b.text for b in _buttons(kb))
    assert "🟢 Active Listings (45)" in text and "Page 2 of 2" in text
    refresh = [b for b in _buttons(kb) if b.text == "🔄 Refresh"][0]
    assert refresh.callback_data == "activelist:show:2"


@pytest.mark.parametrize("arg,page", [(None, 1), ("", 1), ("abc", 1), ("0", 1), ("-3", 1),
                                      ("99", 2), ("2", 2)])
def test_page_arg_clamps(monkeypatch, arg, page):
    text, kb = _show(monkeypatch, _many(45), arg)
    assert f"Page {page} of 2" in text
    assert _picks(kb)[0].text.startswith(f"{(page - 1) * 30 + 1}. ")


def test_empty_message_only_when_no_live_listings(monkeypatch):
    text, _ = _show(monkeypatch, [_row("ENDED", "Gone", "$1.00", "100000000004")])
    assert text == "No active listings right now."


# ── detail card: live views / watchers ──────────────────────────────────────

def _pick(monkeypatch, live):
    import yaml
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "config", "col_map.yaml")) as f:
        col = yaml.safe_load(f)["columns"]
    row = [""] * 60
    row[tb.col_to_idx(col["status"])] = "AUDIT_REVIEW"
    row[tb.col_to_idx(col["title"])] = "Energy Shot"
    row[tb.col_to_idx(col["ebay_listing_url"])] = "https://www.ebay.com/itm/123456789012"
    monkeypatch.setattr(tb, "_read_product_rows", lambda: (col, "svc", "Tab", 4, [row]))
    monkeypatch.setattr(tb, "get_pending", lambda item_id: None)
    calls = []

    def fetch(item_id):
        calls.append(item_id)
        if isinstance(live, Exception):
            raise live
        return live
    monkeypatch.setattr(tb, "fetch_item_price", fetch)
    q = SimpleNamespace(edit_message_text=AsyncMock())
    asyncio.run(tb.cb_activelist_pick(SimpleNamespace(callback_query=q, message=None),
                                      SimpleNamespace(user_data={}, bot_data={}), "4"))
    return q.edit_message_text.call_args.args[0], calls


def test_card_shows_live_views_and_watchers(monkeypatch):
    text, calls = _pick(monkeypatch, {"ok": True, "view_count": 214, "watch_count": 3})
    assert calls == ["123456789012"]
    assert "👁️ 214 views · ⭐ 3 watchers" in text
    assert "Net: " in text and "reserve" not in text.lower()


def test_card_shows_watchers_alone_when_views_unknown(monkeypatch):
    text, _ = _pick(monkeypatch, {"ok": True, "view_count": None, "watch_count": 0})
    assert "⭐ 0 watchers" in text and "views" not in text


@pytest.mark.parametrize("live", [{"ok": False, "view_count": None, "watch_count": None},
                                  {}, RuntimeError("eBay down")])
def test_card_omits_engagement_when_fetch_fails(monkeypatch, live):
    text, _ = _pick(monkeypatch, live)
    assert "👁️" not in text and "⭐" not in text and "Energy Shot" in text


def test_card_shows_cached_30d_views_before_watchers(monkeypatch):
    from tools import ebay_analytics as ea
    ea._write_json(ea.VIEWS_PATH, {"fetched_at": "2026-10-21T00:00:00+00:00", "days": 30,
                                   "views": {"123456789012": 57}})
    text, _ = _pick(monkeypatch, {"ok": True, "view_count": None, "watch_count": 2})
    assert "👁️ 57 views (30d) · ⭐ 2 watchers" in text


def test_card_without_view_cache_shows_watchers_alone(monkeypatch):
    text, _ = _pick(monkeypatch, {"ok": True, "view_count": None, "watch_count": 2})
    assert "⭐ 2 watchers" in text and "👁️" not in text
