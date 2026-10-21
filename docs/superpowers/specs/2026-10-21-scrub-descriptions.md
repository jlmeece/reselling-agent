# Scrub off-eBay language + fix footer on ALL live listings (Oct 21 2026)

## Goal

Every live eBay listing must have its description scrubbed of off-eBay language — the old
"Save 10% buying direct at [site] — no eBay fees" redirect line, any external URL, any contact
info — and end with the clean footer. The Henredon listing (318910864177) was already hidden
under policy 21920397 ("Offering to buy or sell outside of eBay") for this. Fix every live
listing via the API now, in place, no re-listing, no manual editing.

## Change

### Part 1 — tools/ebay_sync.py: revise description
Add `revise_description(item_id, new_description)`: a ReviseFixedPriceItem body that sets
`Item.Description` to the new HTML string. Mirror the existing `_build_revise_xml` / `_call`
plumbing (same token/header/auth). Skip non-fixed-price listings — reuse the
`listing_type != "FixedPriceItem"` guard from `revise_fixed_price`.

### Part 2 — tools/fix_listing_descriptions.py (one-off, idempotent, --dry-run)
1. `fetch_active_listings()` → every ACTIVE item (with item id + title).
2. For each: GetItem → current `Description`.
3. Detect violations — the description contains ANY of:
   - `Save 10%`, `buying direct`, `no eBay fees`, `code SAVE` (the old redirect line)
   - an `http`/`https` URL whose host is not eBay (ebay.com / ebayimg.com)
   - `message me`, `text me`, `call me`, `email`, an email address, or a phone number
4. If violating: remove the offending line/block (the redirect `p`, the URL, the contact info),
   and set the closing footer to EXACTLY:
   `Sold by JA_Liquidations — Ships from Texas. eBay Money Back Guarantee applies.`
   Keep the rest of the description body (title, bullets, specs) untouched — only strip the
   violation and replace the footer.
5. Revise the listing's Description via `revise_description`.
6. `--dry-run` prints the full plan: item id + title + what offending text was found + the
   before/after footer. Real run revises and prints `item_id → description fixed`.
7. Idempotent — re-running skips listings whose description is already clean.

## Pitfalls (do not skip)

- **Never re-list.** ReviseFixedPriceItem on the existing item id preserves rank/watchers/sold.
- **Only touch the violation + footer.** Do not rewrite the working body (bullets/specs) — a
  botched full-rewrite risks breaking the listing's content.
- **Footer must be exact.** Use the string above verbatim.
- Skip non-fixed-price / multi-variation listings; log + report them, don't guess.

## Acceptance

- Run `--dry-run` first and paste the plan for Jay to review. Then run for real.
- Commit `feat: scrub off-eBay language + fix footer on all live listings`, push to `master`,
  reply `DONE — pushed` with counts (fixed / already-clean / skipped + why).
