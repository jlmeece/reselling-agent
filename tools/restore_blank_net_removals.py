"""
One-off: re-add products the old blank-net auditor bug removed, as PENDING rows.

The bug read a blank net-profit cell as $0.00, so un-priced products were auto-removed with the
exact reason BUG_REASON (current code says "$1.00 min" and skips a blank net entirely). This script
finds those Graveyard rows and appends each product back to the Product Tracker as PENDING with
only cols A / C / D / R / AA (cost, eBay price and net stay blank so research prices them fresh).

Dedupe / idempotency: a Graveyard match is skipped when a Product Tracker row already has the same
Costco product id (col R) or the same title (case-insensitive), and when an earlier Graveyard row
in this run already restored that product (the bug removed the same product many times). A re-run
therefore adds nothing it already restored.

Spec: docs/superpowers/specs/2026-10-21-restore-blank-net-removals.md

Usage:
    python tools/restore_blank_net_removals.py --dry-run   # print the plan, change nothing
    python tools/restore_blank_net_removals.py
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from dotenv import load_dotenv

load_dotenv(encoding="utf-8", override=True)

from tools import sale_schedule as sched  # noqa: E402
from tools.graveyard_writer import read_graveyard  # noqa: E402
from tools.sheet_writer import append_rows_batch, get_sheets_service, read_sheet  # noqa: E402

BUG_REASON = "Below floor ($0.50 min) — net $0.00"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _col_idx(letters):
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _cell(row, idx):
    return str(row[idx]).strip() if idx < len(row) else ""


def plan_restore(graveyard, tracker_rows, COL):
    """
    -> (matched, to_add, skipped). matched = Graveyard rows with BUG_REASON; to_add = one
    Graveyard row per product not already present; skipped = [(graveyard_row, why)].
    """
    t_i, u_i = _col_idx(COL["title"]), _col_idx(COL["costco_url"])
    seen_titles = {_cell(r, t_i).lower() for r in tracker_rows if _cell(r, t_i)}
    seen_pids = {sched.costco_product_id(_cell(r, u_i)) for r in tracker_rows} - {None}

    matched = [g for g in graveyard if (g.get("reason") or "") == BUG_REASON]
    to_add, skipped = [], []
    batch_titles, batch_pids = set(), set()
    for g in matched:
        title = (g.get("title") or "").strip()
        pid = sched.costco_product_id(g.get("costco_url"))
        if not title:
            skipped.append((g, "no title in Graveyard row"))
        elif (pid and pid in seen_pids) or title.lower() in seen_titles:
            skipped.append((g, "already in Product Tracker"))
        elif (pid and pid in batch_pids) or title.lower() in batch_titles:
            skipped.append((g, "duplicate Graveyard entry (restored once)"))
        else:
            to_add.append(g)
            batch_titles.add(title.lower())
            if pid:
                batch_pids.add(pid)
    return matched, to_add, skipped


def row_dict(COL, g):
    """{col: value} for the new PENDING row — only A / C / D / R / AA."""
    return {
        COL["status"]: "PENDING",
        COL["title"]: (g.get("title") or "").strip(),
        COL["category"]: (g.get("category") or "").strip(),
        COL["costco_url"]: (g.get("costco_url") or "").strip(),
        COL["sku"]: (g.get("sku") or "").strip(),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Re-add blank-net Graveyard removals as PENDING")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    with open(os.path.join(_ROOT, "config", "col_map.yaml")) as f:
        COL = yaml.safe_load(f)["columns"]
    with open(os.path.join(_ROOT, "config", "categories.yaml")) as f:
        business = yaml.safe_load(f)["business"]
    sheet_name, start, end = business["sheet_name"], business["data_start_row"], business["data_end_row"]

    service = get_sheets_service()
    graveyard = read_graveyard(service)
    tracker = read_sheet(service, f"'{sheet_name}'!A{start}:AA{end}")
    matched, to_add, skipped = plan_restore(graveyard, tracker, COL)

    prefix = "[DRY RUN] " if args.dry_run else ""
    no_url = sum(1 for g in to_add if not sched.costco_product_id(g.get("costco_url")))
    print(f"{prefix}Graveyard rows with reason {BUG_REASON!r}: {len(matched)}")
    print(f"{prefix}Would re-add {len(to_add)} as PENDING ({no_url} have no Costco URL):"
          if args.dry_run else f"Re-adding {len(to_add)} as PENDING ({no_url} have no Costco URL):")
    for g in to_add:
        print(f"    + {g.get('title')} [{g.get('category') or '?'}] "
              f"(removed {g.get('date_removed') or '?'}){'' if g.get('costco_url') else ' — no URL'}")
    present = [(g, why) for g, why in skipped if why != "duplicate Graveyard entry (restored once)"]
    dupes = len(skipped) - len(present)
    print(f"{prefix}Skipped {len(skipped)}: {len(present)} already present / no title, "
          f"{dupes} duplicate Graveyard entries")
    shown = set()
    for g, why in present:
        key = (g.get("title") or "").lower()
        if key not in shown:
            shown.add(key)
            print(f"    = {g.get('title')} — {why}")

    if args.dry_run or not to_add:
        return 0
    rows = append_rows_batch(service, sheet_name, [row_dict(COL, g) for g in to_add], data_start_row=start)
    print(f"✓ Appended {len(rows)} PENDING rows ({rows[0]}–{rows[-1]})" if rows else "Nothing appended")
    return 0


if __name__ == "__main__":
    sys.exit(main())
