"""Sale Radar selection + formatting (tools/sale_digest.py). Pure — no sheet / Telegram."""
from datetime import datetime

import pytest
import yaml

from tools.sale_digest import format_digest, select_sale_items

with open("config/col_map.yaml", encoding="utf-8") as _f:
    COL = yaml.safe_load(_f)["columns"]

NOW = datetime(2026, 9, 24, 12, 0)


def _idx(letter):
    n = 0
    for c in letter:
        n = n * 26 + ord(c) - 64
    return n - 1


def _row(**cells):
    row = []
    for key, val in cells.items():
        i = _idx(COL[key])
        while len(row) <= i:
            row.append("")
        row[i] = val
    return row


def _sale(title="Kirkland Signature Energy Shot", status="ACTIVE", price="31.99", regular="39.99",
          badge="🔥 -$8 ends 10/18/26", ebay="41.48", fee="0.1325", ship="0", checked="2026-09-24 10:00"):
    return _row(title=title, status=status, costco_cost=price, regular_price=regular, sale_info=badge,
                ebay_price=ebay, fee_rate=fee, ship_cost=ship, last_checked=checked)


def _pick(rows):
    return select_sale_items(rows, COL, now=NOW)


def test_spec_example_line_and_net_math():
    items, skipped = _pick([_sale()])
    assert skipped == {}
    (it,) = items
    assert it["net"] == 3.99                                       # 41.48 - 31.99 - 41.48 * 0.1325
    msg = format_digest(items, now=NOW)
    assert msg == ("🛒 <b>Sale Radar — 1 item on sale</b>\n"
                   "• Kirkland Signature Energy Shot — $31.99 (was $39.99, ends 10/18) | eBay $41.48 | net +$3.99")


@pytest.mark.parametrize("row,reason", [
    (_sale(status="AUDIT_REVIEW"), "junk_status"),
    (_sale(status=""), "junk_status"),
    (_sale(price=""), "no_price"),
    (_sale(badge="🔥 -$100 ends 7/25/26"), "expired"),
    (_sale(badge="🔥 -$100", regular="", price="14.99"), "implausible"),      # $100 off a $15 item
    (_sale(regular="31.99"), "implausible"),                                  # AW not above G
    (_sale(regular="20.00"), "implausible"),
    (_sale(badge="🔥 -$8", regular="", checked="2026-09-20 10:00"), "stale"),  # undated + 4 days old
    (_sale(checked="2026-09-10 10:00"), "stale"),                              # dated but a 2-week-old check
    (_sale(checked=""), "stale"),
])
def test_untrustworthy_rows_are_dropped_and_counted(row, reason):
    items, skipped = _pick([row])
    assert items == [] and skipped == {reason: 1}


def test_rows_without_a_badge_are_not_on_sale_and_not_counted():
    items, skipped = _pick([_sale(badge=""), [], _row(title="x")])
    assert items == [] and skipped == {}


def test_undated_badge_ok_when_checked_recently_and_sane():
    items, skipped = _pick([_sale(badge="🔥 -$8", regular="", checked="2026-09-23 12:30")])
    assert len(items) == 1 and skipped == {} and items[0]["end"] is None and items[0]["regular"] is None
    assert "was" not in format_digest(items, now=NOW) and "ends" not in format_digest(items, now=NOW)


def test_missing_ebay_price_or_fee_gives_net_na_not_a_guess():
    items, _ = _pick([_sale(ebay=""), _sale(title="B", fee="")])
    assert [i["net"] for i in items] == [None, None]
    msg = format_digest(items, now=NOW)
    assert msg.count("net n/a") == 2 and "eBay —" in msg


def test_negative_net_is_signed():
    items, _ = _pick([_sale(ebay="31.00")])
    assert items[0]["net"] < 0
    assert "net -$" in format_digest(items, now=NOW)


def test_ship_cost_reduces_net():
    items, _ = _pick([_sale(ship="2")])
    assert items[0]["net"] == 1.99


def test_sort_net_desc_then_soonest_end_then_unknown_last():
    rows = [
        _sale(title="low", ebay="41.48"),                                        # net +3.99
        _sale(title="high", ebay="60.00"),                                       # net bigger
        _sale(title="tie-late", ebay="41.48", badge="🔥 -$8 ends 11/30/26"),     # net +3.99, later end
        _sale(title="tie-early", ebay="41.48", badge="🔥 -$8 ends 9/30/26"),     # net +3.99, sooner end
        _sale(title="unknown", ebay=""),
    ]
    items, _ = _pick(rows)
    assert [i["title"] for i in items] == ["high", "tie-early", "low", "tie-late", "unknown"]


def test_top_ten_then_and_n_more():
    items, _ = _pick([_sale(title=f"Item {n}", ebay=str(41 + n)) for n in range(13)])
    lines = format_digest(items, now=NOW).splitlines()
    assert lines[0] == "🛒 <b>Sale Radar — 13 items on sale</b>"
    assert len(lines) == 1 + 10 + 1 and lines[-1] == "...and 3 more"


def test_zero_items_means_no_message():
    assert format_digest([], now=NOW) is None
    assert format_digest(_pick([_sale(badge="")])[0], now=NOW) is None


def test_html_in_titles_is_escaped_and_long_titles_truncated_before_escaping():
    nasty = "Bacon & Cheese <Combo> " + "x" * 80
    items, _ = _pick([_sale(title=nasty)])
    msg = format_digest(items, now=NOW)
    assert "&amp;" in msg and "&lt;Combo&gt;" in msg and "<Combo>" not in msg
    assert "…" in msg and "x" * 60 not in msg
    # a title cut right after an ampersand must not leave half an entity like "&am"
    cut = "a" * 38 + "&" + "b" * 20
    line = format_digest(_pick([_sale(title=cut)])[0], now=NOW).splitlines()[1]
    assert "&am " not in line and "&am…" not in line


def test_ending_soon_gets_a_clock_marker():
    soon = _sale(title="soon", badge="🔥 -$8 ends 9/25/26")     # ~36h away, inside 48h
    far = _sale(title="far", badge="🔥 -$8 ends 10/18/26")
    lines = format_digest(_pick([soon, far])[0], now=NOW, warn_hours=48).splitlines()
    assert any(l.startswith("• ⏰ soon") for l in lines)
    assert any(l.startswith("• far") for l in lines)
