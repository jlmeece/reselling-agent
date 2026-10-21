# URGENT: guard the net formula so the auditor can't delete un-priced rows (Oct 21 2026)

## Goal

The 10/22 08:00 audit WILL delete un-priced rows (e.g. r85 KitchenAid, col H blank) because the
col I net formula computes a bogus negative (≈ −$299.99) for a blank-H row, and the auditor reads
that as "Negative net profit". `setup_sheet.py` guards col I with `IF(H<>"")`, but
`formula_seeder.py` and `reseed_formulas.py` write it WITHOUT the guard — so any re-seeded row with
a blank eBay price gets a computed negative instead of a blank. A blank net must read as blank
(`_parse_net` → `None` → "un-priced, never delete"), never as a number.

## Change

1. In `tools/formula_seeder.py` and `tools/reseed_formulas.py`, wrap the col I formula so it
   returns `""` when col H is blank — match the guard `setup_sheet.py` already uses
   (e.g. `=IF(H{r}="","",H{r}-G{r}-AC{r}-AD{r}-AE{r}-AF{r})`).
2. Re-run `python tools/reseed_formulas.py` to rewrite the existing bad formulas (r85 and any other
   blank-H rows with a computed-negative col I).
3. Verify with `--dry-run` first: every row with blank H must end up with blank col I, and every
   row WITH H keeps the correct computed net.

## Tests

- `reseed_formulas` writes a blank-H row as blank col I (not a negative).
- A priced row keeps `=H-G-AC-AD-AE-AF` (or the guarded form that evaluates the same).
- The auditor's `_parse_net` already treats blank as `None` (existing test) — no auditor change.

## Acceptance

- Run `--dry-run`, paste the before/after for the blank-H rows. Then run for real.
- Commit `fix: guard net formula with IF(H<>"") so un-priced rows aren't deleted by audit`,
  push to `master`, reply `DONE — pushed` with the count of rows whose col I was corrected.
