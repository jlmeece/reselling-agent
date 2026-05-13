"""
Tool: listing_copy
Generates SEO-optimized eBay listing copy, HTML descriptions, ad copy, and meta descriptions
via the Claude API in efficient batches of up to 5 products per call.

Uses prompt caching on the system prompt — when batches run back-to-back,
subsequent calls cost ~10% of the first call. Reduces API spend by 60-80%
on large product runs.
"""

import os
import json
import anthropic


SYSTEM_PROMPT = """You are an expert eBay seller and SEO copywriter for the seller account JA_Liquidations. \
You write listing copy that builds trust, ranks in eBay search, and converts browsers into buyers. \
Every listing includes a redirect message that offers the buyer 10% off to purchase direct from the seller's website.

Seller identity: JA_Liquidations — ships from Texas. Use this to build buyer confidence.

Rules:
- Never use the word "beautiful" — it is generic and kills trust
- Always mention specific materials, dimensions, or specs in at least one bullet
- Redirect message must reference the exact site URL and discount code provided
- Headlines must be scannable, not clever — buyers search with keywords not wit
- Meta descriptions must be exactly 155 characters
- HTML descriptions must use ONLY inline styles — no <style> blocks, eBay strips them
- HTML descriptions must contain NO <a href> to external sites — eBay policy
- HTML descriptions must contain NO <script>, <form>, <embed>, or event handlers
- Footer line in every HTML description must read exactly:
  "Sold by JA_Liquidations — Ships from Texas. eBay Money Back Guarantee applies to all purchases."

Return ONLY a raw JSON array. No markdown fences, no explanation. Start with [ and end with ]."""


_CATEGORY_RULES = {
    "Jewelry": """
JEWELRY/GOLD COPY RULES: Mention assay certificate or COA if applicable. \
Reference mint or hallmark (e.g. PAMP Suisse, Argor Heraeus, Perth Mint). \
State IRA-eligibility if gold (.999+ fine). Lead with spot premium context — buyers \
want to know they're getting value vs melt price. Trust is everything: "authentic," \
"certified," "sealed" are high-conversion words for this category.""",

    "Outdoor Furniture": """
OUTDOOR FURNITURE COPY RULES: Lead with material durability (HDPE, powder-coated steel, \
Sunbrella fabric, etc.). Call out weather/UV resistance explicitly. Mention HOA-friendly \
neutral colors where applicable. Note that item ships fully or partially assembled — \
reduces buyer hesitation. Reference Costco quality and brand name prominently. \
Seasonal urgency is real: patio buyers move fast in spring.""",

    "Watches": """
WATCHES COPY RULES: Box and papers are included (standard from Costco) — always state this. \
Specify movement type (solar, eco-drive, quartz, automatic). State water resistance in ATM or meters. \
Brand heritage matters — Citizen, Seiko, Luminox are trusted names, lean into that. \
Warranty info (usually 2-5 years) is a trust signal. Buyers comparison-shop on specs: \
case size, band material, dial color belong in bullets.""",
}


_HTML_TEMPLATE_INSTRUCTIONS = """
For the `description` field, generate a complete eBay-safe HTML listing body. \
Use ONLY these tags: div, h3, p, ul, li, b, table, tr, td, br. \
ALL styles must be inline (no <style> block). NO external links. NO scripts. \
Follow this exact structure (fill in real product content):

<div style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;max-width:680px;margin:0 auto;padding:24px;color:#1d1d1f;background:#fff">
  <h3 style="font-size:20px;font-weight:600;margin:0 0 8px 0">{SEO TITLE}</h3>
  <p style="font-size:15px;color:#6e6e73;margin:0 0 20px 0">{ONE-LINE VALUE PROPOSITION}</p>
  <ul style="list-style:none;padding:0;margin:0 0 24px 0">
    <li style="padding:10px 0;border-bottom:1px solid #f2f2f2;font-size:14px">✓ {BULLET 1}</li>
    <li style="padding:10px 0;border-bottom:1px solid #f2f2f2;font-size:14px">✓ {BULLET 2}</li>
    <li style="padding:10px 0;border-bottom:1px solid #f2f2f2;font-size:14px">✓ {BULLET 3}</li>
    <li style="padding:10px 0;border-bottom:1px solid #f2f2f2;font-size:14px">✓ {BULLET 4}</li>
    <li style="padding:10px 0;font-size:14px">✓ {BULLET 5}</li>
  </ul>
  <div style="background:#f5f5f7;border-radius:10px;padding:16px;margin:0 0 20px 0">
    <b style="font-size:14px">Save 10% buying direct:</b>
    <p style="font-size:13px;color:#1d1d1f;margin:6px 0 0 0">{REDIRECT MESSAGE — site URL and code}</p>
  </div>
  <p style="font-size:12px;color:#6e6e73;margin:0">Sold by JA_Liquidations — Ships from Texas. New in original packaging. \
eBay Money Back Guarantee applies to all purchases.</p>
</div>

The HTML must be a single string with no newlines that would break a CSV cell. \
Replace all literal newlines with a single space inside the HTML string."""


def generate_listing_copy(products_batch):
    """
    Sends up to 5 products to Claude in one API call with prompt caching.
    products_batch: list of dicts with keys: title, category, cost, sell_price, site_url, discount_code
    Returns: list of dicts with listing copy fields per product.
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # Collect unique categories in this batch for targeted rules injection
    categories_in_batch = {p.get("category", "") for p in products_batch}
    category_rules_text = ""
    for cat in categories_in_batch:
        for key, rule in _CATEGORY_RULES.items():
            if key.lower() in cat.lower() or cat.lower() in key.lower():
                category_rules_text += rule + "\n"

    product_lines = []
    for i, p in enumerate(products_batch, 1):
        product_lines.append(
            f"{i}. PRODUCT: {p['title']}\n"
            f"   CATEGORY: {p['category']}\n"
            f"   COSTCO COST: ${p['cost']}\n"
            f"   EBAY SELL PRICE: ${p['sell_price']}\n"
            f"   SITE URL: {p.get('site_url', '')}\n"
            f"   DISCOUNT CODE: {p.get('discount_code', 'SAVE10')}"
        )

    user_content = (
        "Generate listing copy for these products:\n\n"
        + "\n\n".join(product_lines)
    )

    if category_rules_text.strip():
        user_content += f"\n\nCATEGORY-SPECIFIC RULES TO APPLY:\n{category_rules_text.strip()}"

    user_content += (
        f"\n\n{_HTML_TEMPLATE_INSTRUCTIONS}\n\n"
        "Return a JSON array with one object per product. Each object must have:\n"
        "- seo_title: eBay title max 80 chars, keyword-rich, no ALL CAPS\n"
        "- bullets: exactly 5 bullet points separated by | character\n"
        "- description: complete eBay HTML body as described above (single string, no literal newlines)\n"
        "- meta_desc: exactly 155 chars for WordPress SEO\n"
        "- keywords: 6-8 comma-separated search terms\n"
        "- alt_text: one descriptive sentence for the main product image\n"
        "- redirect_msg: \"Save 10% buying direct at [SITE_URL] with code [CODE] — same product, no eBay fees.\"\n"
        "- google_hl: 3 Google ad headlines max 30 chars each, separated by |\n"
        "- google_desc: 2 Google ad descriptions max 90 chars each, separated by |\n"
        "- meta_text: Meta/Facebook primary text max 125 chars\n"
        "- meta_hl: Meta headline max 40 chars\n"
        "- demand_note: one sentence on why this sells well or a risk to watch"
    )

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_content}],
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    return json.loads(raw)
