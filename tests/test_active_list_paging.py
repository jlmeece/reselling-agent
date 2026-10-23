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


# ── detail card Prev / Next ─────────────────────────────────────────────────

def _nav(kb):
    return [(b.text, b.callback_data) for r in kb.inline_keyboard for b in r
            if b.text in ("⬅️ Prev", "Next ➡️")]


def test_action_kb_prev_next_only_when_given():
    assert _nav(tb._active_action_kb(5)) == []
    assert len(tb._active_action_kb(5).inline_keyboard) == len(tb._active_action_kb(5, prev_row_num=None).inline_keyboard)
    assert _nav(tb._active_action_kb(5, prev_row_num=4, next_row_num=9)) == [
        ("⬅️ Prev", "activelist:pick:4"), ("Next ➡️", "activelist:pick:9")]
    assert _nav(tb._active_action_kb(5, next_row_num=9)) == [("Next ➡️", "activelist:pick:9")]


def _pick_nav(monkeypatch, rows, row_num, arg=None, user_data=None, full=False):
    import yaml
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "config", "col_map.yaml")) as f:
        col = yaml.safe_load(f)["columns"]
    sheet_rows = []
    for status, title, net, item_id in rows:
        r = [""] * 60
        r[tb.col_to_idx(col["status"])] = status
        r[tb.col_to_idx(col["title"])] = title
        r[tb.col_to_idx(col["net_profit"])] = net
        r[tb.col_to_idx(col["ebay_listing_url"])] = f"https://www.ebay.com/itm/{item_id}" if item_id else ""
        sheet_rows.append(r)
    monkeypatch.setattr(tb, "_read_product_rows", lambda: (col, "svc", "Tab", 4, sheet_rows))
    monkeypatch.setattr(tb, "get_pending", lambda item_id: None)
    monkeypatch.setattr(tb, "fetch_item_price", lambda item_id: {})
    q = SimpleNamespace(edit_message_text=AsyncMock())
    asyncio.run(tb.cb_activelist_pick(SimpleNamespace(callback_query=q, message=None),
                                      SimpleNamespace(user_data=user_data or {}, bot_data={}),
                                      arg or str(row_num)))
    kb = q.edit_message_text.call_args.kwargs["reply_markup"]
    return kb if full else _nav(kb)


_THREE = [("PAUSED_MARGIN", "Mid", "$5.00", "100000000002"),      # row 4
          ("ACTIVE", "Top", "$9.00", "100000000001"),             # row 5
          ("AUDIT_REVIEW", "Low", "$1.00", "100000000003"),       # row 6
          ("ENDED", "Gone", "$7.00", "100000000004")]             # row 7 (not live)


def test_pick_middle_item_has_prev_and_next(monkeypatch):
    assert _pick_nav(monkeypatch, _THREE, 4) == [("⬅️ Prev", "activelist:pick:5"),
                                                ("Next ➡️", "activelist:pick:6")]


def test_pick_first_item_has_next_only(monkeypatch):
    assert _pick_nav(monkeypatch, _THREE, 5) == [("Next ➡️", "activelist:pick:4")]


def test_pick_last_item_has_prev_only(monkeypatch):
    assert _pick_nav(monkeypatch, _THREE, 6) == [("⬅️ Prev", "activelist:pick:4")]


def test_pick_not_live_or_lone_item_has_no_nav(monkeypatch):
    assert _pick_nav(monkeypatch, _THREE, 7) == []
    assert _pick_nav(monkeypatch, [("ACTIVE", "Only", "$3.00", "100000000001")], 4) == []


# ── 30-day views inline + sort toggle ────────────────────────────────────────

def _cache_views(views):
    from tools import ebay_analytics as ea
    ea._write_json(ea.VIEWS_PATH, {"fetched_at": "2026-10-21T00:00:00+00:00", "days": 30,
                                   "views": views})


_MIXED = [_row("ACTIVE", "High net", "$9.00", "100000000001"),     # 5 views
          _row("ACTIVE", "No views", "$8.00", "100000000002"),     # not cached
          _row("ACTIVE", "Most viewed", "$1.00", "100000000003"),  # 80 views
          _row("ACTIVE", "Also uncached", "$7.00", "100000000004")]


def test_views_sort_puts_most_viewed_first_and_unknown_last(monkeypatch):
    _cache_views({"100000000001": 5, "100000000003": 80})
    text, kb = _show(monkeypatch, _MIXED, "1:views")
    assert [b.text for b in _picks(kb)] == ["1. Most viewed · 👁️ 80", "2. High net · 👁️ 5",
                                            "3. No views", "4. Also uncached"]   # unknown: net tie-break
    assert "Sorted by views" in text


def test_net_sort_unchanged_and_shows_views_inline(monkeypatch):
    _cache_views({"100000000001": 5, "100000000003": 80})
    _text, kb = _show(monkeypatch, _MIXED)
    assert [b.text for b in _picks(kb)] == ["1. High net · 👁️ 5", "2. No views",
                                            "3. Also uncached", "4. Most viewed · 👁️ 80"]


def test_no_cache_keeps_plain_labels(monkeypatch):
    _text, kb = _show(monkeypatch, _MIXED, "1:views")
    assert [b.text for b in _picks(kb)] == ["1. High net", "2. No views", "3. Also uncached",
                                            "4. Most viewed"]                     # all unknown -> net


def test_sort_toggle_flips_label_and_callback(monkeypatch):
    _text, kb = _show(monkeypatch, _MIXED)
    toggle = [b for b in _buttons(kb) if b.text.startswith("↕️")]
    assert [(b.text, b.callback_data) for b in toggle] == [("↕️ Sort: Views", "activelist:show:1:views")]
    _text, kb = _show(monkeypatch, _MIXED, "1:views")
    toggle = [b for b in _buttons(kb) if b.text.startswith("↕️")]
    assert [(b.text, b.callback_data) for b in toggle] == [("↕️ Sort: Net", "activelist:show:1:net")]
    refresh = [b for b in _buttons(kb) if b.text == "🔄 Refresh"][0]
    assert refresh.callback_data == "activelist:show:1:views"      # Refresh keeps the sort


def test_views_sort_survives_paging(monkeypatch):
    _text, kb = _show(monkeypatch, _many(45), "2:views")
    datas = [b.callback_data for b in _buttons(kb)]
    assert "activelist:show:1:views" in datas and "activelist:show:2:net" in datas


@pytest.mark.parametrize("arg,expected", [(None, (None, "net")), ("", (None, "net")),
                                          ("2", (2, "net")), ("2:views", (2, "views")),
                                          ("2:net", (2, "net")), ("2:junk", (2, "net")),
                                          ("abc:views", (None, "views")),
                                          ("views", (None, "views")), ("net", (None, "net"))])
def test_parse_activelist_arg_back_compat(arg, expected):
    assert tb._parse_activelist_arg(arg) == expected


def test_format_active_line_appends_views_only_when_given():
    p = {"title": "Shot", "ebay_price": "$10.00", "net_profit": "$2.00", "net_margin": "20%",
         "units_sold": "1"}
    assert "👁️" not in tb._format_active_line(p)
    assert tb._format_active_line(p, views=12).endswith("📦 sold 1 · 👁️ 12")
    assert tb._format_active_line(p, views=0).endswith(" · 👁️ 0")


# ── sort survives list -> card -> Prev/Next -> back ─────────────────────────

# net order: Top(5) > Mid(4) > Low(6); views order: Low 80 > Top 5 > Mid (uncached)
_SORT_VIEWS = {"100000000003": 80, "100000000001": 5}


def test_views_list_item_buttons_carry_sort_and_remember_it(monkeypatch):
    _cache_views({"100000000001": 5, "100000000003": 80})
    monkeypatch.setattr(tb, "_read_product_rows", lambda: (COL, "svc", "Tab", 4, _MIXED))
    monkeypatch.setattr(tb, "_load_sales_summary", lambda: _EMPTY_LOG)
    q = SimpleNamespace(edit_message_text=AsyncMock())
    ctx = SimpleNamespace(user_data={}, bot_data={})
    asyncio.run(tb.cb_activelist_show(SimpleNamespace(callback_query=q, message=None), ctx, "1:views"))
    picks = _picks(q.edit_message_text.call_args.kwargs["reply_markup"])
    assert all(b.callback_data.endswith(":views") for b in picks)
    assert ctx.user_data["activelist_sort"] == "views"


def test_net_list_item_buttons_stay_plain(monkeypatch):
    _text, kb = _show(monkeypatch, _MIXED)
    assert all(b.callback_data.count(":") == 2 for b in _picks(kb))


def test_pick_with_views_suffix_navigates_in_views_order(monkeypatch):
    _cache_views(_SORT_VIEWS)
    # views order Low(6) -> Top(5) -> Mid(4): Top's neighbours are Low and Mid
    assert _pick_nav(monkeypatch, _THREE, 5, arg="5:views") == [
        ("⬅️ Prev", "activelist:pick:6:views"), ("Next ➡️", "activelist:pick:4:views")]


def test_pick_without_suffix_uses_remembered_sort(monkeypatch):
    _cache_views(_SORT_VIEWS)
    assert _pick_nav(monkeypatch, _THREE, 5, user_data={"activelist_sort": "views"}) == [
        ("⬅️ Prev", "activelist:pick:6:views"), ("Next ➡️", "activelist:pick:4:views")]


def test_explicit_net_suffix_overrides_remembered_views(monkeypatch):
    _cache_views(_SORT_VIEWS)
    assert _pick_nav(monkeypatch, _THREE, 5, arg="5:net",
                     user_data={"activelist_sort": "views"}) == [("Next ➡️", "activelist:pick:4")]


def test_card_back_button_returns_to_same_sort(monkeypatch):
    _cache_views(_SORT_VIEWS)
    kb = _pick_nav(monkeypatch, _THREE, 5, arg="5:views", full=True)
    datas = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "activelist:show:views" in datas
    _text, kb2 = _show(monkeypatch, _MIXED, "views")        # what that button routes to
    assert "Sorted by views" in _text
    kb = _pick_nav(monkeypatch, _THREE, 5, full=True)
    assert "activelist:show" in [b.callback_data for r in kb.inline_keyboard for b in r]


def test_stepper_order_views_sort():
    col = {"status": "A", "title": "C", "net_profit": "I", "ebay_listing_url": "Q"}
    rows = [_row("ACTIVE", "Top", "$9.00", "100000000001"),
            _row("ACTIVE", "Mid", "$5.00", "100000000002"),
            _row("ACTIVE", "Low", "$1.00", "100000000003")]
    _cache_views(_SORT_VIEWS)
    sheet = (col, "svc", "Tab", 4, rows)
    assert [t for _r, _i, t in tb._active_stepper_order(sheet)] == ["Top", "Mid", "Low"]
    assert [t for _r, _i, t in tb._active_stepper_order(sheet, sort="views")] == ["Low", "Top", "Mid"]
