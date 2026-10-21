"""tools/restore_blank_net_removals.py — plan/dedupe logic only (no Sheets calls)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.restore_blank_net_removals import BUG_REASON, plan_restore, row_dict

COL = {"status": "A", "title": "C", "category": "D", "costco_url": "R", "sku": "AA"}


def _gy(title, reason=BUG_REASON, url="", sku="", category="Pharmacy"):
    return {"title": title, "reason": reason, "costco_url": url, "sku": sku, "category": category,
            "date_removed": "2026-10-19"}


def _tracker(title, url=""):
    row = [""] * 27
    row[2], row[17] = title, url
    return row


def test_only_exact_bug_reason_matches():
    gy = [_gy("A"), _gy("B", reason="Below floor ($1.00 min) — net $0.40"),
          _gy("C", reason="Negative net profit ($-5.00)")]
    matched, to_add, _ = plan_restore(gy, [], COL)
    assert [g["title"] for g in matched] == ["A"] and [g["title"] for g in to_add] == ["A"]


def test_skips_present_by_title_case_insensitive_and_by_product_id():
    gy = [_gy("14kt Yellow Gold Serpentine Chain Bracelet"),
          _gy("Renamed Widget", url="https://www.costco.com/.product.100404264.html"),
          _gy("New Vitamin")]
    tracker = [_tracker("14KT yellow gold serpentine chain bracelet"),
               _tracker("Widget", url="https://www.costco.com/p/-/widget/100404264")]
    _, to_add, skipped = plan_restore(gy, tracker, COL)
    assert [g["title"] for g in to_add] == ["New Vitamin"]
    assert [why for _, why in skipped] == ["already in Product Tracker"] * 2


def test_same_product_removed_many_times_is_restored_once():
    gy = [_gy("Airborne"), _gy("airborne"), _gy("Airborne")]
    _, to_add, skipped = plan_restore(gy, [], COL)
    assert len(to_add) == 1 and len(skipped) == 2


def test_rerun_after_restore_adds_nothing():
    gy = [_gy("Airborne", url="https://www.costco.com/.product.111.html")]
    _, to_add, _ = plan_restore(gy, [], COL)
    tracker = [_tracker(g["title"], g["costco_url"]) for g in to_add]
    assert plan_restore(gy, tracker, COL)[1] == []


def test_row_dict_writes_only_a_c_d_r_aa():
    d = row_dict(COL, _gy("Airborne", url="https://www.costco.com/.product.111.html", sku="111"))
    assert d == {"A": "PENDING", "C": "Airborne", "D": "Pharmacy",
                 "R": "https://www.costco.com/.product.111.html", "AA": "111"}
