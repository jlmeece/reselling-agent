"""One-shot: clear every AUDIT_REVIEW row from the Product Tracker (2026-11-03).

AUDIT_REVIEW is retired: the auditor no longer writes it and the bot's audit queue is gone,
so the rows already flagged would stay frozen forever (AUDIT_REVIEW is still PROTECTED).
Per row, against ONE eBay ActiveList fetch (agents.auditor._live_index):

  * live listing, quantity > 0   -> status ACTIVE (+ col T note); the monitor owns it again
  * live listing hidden at qty 0 -> left as is and listed, unless --hidden-status says what to
                                    set (ACTIVE would fight an OOS / sale-end hide)
  * no live listing              -> archived to the Graveyard tab, then the row is deleted
                                    (highest row first, title re-checked before each delete)

eBay unreachable = nothing is written. No Telegram; prints a summary.

    python tools/migrate_audit_review.py --dry-run          # the plan, no writes
    python tools/migrate_audit_review.py                    # do it
    python tools/migrate_audit_review.py --hidden-status PAUSED_OOS
"""
import argparse
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(encoding="utf-8", override=True)

import yaml

from agents import auditor
from agents.scheduler import load_col_map
from tools.graveyard_writer import setup_graveyard_tab, write_to_graveyard
from tools.sheet_writer import get_sheets_service, read_sheet, write_row_partial

STATUS = "AUDIT_REVIEW"
ACTIVE_NOTE = "[audit] live eBay listing restored to ACTIVE"
REMOVE_REASON = "AUDIT_REVIEW retired — no live eBay listing"
HIDDEN_CHOICES = ("ACTIVE", "PAUSED_OOS", "PAUSED_MARGIN")


def _business():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config", "categories.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["business"]


def _product(row, COL, sheet_row):
    """The auditor's product_dict for a sheet row (Graveyard entry + report line)."""
    g = lambda k: auditor._safe(row, auditor._col_to_idx(COL[k]))
    try:
        sold = int(float(g("sold_90d") or 0))
    except (TypeError, ValueError):
        sold = 0
    try:
        score = float(g("demand_score")) if g("demand_score") else None
    except (TypeError, ValueError):
        score = None
    net = auditor._parse_net(g("net_profit"))
    return {
        "title": g("title"), "category": g("category") or "Unknown", "status": g("status"),
        "net_profit": net if net is not None else "", "sold_90d": sold, "score": score,
        "costco_cost": auditor._safe_float(g("costco_cost")),
        "ebay_price": auditor._safe_float(g("ebay_price")),
        "days_on_sheet": auditor._days_since(g("last_checked")),
        "original_row": sheet_row, "costco_url": g("costco_url"), "sku": g("sku"),
        "ebay_url": g("ebay_listing_url"), "seo_title": g("seo_title"),
        "reason_was": g("tier_summary"),
    }


def plan(rows, COL, start_row, live):
    """{"activate": [...], "hidden": [...], "delete": [...]}, each [(sheet_row, product, why)]."""
    out = {"activate": [], "hidden": [], "delete": []}
    for idx, row in enumerate(rows):
        if not row or auditor._safe(row, auditor._col_to_idx(COL["status"])) != STATUS:
            continue
        sheet_row = idx + start_row
        p = _product(row, COL, sheet_row)
        titles = (p["title"], p["seo_title"])
        why = auditor._live_match(live, p["ebay_url"], p["sku"], titles)
        if not why:
            out["delete"].append((sheet_row, p, REMOVE_REASON))
        elif auditor._live_selling(live, p["ebay_url"], p["sku"], titles):
            out["activate"].append((sheet_row, p, f"live, matched by {why}"))
        else:
            out["hidden"].append((sheet_row, p, f"hidden at qty 0, matched by {why}"))
    return out


def _print_plan(p, hidden_status):
    hidden_to = hidden_status or "left as AUDIT_REVIEW (pass --hidden-status to set one)"
    sections = (("activate", "-> ACTIVE (live eBay listing, selling)"),
                ("hidden", f"-> {hidden_to}: eBay listing hidden at quantity 0"),
                ("delete", "-> Graveyard + DELETE (no live eBay listing)"))
    print(f"\nAUDIT_REVIEW rows: {sum(len(v) for v in p.values())}")
    for key, header in sections:
        print(f"\n{len(p[key])} {header}")
        for sheet_row, prod, why in p[key]:
            net = prod["net_profit"]
            net_s = f"${net:.2f}" if isinstance(net, float) else "—"
            print(f"  r{sheet_row:<4} {prod['title'][:58]:<58} net {net_s:>8} · {why}")
            if prod["reason_was"]:
                print(f"         was: {prod['reason_was'][:100]}")


def _title_still(service, sheet_name, COL, sheet_row, title):
    return auditor._current_title(service, sheet_name, COL, sheet_row) == title


def apply(service, sheet_name, COL, p, hidden_status, run_date):
    """Writes first (row numbers still valid), then Graveyard, then deletes highest row first."""
    done = {"activated": [], "hidden_set": [], "skipped": [], "deleted": 0, "not_deleted": []}
    writes = [("activated", r, prod, "ACTIVE", ACTIVE_NOTE) for r, prod, _ in p["activate"]]
    if hidden_status:
        writes += [("hidden_set", r, prod, hidden_status,
                    f"[audit] eBay listing hidden at qty 0 — set {hidden_status}")
                   for r, prod, _ in p["hidden"]]
    for kind, sheet_row, prod, status, note in writes:
        if not _title_still(service, sheet_name, COL, sheet_row, prod["title"]):
            done["skipped"].append((sheet_row, prod, "row moved, not written"))
            continue
        write_row_partial(service, sheet_name, sheet_row, [(COL["status"], status),
                                                          (COL["tier_summary"], note)])
        done[kind].append((sheet_row, prod))
        time.sleep(0.3)   # Sheets write cap is 60/min per service account

    if p["delete"]:
        setup_graveyard_tab(service)
        write_to_graveyard(service, [auditor._graveyard_row(run_date, prod, why)
                                     for _, prod, why in p["delete"]])
        done["not_deleted"] = auditor._delete_rows(service, sheet_name, COL, p["delete"])
        done["deleted"] = len(p["delete"]) - len(done["not_deleted"])
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    ap.add_argument("--hidden-status", choices=HIDDEN_CHOICES,
                    help="status for rows whose eBay listing is hidden at quantity 0 (default: leave them)")
    args = ap.parse_args(argv)

    business = _business()
    COL = load_col_map()
    sheet_name, start, end = business["sheet_name"], business["data_start_row"], business["data_end_row"]
    service = get_sheets_service()
    rows = read_sheet(service, f"'{sheet_name}'!A{start}:AV{end}")
    live = auditor._live_index()
    if live is None:
        print("eBay ActiveList unreachable — cannot tell live rows from dead ones. Nothing written.")
        return 1

    p = plan(rows, COL, start, live)
    _print_plan(p, args.hidden_status)
    if args.dry_run:
        print("\nDRY RUN — nothing written.")
        return 0

    done = apply(service, sheet_name, COL, p, args.hidden_status, datetime.now().strftime("%Y-%m-%d"))
    print(f"\nDONE: {len(done['activated'])} -> ACTIVE · {len(done['hidden_set'])} hidden set "
          f"{args.hidden_status or '-'} · {done['deleted']} deleted (in the Graveyard tab)")
    for sheet_row, prod, why in done["skipped"] + done["not_deleted"]:
        print(f"  NOT done r{sheet_row} {prod['title'][:50]} — {why}")
    left = 0 if args.hidden_status else len(p["hidden"])
    if left:
        print(f"  {left} row(s) with a hidden listing left as AUDIT_REVIEW")
    return 0


if __name__ == "__main__":
    sys.exit(main())
