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
  I  (net profit) = H - G - AC - AD - AE - AF

Note: AF was G*0.08 in the original Oct-7 fix; re-verified 2026-10-08 against a real
sale ($24.75 tax on $299.99 = 8.25%) — the "8%" was a misread of the air-fryer numbers.

Also fixes AB (eBay fee rate) 0.1255 -> 0.1325 for Small Appliances rows.

Run on Windows where the Google credentials live (the VPS .env has no sheet creds):
  python tools/reseed_formulas.py            # writes
  python tools/reseed_formulas.py --dry-run  # prints the plan, writes nothing
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.sheet_writer import get_sheets_service, execute_with_retry
from tools.formula_seeder import _get_tab_id, _col_idx

SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Product Tracker")
DATA_START = 4
DATA_END = 500


def _col_letter(idx: int) -> str:
    return chr(ord("A") + idx)


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

    requests = []
    fixed_ab = 0
    populated = 0
    for i, row in enumerate(rows):
        r = DATA_START + i
        status = row[0].strip() if len(row) > 0 and row[0] else ""
        if not status:
            continue
        populated += 1

        # AB = fee rate (col 28 => index 27)
        ab_val = None
        if len(row) > 27 and row[27]:
            try:
                ab_val = float(row[27])
            except ValueError:
                ab_val = None

        # Rewrite the three formula cells for this row.
        for col_letter, formula in (
            ("I", f"=H{r}-G{r}-AC{r}-AD{r}-AE{r}-AF{r}"),
            ("AC", f"=H{r}*AB{r}*1.08+0.30"),
            ("AF", f"=G{r}*0.0825"),
        ):
            requests.append({
                "updateCells": {
                    "range": {
                        "sheetId": tab_id,
                        "startRowIndex": r - 1,
                        "endRowIndex": r,
                        "startColumnIndex": _col_idx(col_letter),
                        "endColumnIndex": _col_idx(col_letter) + 1,
                    },
                    "rows": [{"values": [{"userEnteredValue": {"formulaValue": formula}}]}],
                    "fields": "userEnteredValue",
                }
            })

        # Fix Small Appliances fee rate (0.1255 -> 0.1325).
        if ab_val is not None and abs(ab_val - 0.1255) < 1e-6:
            requests.append({
                "updateCells": {
                    "range": {
                        "sheetId": tab_id,
                        "startRowIndex": r - 1,
                        "endRowIndex": r,
                        "startColumnIndex": _col_idx("AB"),
                        "endColumnIndex": _col_idx("AB") + 1,
                    },
                    "rows": [{"values": [{"userEnteredValue": {"numberValue": 0.1325}}]}],
                    "fields": "userEnteredValue",
                }
            })
            fixed_ab += 1

    print(f"Rows with data: {populated}")
    print(f"Formula cells to rewrite: {len(requests)}")
    print(f"Small Appliances fee-rate fixes (AB 0.1255->0.1325): {fixed_ab}")

    if dry_run:
        print("DRY RUN — nothing written.")
        return

    if not requests:
        print("Nothing to do.")
        return

    # Single batchUpdate — well under the 60 writes/min rate limit.
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests}
    ).execute()
    print("Done. Formulas re-seeded.")


if __name__ == "__main__":
    main()
