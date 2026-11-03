"""tools/migrate_audit_review.py — one-shot AUDIT_REVIEW clear-out: plan buckets + apply order."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents import auditor  # noqa: E402
from agents.scheduler import load_col_map  # noqa: E402
from tools import migrate_audit_review as M  # noqa: E402

COL = load_col_map()


def _row(status, title, ebay_url="", sku=""):
    row = [""] * 48
    for k, v in {"status": status, "title": title, "ebay_listing_url": ebay_url, "sku": sku,
                 "category": "Outdoor Furniture", "net_profit": "3.00"}.items():
        row[auditor._col_to_idx(COL[k])] = v
    return row


def _index(listings):
    ids, titles, skus = {}, {}, {}
    for l in listings:
        avail = l["quantity"] - l.get("quantity_sold", 0)
        ids[l["item_id"]] = avail
        titles[auditor._norm_title(l["title"])] = avail
        if l.get("sku"):
            skus[l["sku"]] = avail
    return ids, titles, skus


INDEX = _index([{"item_id": "111111111111", "title": "Selling Set", "quantity": 5},
                {"item_id": "222222222222", "title": "Hidden Set", "quantity": 0}])
ROWS = [_row("AUDIT_REVIEW", "Selling Set", "https://www.ebay.com/itm/111111111111"),
        _row("SCORED", "Not audited"),
        _row("AUDIT_REVIEW", "Hidden Set", "https://www.ebay.com/itm/222222222222"),
        _row("AUDIT_REVIEW", "Dead Row")]


def test_plan_buckets_only_audit_review_rows():
    p = M.plan(ROWS, COL, 4, INDEX)
    assert [r for r, _, _ in p["activate"]] == [4]
    assert [r for r, _, _ in p["hidden"]] == [6]
    assert [r for r, _, _ in p["delete"]] == [7]


def _apply(monkeypatch, hidden_status=None, moved=()):
    calls = {"writes": [], "graveyard": [], "deleted": []}
    titles = {i + 4: auditor._safe(r, auditor._col_to_idx(COL["title"])) for i, r in enumerate(ROWS)}
    monkeypatch.setattr(auditor, "_current_title",
                        lambda svc, sheet, col, row: "moved" if row in moved else titles[row])
    monkeypatch.setattr(M, "write_row_partial",
                        lambda svc, sheet, row, pairs: calls["writes"].append((row, dict(pairs))))
    monkeypatch.setattr(M, "setup_graveyard_tab", lambda svc: None)
    monkeypatch.setattr(M, "write_to_graveyard", lambda svc, rows: calls["graveyard"].extend(rows))
    monkeypatch.setattr(auditor, "_delete_rows",
                        lambda svc, sheet, col, rows: calls["deleted"].extend(r for r, _, _ in rows) or [])
    monkeypatch.setattr(M.time, "sleep", lambda s: None)
    done = M.apply("svc", "Product Tracker", COL, M.plan(ROWS, COL, 4, INDEX), hidden_status, "2026-11-03")
    return calls, done


def test_apply_activates_archives_and_deletes_but_leaves_hidden_by_default(monkeypatch):
    calls, done = _apply(monkeypatch)
    assert calls["writes"] == [(4, {COL["status"]: "ACTIVE", COL["tier_summary"]: M.ACTIVE_NOTE})]
    assert [g["title"] for g in calls["graveyard"]] == ["Dead Row"]
    assert calls["graveyard"][0]["status_at_removal"] == "AUDIT_REVIEW"
    assert calls["deleted"] == [7] and done["deleted"] == 1


def test_apply_hidden_status_sets_hidden_rows(monkeypatch):
    calls, done = _apply(monkeypatch, hidden_status="PAUSED_OOS")
    assert (6, {COL["status"]: "PAUSED_OOS",
                COL["tier_summary"]: "[audit] eBay listing hidden at qty 0 — set PAUSED_OOS"}) in calls["writes"]
    assert len(done["hidden_set"]) == 1 and len(done["activated"]) == 1


def test_apply_skips_a_row_that_moved(monkeypatch):
    calls, done = _apply(monkeypatch, moved=(4,))
    assert calls["writes"] == [] and done["skipped"][0][0] == 4
