"""Dump every AUDIT_REVIEW row from the Product Tracker tab, grouped by flag reason.

Read-only — never writes to the sheet. Run on the Windows machine (where
google_credentials.json lives):

    python tools\\dump_audit_review.py

Prints a grouped summary + full list to the console AND writes a copy to
audit_review_dump.txt in the project folder so it can be shared easily.
"""
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(encoding="utf-8", override=True)

from tools.sheet_writer import get_sheets_service, read_sheet

SHEET_NAME = "Product Tracker"
DATA_START = 4      # first data row in the tab
DATA_END = 500      # last possible row (read_sheet truncates empty trailing rows)

# Column letters (match config/col_map.yaml). A = index 0.
IDX_STATUS = ord("A") - ord("A")   # 0  status
IDX_TITLE = ord("C") - ord("A")    # 2  title
IDX_CATEGORY = ord("D") - ord("A")  # 3  category
IDX_COST = ord("G") - ord("A")     # 6  costco_cost
IDX_PRICE = ord("H") - ord("A")    # 7  ebay_price
IDX_NET = ord("I") - ord("A")      # 8  net_profit (formula — returns computed value)
IDX_SOLD = ord("K") - ord("A")     # 10 sold_90d
IDX_CHECKED = ord("O") - ord("A")  # 14 last_checked
IDX_TIER = ord("T") - ord("A")     # 19 tier_summary ("[AUDIT_REVIEW] <reason>")


def _cell(row, idx):
    if idx < len(row):
        v = str(row[idx]).strip()
        return v
    return ""


def _bucket(reason):
    """Collapse the tier_summary reason into a decision bucket."""
    r = reason.lower()
    if "stale" in r:
        return "STALE (no progress 45-60d)"
    if "zero velocity" in r:
        return "ZERO VELOCITY (high net, no sales)"
    if "borderline" in r or "below $1" in r or "below floor" in r:
        return "BORDERLINE NET (<$1)"
    if "oos" in r or "restock" in r:
        return "OUT OF STOCK"
    return "OTHER"


def _net_float(raw):
    s = raw.replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def main():
    service = get_sheets_service()
    rows = read_sheet(service, f"'{SHEET_NAME}'!A{DATA_START}:BB{DATA_END}")

    hits = []
    for i, row in enumerate(rows):
        if _cell(row, IDX_STATUS) != "AUDIT_REVIEW":
            continue
        sheet_row = DATA_START + i
        hits.append({
            "row": sheet_row,
            "title": _cell(row, IDX_TITLE),
            "category": _cell(row, IDX_CATEGORY),
            "net": _cell(row, IDX_NET),
            "sold": _cell(row, IDX_SOLD),
            "checked": _cell(row, IDX_CHECKED),
            "cost": _cell(row, IDX_COST),
            "price": _cell(row, IDX_PRICE),
            "raw_reason": _cell(row, IDX_TIER),
            "bucket": _bucket(_cell(row, IDX_TIER)),
        })

    lines = []
    lines.append(f"AUDIT REVIEW DUMP — {len(hits)} rows")
    lines.append("=" * 60)

    counts = Counter(h["bucket"] for h in hits)
    lines.append("Breakdown:")
    for bucket, n in counts.most_common():
        lines.append(f"  {n:3d}  {bucket}")

    # Group and sort: STALE first, then ZERO VELOCITY, then BORDERLINE, then OTHER.
    order = {"BORDERLINE NET (<$1)": 0, "STALE (no progress 45-60d)": 1,
             "ZERO VELOCITY (high net, no sales)": 2, "OUT OF STOCK": 3, "OTHER": 4}
    groups = defaultdict(list)
    for h in hits:
        groups[h["bucket"]].append(h)
    for bucket in sorted(groups, key=lambda b: order.get(b, 9)):
        items = sorted(groups[bucket], key=lambda h: (_net_float(h["net"]) is None,
                                                       -(_net_float(h["net"]) or 0)))
        lines.append("")
        lines.append(f"--- {bucket} ({len(items)}) ---")
        for h in items:
            net = h["net"] if h["net"] else "?"
            lines.append(
                f"row {h['row']:3d} | {h['category'][:14]:14s} | net {net:>10s} | "
                f"sold {h['sold'] or '?':>4s} | {h['title'][:60]}"
            )
            if h["raw_reason"]:
                lines.append(f"          reason: {h['raw_reason']}")

    text = "\n".join(lines)
    print(text)

    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "audit_review_dump.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
