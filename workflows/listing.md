# Workflow: Listing
**Layer:** Workflow (Layer 1 — Instructions)
**Agent reads this:** Yes — SOP for copy generation and listing approval
**Triggered:** Automatically during monitor.md run (for new products), or manually for re-generation

---

## Objective

Generate SEO-optimized eBay listing copy, ad copy, and site meta descriptions for new products. Write all output to the Google Sheet. Copy is generated once per product — the agent does not regenerate existing copy unless explicitly told to.

---

## Required Inputs

- Product row data: title, category, costco_cost (col E), ebay_price (col F), site_url (from categories.yaml)
- Column L (seo_title) must be empty for the product to be queued
- `.env`: ANTHROPIC_API_KEY must be set

---

## Tools Used

1. `tools/listing_copy.py` — `generate_listing_copy(batch)` — calls Claude API
2. `tools/sheet_writer.py` — `write_row_partial()` — writes copy to sheet

---

## What Gets Generated (Per Product)

| Column | Field | Description |
|--------|-------|-------------|
| L | seo_title | eBay title, max 80 chars, keyword-rich |
| M | bullets | 5 selling bullets separated by \| |
| N | description | (included in bullets field) |
| O | redirect_msg | "Save 10% at [site] with code SAVE10 — no eBay fees" |
| P | meta_desc | 155-char WordPress SEO description |
| Q | keywords | 6–8 search terms for eBay and Google |
| R | alt_text | Image alt text for accessibility and SEO |
| AC | google_hl | 3 Google Ads headlines (max 30 chars each), pipe-separated |
| AD | google_desc | 2 Google Ads descriptions (max 90 chars each), pipe-separated |
| AE | meta_text | Meta/Facebook ad primary text, max 125 chars |
| AF | meta_hl | Meta/Facebook ad headline, max 40 chars |
| AJ | notes | One-sentence demand insight or risk flag |

---

## The Redirect Message (Column O) — Most Important Field

Every eBay listing must include the redirect message in the listing description. It directs buyers to your WooCommerce site for 10% off. Example:

> "Save 10% buying direct at coming-soon.com/jewelry with code SAVE10 — same product, no eBay fees."

This is your retention moat. Never skip this field.

---

## Batching Logic

- Products are batched in groups of **5** (configured in categories.yaml batch_size)
- Each batch = one Claude API call
- Prompt caching is active on the system prompt — batches 2+ in a sequence cost ~10% of batch 1
- Wait 1 second between batches to avoid rate limits

---

## Copy Quality Rules (enforced in system prompt)

- Never use the word "beautiful" — generic, kills trust
- Mention specific materials, dimensions, or specs in at least one bullet
- seo_title must read like a search query, not a marketing headline
- Redirect message must always reference the correct site URL for the product's category

---

## When to Re-Generate Copy

The agent never overwrites copy that already exists. To force a re-generate:
1. Clear the value in column L (seo_title) for that row
2. The next agent run will include that row in the copy batch

---

## Edge Cases

- **Claude returns malformed JSON:** Strip markdown fences, try json.loads — if still fails, log error and skip that batch
- **Product has no sell_price (col F):** Skip — cannot calculate margin or write meaningful redirect message
- **Category not in categories.yaml:** Use empty site_url string — copy will still be generated but redirect message will have a blank URL
- **Batch size > 5:** Never exceed 5 — response JSON becomes unreliable above this count

---

## Self-Improvement Notes

*Update this section when you encounter prompt quality issues or new requirements.*

- The system prompt is cached (ephemeral) — same prompt text across all batches in a session means batches 2+ are 80-90% cheaper
- If copy quality drops for a specific category, add category-specific copy rules to the system prompt in tools/listing_copy.py SYSTEM_PROMPT constant
- Gold/jewelry copy should always mention karat, weight, and certification when available in the product title
