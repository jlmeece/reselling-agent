"""tools/sale_schedule.py — sale-end store, pre-stage eligibility (24h window), action store.
State files are redirected to tmp by the autouse fixture in conftest.py."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import sale_schedule as sched
from tools.reprice import restore_margin_price

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
END = "2026-10-19T06:59:00+00:00"            # Energy Shot's real promotionEndDate (18h59m after NOW)
URL_P = "https://www.costco.com/p/-/kirkland-signature-energy-shot-48-bottles-2-ounces-each/4000099948"
URL_OLD = "https://www.costco.com/kirkland-energy.product.4000099948.html"
EBAY = "https://www.ebay.com/itm/123456789012"

COL = {"status": "A", "title": "C", "category": "D", "costco_cost": "G", "ebay_price": "H",
       "ebay_listing_url": "Q", "costco_url": "R", "sku": "AA", "fee_rate": "AB", "ship_cost": "AD",
       "regular_price": "AW"}
CATS = {"Pharmacy": {"fee_rate": 0.1325, "ad_rate": 0.0}}


def _row(status="ACTIVE", url=URL_P, ebay=EBAY, G="31.99", H="41.48", AW="39.99", fee="13.25%"):
    vals = {"A": status, "C": "Energy Shot", "D": "Pharmacy", "G": G, "H": H, "Q": ebay, "R": url,
            "AA": "4000099948", "AB": fee, "AD": "0", "AW": AW}
    row = [""] * 49
    for col, v in vals.items():
        row[sched._col_idx(col)] = v
    return row


def _ends(end=END):
    return {"4000099948": {"end_ts": end, "regular_price": 39.99}}


# ── helpers ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,pid", [
    (URL_P, "4000099948"), (URL_OLD, "4000099948"),
    ("https://www.costco.com/p/-/slug/11487038?x=1", "11487038"), ("", None), ("https://x.com/a", None),
])
def test_costco_product_id_both_url_shapes(url, pid):
    assert sched.costco_product_id(url) == pid


def test_format_end_names_pacific_and_central():
    assert sched.format_end(END) == "Sun 10/18 11:59 PM PT (Mon 1:59 AM CT)"


def test_parse_ts_handles_z_and_naive():
    assert sched.parse_ts("2026-10-19T06:59:00Z") == datetime(2026, 10, 19, 6, 59, tzinfo=timezone.utc)
    assert sched.parse_ts("2026-10-19T06:59:00").tzinfo is not None
    assert sched.parse_ts("garbage") is None and sched.parse_ts(None) is None


# ── sale-end store ───────────────────────────────────────────────────────────

def test_record_and_clear_sale_end():
    assert sched.record_sale_end("4000099948", "2026-10-19T06:59:00Z", sale_price=31.99,
                                 regular_price=39.99, title="Energy Shot") is True
    got = sched.get_sale_end("4000099948")
    assert got["end_ts"] == END and got["regular_price"] == 39.99
    assert sched.record_sale_end("4000099948", END) is False        # same end -> unchanged
    assert sched.clear_sale_end("4000099948") is True
    assert sched.get_sale_end("4000099948") is None
    assert sched.clear_sale_end("4000099948") is False


def _scrape(price=31.99, on_sale=True, end=END, original=39.99):
    return {"price": price, "on_sale": on_sale, "sale_end_ts": end if on_sale else None,
            "original_price": original if on_sale else None}


def test_update_from_scrape_records_moves_and_clears():
    assert sched.update_sale_end_from_scrape(URL_P, _scrape()) == "recorded"
    assert sched.update_sale_end_from_scrape(URL_P, _scrape()) is None            # unchanged
    later = "2026-10-26T06:59:00+00:00"
    assert sched.update_sale_end_from_scrape(URL_P, _scrape(end=later)) == "moved"  # extended
    assert sched.get_sale_end("4000099948")["end_ts"] == later
    assert sched.update_sale_end_from_scrape(URL_P, {"price": None}) is None        # miss: no change
    assert sched.get_sale_end("4000099948") is not None
    assert sched.update_sale_end_from_scrape(URL_P, _scrape(39.99, on_sale=False)) == "cleared"
    assert sched.get_sale_end("4000099948") is None


def test_writes_are_atomic_json():
    sched.record_sale_end("1", END)
    with open(sched.SALE_END_PATH, encoding="utf-8") as f:
        assert json.load(f)["1"]["end_ts"] == END
    assert not [p for p in os.listdir(os.path.dirname(sched.SALE_END_PATH)) if p.endswith(".tmp")]


# ── pre-stage eligibility ────────────────────────────────────────────────────

def _cands(rows=None, ends=None, store=None, now=NOW):
    return sched.prestage_candidates(rows or [_row()], COL, CATS, _ends() if ends is None else ends,
                                     store or {"actions": {}, "prompted": {}}, now=now, start_row=4)


def test_inside_24h_window_gets_one_prompt_with_target():
    (c,) = _cands()
    assert c["item_id"] == "123456789012" and c["row"] == 4 and c["costco_pid"] == "4000099948"
    assert c["sale_end_ts"] == END
    assert (c["old_cost"], c["new_cost"], c["ebay_price"]) == (31.99, 39.99, 41.48)
    assert c["target"] == restore_margin_price(31.99, 39.99, 41.48, 0.1325, 0.0) == 50.99


def test_more_than_24h_out_is_not_yet():
    assert _cands(now=NOW - timedelta(hours=6)) == []            # 24h59m left
    assert len(_cands(now=NOW - timedelta(hours=4))) == 1        # 22h59m left


def test_first_seen_with_less_than_24h_left_prompts_at_once():
    assert len(_cands(now=datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc))) == 1   # 59 min left


def test_past_end_is_never_prestaged():
    assert _cands(now=datetime(2026, 10, 19, 7, 0, tzinfo=timezone.utc)) == []


def test_already_prompted_for_this_end_but_not_for_a_new_end():
    store = {"actions": {}, "prompted": {"123456789012": {"sale_end_ts": END}}}
    assert _cands(store=store) == []
    later = "2026-10-19T08:00:00+00:00"                            # end moved -> prompt again
    assert len(_cands(store=store, ends=_ends(later))) == 1


def test_pending_action_blocks_prompt():
    assert _cands(store={"actions": {"123456789012": {}}, "prompted": {}}) == []


@pytest.mark.parametrize("row", [
    _row(status="READY"), _row(ebay=""), _row(ebay="https://example.com/x"), _row(url=""),
    _row(AW=""),                       # no regular price anywhere -> can't compute
    _row(H="79.99"),                   # eBay price already restores the margin
])
def test_ineligible_rows(row):
    ends = _ends()
    if row[sched._col_idx("AW")] == "":
        ends = {"4000099948": {"end_ts": END}}                    # no stored regular either
    assert _cands(rows=[row], ends=ends) == []


def test_store_regular_price_fills_blank_aw():
    (c,) = _cands(rows=[_row(AW="")])
    assert c["new_cost"] == 39.99


def test_fee_falls_back_to_category():
    (c,) = _cands(rows=[_row(fee="")])
    assert c["fee_rate"] == 0.1325


# ── action store ─────────────────────────────────────────────────────────────

PROMPT = {"item_id": "123456789012", "title": "Energy Shot", "sku": "4000099948", "target": 50.99,
          "sale_end_ts": END, "costco_pid": "4000099948", "costco_url": URL_P,
          "old_cost": 31.99, "new_cost": 39.99, "ebay_price": 41.48}


def test_schedule_due_cancel_cycle():
    sched.record_prompt(PROMPT)
    assert sched.get_prompt("123456789012")["target"] == 50.99
    a = sched.schedule_action("123456789012", "reprice", PROMPT, now=NOW)
    assert (a["action"], a["target_price"], a["apply_at"]) == ("reprice", 50.99, END)
    assert sched.due_actions(NOW) == []
    assert [d["item_id"] for d in sched.due_actions(datetime(2026, 10, 19, 7, 0, tzinfo=timezone.utc))] \
        == ["123456789012"]
    assert sched.cancel_action("123456789012")["action"] == "reprice"
    assert sched.cancel_action("123456789012") is None


def test_end_action_has_no_price_and_unknown_action_rejected():
    assert sched.schedule_action("1", "end", PROMPT)["target_price"] is None
    with pytest.raises(ValueError):
        sched.schedule_action("1", "delete", PROMPT)


def test_reschedule_and_last_error():
    sched.schedule_action("123456789012", "reprice", PROMPT)
    later = datetime(2026, 10, 26, 6, 59, tzinfo=timezone.utc)
    sched.reschedule("123456789012", later)
    assert sched.get_action("123456789012")["apply_at"] == later.isoformat()
    assert sched.set_last_error("123456789012", "auth 932") is True
    assert sched.set_last_error("123456789012", "auth 932") is False       # same -> no re-alert
    assert sched.set_last_error("123456789012", "network") is True


def test_mark_applied_history_prune_and_recently_applied():
    sched.schedule_action("123456789012", "reprice", PROMPT)
    sched.record_prompt(PROMPT)
    old = {"item_id": "999", "action": "reprice", "result": {"ok": True},
           "applied_at": (NOW - timedelta(days=40)).isoformat()}
    store = sched.load_store()
    store["applied"].append(old)
    sched._save(sched.ACTIONS_PATH, store)

    sched.mark_applied("123456789012", {"ok": True}, now=NOW)
    s = sched.load_store()
    assert "123456789012" not in s["actions"] and "123456789012" not in s["prompted"]
    assert [h["item_id"] for h in s["applied"]] == ["123456789012"]          # 40-day-old pruned
    assert sched.recently_applied("123456789012", now=NOW + timedelta(days=1)) is True
    assert sched.recently_applied("123456789012", now=NOW + timedelta(days=4)) is False
    assert sched.recently_applied("123456789012", action="end", now=NOW) is False


def test_failed_apply_does_not_count_as_recent():
    sched.schedule_action("1", "reprice", PROMPT)
    sched.mark_applied("1", {"ok": False}, now=NOW)
    assert sched.recently_applied("1", now=NOW) is False


def test_fast_forward_only_when_pending_and_read_only_otherwise():
    assert sched.fast_forward("123456789012", now=NOW) is False
    assert not os.path.exists(sched.ACTIONS_PATH)                           # nothing written
    sched.schedule_action("123456789012", "end", PROMPT)
    assert sched.fast_forward("123456789012", now=NOW) is True
    a = sched.get_action("123456789012")
    assert a["apply_at"] == NOW.isoformat() and a["fast_forwarded"] is True
    assert sched.due_actions(NOW)
