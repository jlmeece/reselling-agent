"""
One-off: scrub off-eBay language from every live listing's description and set the exact footer,
revising each listing in place (ReviseFixedPriceItem Description — never a re-list).

A description violates when it contains the old redirect line ("Save 10%", "buying direct",
"no eBay fees", "code SAVE"), a non-eBay URL or bare domain (coming-soon.com/…), or contact info
("message me", "text me", "call me", "email", an email address, a phone number). Only the
offending element/text is removed and the closing "Sold by …" footer is set to FOOTER; the rest
of the body (title, bullets, specs) is left byte-for-byte as it was. A description that is still
violating after the scrub is NOT sent (reported instead). Clean descriptions are skipped, so
re-runs are idempotent.

Source of the current description: GetItem. eBay refuses GetItem on a listing it has hidden for
policy 21920397 ("Offering to buy or sell outside of eBay") — for those the exported HTML in the
row's col AK (description) is used; on 2026-10-21 col AK matched the live description on all 44
linked rows. After a successful revise the row's col AK is scrubbed the same way so a re-export
can't bring the line back.

Spec: docs/superpowers/specs/2026-10-21-scrub-descriptions.md

Usage:
    python tools/fix_listing_descriptions.py --dry-run
    python tools/fix_listing_descriptions.py
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from dotenv import load_dotenv

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_ROOT, ".env"), encoding="utf-8", override=True)

from tools.ebay_sync import (extract_item_id, fetch_active_listings,  # noqa: E402
                             fetch_item_description, revise_description)

FOOTER = "Sold by JA_Liquidations — Ships from Texas. eBay Money Back Guarantee applies."
FOOTER_STYLE = "font-size:12px;color:#6e6e73;margin:0"
POLICY_HIDDEN_CODE = "21920397"

_REDIRECT = r"Save 10%|buying direct|no eBay fees|code SAVE"
_CONTACT = r"\bmessage me\b|\btext me\b|\bcall me\b|\bemail\b"
_EMAIL = r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
_PHONE = r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"
_URL = r"\bhttps?://[^\s<>\"']+"
_DOMAIN = r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|shop|store|io|co|us|biz|info)\b(?:/[^\s<>\"']*)?"

_RX = {k: re.compile(v, re.I) for k, v in (("redirect", _REDIRECT), ("contact", _CONTACT),
                                         ("email", _EMAIL), ("phone", _PHONE))}
_URL_RX = re.compile(_URL, re.I)
_DOMAIN_RX = re.compile(_DOMAIN, re.I)
_TAG_RX = re.compile(r"<[^>]+>")


def _is_ebay_host(text):
    host = re.sub(r"^https?://", "", text, flags=re.I).split("/")[0].lower()
    return host in ("ebay.com", "ebayimg.com") or host.endswith((".ebay.com", ".ebayimg.com"))


def _external_links(text):
    """Non-eBay http(s) URLs + bare domains in visible text and in attributes."""
    found = [u for u in _URL_RX.findall(text) if not _is_ebay_host(u)]
    visible = _URL_RX.sub(" ", _TAG_RX.sub(" ", text))
    found += [d for d in _DOMAIN_RX.findall(visible) if not _is_ebay_host(d)]
    return found


def find_violations(html):
    """Distinct offending strings found in the description (empty list = clean)."""
    text = _TAG_RX.sub(" ", html or "")
    hits = []
    for rx in _RX.values():
        hits += [m.group(0) for m in rx.finditer(text)]
    hits += _external_links(html or "")
    out = []
    for h in hits:
        if h not in out:
            out.append(h)
    return out


def _offending(fragment):
    text = _TAG_RX.sub(" ", fragment)
    return (any(rx.search(text) for rx in (_RX["redirect"], _RX["contact"]))
            or bool(_RX["email"].search(text)) or bool(_RX["phone"].search(text))
            or bool(_external_links(fragment)))


def _remove_elements(html, tag):
    """Remove every innermost-enough <tag>…</tag> element whose content offends."""
    rx = re.compile(rf"<{tag}\b[^>]*>(?:(?!<{tag}\b|</{tag}>).)*</{tag}>", re.I | re.S)
    return rx.sub(lambda m: "" if _offending(m.group(0)) else m.group(0), html)


def _strip_text(html):
    """Last resort inside remaining text: drop URLs/domains/emails/phones themselves."""
    def clean(seg):
        seg = _URL_RX.sub(lambda m: m.group(0) if _is_ebay_host(m.group(0)) else "", seg)
        seg = _DOMAIN_RX.sub(lambda m: m.group(0) if _is_ebay_host(m.group(0)) else "", seg)
        return _RX["phone"].sub("", _RX["email"].sub("", seg))
    parts = re.split(r"(<[^>]+>)", html)
    return "".join(p if p.startswith("<") else clean(p) for p in parts)


def _set_footer(html):
    """Last <p>…Sold by…</p> -> FOOTER (its own attributes kept); none -> appended."""
    matches = list(re.finditer(r"(<p\b[^>]*>)(?:(?!</p>).)*?Sold by(?:(?!</p>).)*</p>", html, re.I | re.S))
    if matches:
        m = matches[-1]
        return html[:m.start()] + f"{m.group(1)}{FOOTER}</p>" + html[m.end():]
    footer = f'<p style="{FOOTER_STYLE}">{FOOTER}</p>'
    stripped = html.rstrip()
    if stripped.lower().endswith("</div>"):
        cut = stripped.lower().rfind("</div>")
        return stripped[:cut] + footer + " " + stripped[cut:]
    return stripped + " " + footer


def scrub(html):
    """-> (new_html, remaining_violations). Removes offending p/li/b/span/td elements, strips
    any offending text left outside them, drops a div that is now empty (the old redirect box),
    and sets the footer. A div is never removed for its content — the outer wrapper is one."""
    out = html
    for tag in ("p", "li", "b", "span", "td"):
        out = _remove_elements(out, tag)
    out = _strip_text(out)
    out = re.sub(r"<div\b[^>]*>\s*</div>", "", out, flags=re.I)
    out = _set_footer(out)
    out = re.sub(r"(> )\s+", r"\1", out)            # collapse the gap a removed block leaves
    return out, find_violations(out)


def _footer_of(html):
    """Text of the last <p> holding 'Sold by' — the same element _set_footer replaces."""
    m = list(re.finditer(r"<p\b[^>]*>((?:(?!</p>).)*?Sold by(?:(?!</p>).)*)</p>", html or "", re.I | re.S))
    return _TAG_RX.sub("", m[-1].group(1)).strip() if m else "(none)"


SMALL_PHOTO_CODE = "21919137"   # eBay refuses any revise while a listing photo is < 500px
MIN_PHOTO_PX = 500


def eps_size(url):
    """(w, h) from an EPS URL's /s/<base64 'WxH'>/ token, or None."""
    m = re.search(r"/s/([A-Za-z0-9+=]+)/", str(url or ""))
    if not m:
        return None
    try:
        import base64
        tok = m.group(1).rstrip("=")
        w, h = base64.b64decode(tok + "=" * (-len(tok) % 4)).decode().lower().split("x")
        return int(w), int(h)
    except Exception:
        return None


def drop_small_photos(item_id, listing_type):
    """Re-send the listing's pictures without the ones eBay reports under MIN_PHOTO_PX (size read
    from the EPS URL). -> (ok, message). Refuses when no picture would be left."""
    from tools.ebay_sync import fetch_item_price, revise_picture
    live = fetch_item_price(item_id)
    pics = live.get("picture_urls") or []
    def large(u):
        size = eps_size(u)
        return size is None or max(size) >= MIN_PHOTO_PX     # unknown size: keep, don't guess
    keep = [u for u in pics if large(u)]
    if not keep or len(keep) == len(pics):
        return False, f"no small photo to drop safely ({len(pics)} photos, {len(keep)} large)"
    res = revise_picture(item_id, keep[0], keep[1:], listing_type=listing_type)
    return res["ok"], (f"dropped {len(pics) - len(keep)} photo(s) under {MIN_PHOTO_PX}px, kept {len(keep)}"
                       if res["ok"] else res["message"][:160])


def _load_sheet():
    with open(os.path.join(_ROOT, "config", "col_map.yaml")) as f:
        COL = yaml.safe_load(f)["columns"]
    with open(os.path.join(_ROOT, "config", "categories.yaml")) as f:
        business = yaml.safe_load(f)["business"]
    from tools.sheet_writer import get_sheets_service, read_sheet
    service = get_sheets_service()
    start, end = business["data_start_row"], business["data_end_row"]
    rows = read_sheet(service, f"'{business['sheet_name']}'!A{start}:AK{end}")
    return COL, service, business["sheet_name"], start, rows


def _col(letters):
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Scrub off-eBay language from live listing descriptions")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, revise nothing")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    listings = fetch_active_listings()
    if not listings:
        print("❌ Couldn't fetch eBay listings — nothing changed.")
        return 1
    COL, service, sheet_name, start, rows = _load_sheet()
    q_i, ak_i = _col(COL["ebay_listing_url"]), _col(COL["description"])
    by_item = {}
    for off, r in enumerate(rows):
        iid = extract_item_id(r[q_i] if q_i < len(r) else "")
        if iid:
            by_item.setdefault(iid, []).append((start + off, r[ak_i].strip() if ak_i < len(r) else ""))

    prefix = "[DRY RUN] " if args.dry_run else ""
    fixed, clean, skipped, failed = [], [], [], []
    for l in listings:
        iid, title = l["item_id"], (l.get("title") or "")[:60]
        live = fetch_item_description(iid)
        source, listing_type = "GetItem", live.get("listing_type") or l.get("listing_type") or None
        desc = live["description"] if live["ok"] else ""
        if not live["ok"]:
            sheet = by_item.get(iid, [])
            if live["error_code"] == POLICY_HIDDEN_CODE and len(sheet) == 1 and sheet[0][1]:
                desc, source = sheet[0][1], f"sheet col AK row {sheet[0][0]} (eBay hid the listing)"
                listing_type = l.get("listing_type") or None
            else:
                why = (f"GetItem failed ({live['error_code'] or live['error_kind']}: {live['message'][:80]})"
                       + ("" if live["error_code"] != POLICY_HIDDEN_CODE else
                          f"; col AK unusable ({len(sheet)} linked rows)"))
                skipped.append((iid, title, why))
                print(f"  skip   {iid} {title!r}: {why}")
                continue
        if listing_type and listing_type != "FixedPriceItem":
            skipped.append((iid, title, f"listing type {listing_type}"))
            print(f"  skip   {iid} {title!r}: listing type {listing_type}")
            continue
        if live.get("has_variations"):
            skipped.append((iid, title, "multi-variation listing"))
            print(f"  skip   {iid} {title!r}: multi-variation listing")
            continue
        hits = find_violations(desc)
        if not hits:
            clean.append(iid)
            continue
        new, left = scrub(desc)
        print(f"  {prefix}{iid} {title!r} [{source}]")
        print(f"      found: {', '.join(repr(h) for h in hits)}")
        print(f"      footer before: {_footer_of(desc)!r}")
        print(f"      footer after:  {_footer_of(new)!r}")
        print(f"      length {len(desc)} → {len(new)}")
        if left:
            failed.append((iid, title, f"still violating after scrub: {left}"))
            print(f"      ✗ NOT sent — still violating: {left}")
            continue
        if args.dry_run:
            fixed.append(iid)
            continue
        res = revise_description(iid, new, listing_type=listing_type)
        if not res["ok"] and res["error_code"] == SMALL_PHOTO_CODE:
            ok, msg = drop_small_photos(iid, listing_type)
            print(f"      {'✓' if ok else '✗'} photos: {msg}")
            if ok:
                res = revise_description(iid, new, listing_type=listing_type)
        if not res["ok"]:
            failed.append((iid, title, f"{res['error_kind']} {res['error_code']}: {res['message'][:120]}"))
            print(f"      ✗ revise failed: {res['message'][:160]}")
            if res["error_kind"] == "auth":
                print("eBay token rejected — stopping.")
                break
            continue
        fixed.append(iid)
        print(f"  {iid} → description fixed")
        for row, ak in by_item.get(iid, []):
            if ak and find_violations(ak):
                ak_new, ak_left = scrub(ak)
                if not ak_left:
                    from tools.sheet_writer import safe_write_row
                    safe_write_row(service, sheet_name, row, [(COL["description"], ak_new)])
                    print(f"      sheet row {row} col AK scrubbed too")

    print(f"\n{prefix}Summary: {'would fix' if args.dry_run else 'fixed'} {len(fixed)} · "
          f"already clean {len(clean)} · skipped {len(skipped)} · failed {len(failed)}")
    for iid, title, why in skipped:
        print(f"  SKIPPED {iid} {title!r}: {why}")
    for iid, title, why in failed:
        print(f"  FAILED  {iid} {title!r}: {why}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
