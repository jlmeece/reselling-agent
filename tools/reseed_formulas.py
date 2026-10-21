"""One-shot: re-seed the corrected net-profit formulas (Oct 2026) across all
existing data rows.

The fix in tools/formula_seeder.py only affects NEW rows (researcher.py calls
seed_formula_row on each research pass). This retroactively applies the same
corrected formulas to rows already in the sheet, so the whole tracker — and the
30-day "total earned" — stops being overstated.

Root cause (verified against a real Oct 2026 sale, SKU X0bac7f5423):
  old net formula  = H - G - (H*AB) - AD - AE        -> showed ~$39
  true net         = $20.48
  gap = Costco sales tax not counted ($12.80) + eBay fee understated ($6.11:
        missing $0.30/order, FVF charged on order total incl. buyer tax, and
        Small Appliances rate set 12.55% vs actual ~13.25%).

Corrected formulas (category-agnostic — uses each row's own AB fee rate):
  AC (eBay fee)   = H*AB*1.08 + 0.30    (FVF on item + ~8% avg buyer tax + $0.30/order)
  AF (Costco tax) = G*0.0825             (8.25% sales tax on Jay's Costco purchase)
  I  (net profit) = H - G - AC - AD - AE - AF, blank while H is blank (2026-10-21 guard:
                    an un-priced row computed -cost and the auditor deleted it)

Note: AF was G*0.08 in the original Oct-7 fix; re-verified 2026-10-08 against a real
sale ($24.75 tax on $299.99 = 8.25%) — the "8%" was a misread of the air-fryer numbers.

Also fixes AB (eBay fee rate) 0.1255 -> 0.1325 for Small Appliances rows.

Run on Windows where the Google credentials live (the VPS .env has no sheet creds):
  python tools/reseed_formulas.py            # writes
  python tools/reseed_formulas.py --dry-run  # prints the plan, writes nothing
"""
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(encoding="utf-8", override=True)

from tools.sheet_writer import get_sheets_service, execute_with_retry
from tools.formula_seeder import _get_tab_id, _col_idx, net_profit_formula

SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Product Tracker")
DATA_START = 4
DATA_END = 500


def _formula_cell(tab_id, r, col_letter, value_key, value):
    return {
        "updateCells": {
            "range": {
                "sheetId": tab_id,
                "startRowIndex": r - 1,
                "endRowIndex": r,
                "startColumnIndex": _col_idx(col_letter),
                "endColumnIndex": _col_idx(col_letter) + 1,
            },
            "rows": [{"values": [{"userEnteredValue": {value_key: value}}]}],
            "fields": "userEnteredValue",
        }
    }


def build_requests(rows, tab_id, start_row=DATA_START) -> list:
    """updateCells requests re-seeding I / AC / AF on every row with a status (rows = A:AB
    values from start_row), plus the Small Appliances AB 0.1255 -> 0.1325 fix. Col I is the
    guarded net_profit_formula: blank while col H is blank, so the auditor sees an un-priced
    row (not a negative net) and never deletes it. Pure: no I/O."""
    requests = []
    for i, row in enumerate(rows):
        r = start_row + i
        status = row[0].strip() if len(row) > 0 and row[0] else ""
        if not status:
            continue

        for col_letter, formula in (
            ("I", net_profit_formula(r)),
            ("AC", f"=H{r}*AB{r}*1.08+0.30"),
            ("AF", f"=G{r}*0.0825"),
        ):
            requests.append(_formula_cell(tab_id, r, col_letter, "formulaValue", formula))

        # Fix Small Appliances fee rate (0.1255 -> 0.1325). AB = index 27.
        ab_val = None
        if len(row) > 27 and row[27]:
            try:
                ab_val = float(row[27])
            except ValueError:
                ab_val = None
        if ab_val is not None and abs(ab_val - 0.1255) < 1e-6:
            requests.append(_formula_cell(tab_id, r, "AB", "numberValue", 0.1325))
    return requests


def _blank_h_report(service, sheet_id) -> list[str]:
    """Before/after lines for every status row whose col H is blank (the rows the guard fixes)."""
    rng = f"'{SHEET_NAME}'!A{DATA_START}:I{DATA_END}"
    vals = execute_with_retry(service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=rng)).get("values", [])
    forms = execute_with_retry(service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=rng, valueRenderOption="FORMULA")).get("values", [])
    lines = []
    for i, row in enumerate(vals):
        r = DATA_START + i
        cell = lambda rr, k: rr[k] if len(rr) > k else ""
        if not str(cell(row, 0)).strip() or str(cell(row, 7)).strip():
            continue
        f = forms[i] if i < len(forms) else []
        lines.append(f"  r{r} {cell(row, 0)} {str(cell(row, 2))[:40]!r}: "
                     f"I before {str(cell(f, 8)) or '(empty)'} = {cell(row, 8) or '(blank)'}"
                     f"  ->  after {net_profit_formula(r)} = (blank)")
    return lines


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    service = get_sheets_service()
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise SystemExit("GOOGLE_SHEET_ID not set")
    tab_id = _get_tab_id(service, sheet_id, SHEET_NAME)

    # Read status (A) and fee rate (AB) for every data row to find populated rows.
    rng = f"'{SHEET_NAME}'!A{DATA_START}:AB{DATA_END}"
    result = execute_with_retry(
        service.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng)
    )
    rows = result.get("values", [])

    requests = build_requests(rows, tab_id)
    populated = sum(1 for row in rows if row and str(row[0]).strip())
    fixed_ab = sum(1 for q in requests
                   if "numberValue" in q["updateCells"]["rows"][0]["values"][0]["userEnteredValue"])

    print(f"Rows with data: {populated}")
    print(f"Formula cells to rewrite: {len(requests)}")
    print(f"Small Appliances fee-rate fixes (AB 0.1255->0.1325): {fixed_ab}")
    blank_h = _blank_h_report(service, sheet_id)
    print(f"Rows with blank col H (col I becomes blank): {len(blank_h)}")
    for line in blank_h:
        print(line)

    if dry_run:
        print("DRY RUN — nothing written.")
        return

    if not requests:
        print("Nothing to do.")
        return

    # Single batchUpdate — well under the 60 writes/min rate limit.
    execute_with_retry(service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests}
    ), "reseed formulas")
    print("Done. Formulas re-seeded.")


if __name__ == "__main__":
    main()
