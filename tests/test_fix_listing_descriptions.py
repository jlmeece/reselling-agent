"""tools/fix_listing_descriptions.py — detection + scrub (pure HTML; no eBay/Sheets calls)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.fix_listing_descriptions import FOOTER, find_violations, scrub

OLD_FOOTER = ('<p style="font-size:12px;color:#6e6e73;margin:0">Sold by JA_Liquidations — Ships from '
              'Texas. eBay Money Back Guarantee applies to all purchases.</p>')
BODY = ('<div style="font-family:Arial;max-width:680px"> <h3 style="font-size:20px">POLYWOOD Chair</h3> '
        '<p style="font-size:15px">Commercial-grade HDPE — Costco quality.</p> '
        '<ul style="list-style:none"> <li style="padding:10px 0">✓ <b>300-lb capacity</b>, 17-in seat</li> '
        '<li style="padding:10px 0">✓ <b>20-year warranty</b></li> </ul> ')
REDIRECT = ('<div style="background:#f5f5f7;border-radius:10px;padding:16px;margin:0 0 20px 0"> '
            '<b style="font-size:14px">Save 10% buying direct:</b> <p style="font-size:13px;margin:6px 0 0 0">'
            'Save 10% buying direct at coming-soon.com/outdoor with code SAVE10 — same product, no eBay fees.'
            '</p> </div> ')
VIOLATING = BODY + REDIRECT + OLD_FOOTER + " </div>"
CLEAN = BODY + OLD_FOOTER + " </div>"


def test_detects_redirect_and_bare_domain():
    hits = find_violations(VIOLATING)
    for h in ("Save 10%", "buying direct", "code SAVE", "no eBay fees", "coming-soon.com/outdoor"):
        assert h in hits


def test_clean_description_has_no_violations():
    assert find_violations(CLEAN) == []
    assert find_violations('<p>See <img src="https://i.ebayimg.com/x.jpg"> on www.ebay.com/itm/1</p>') == []


def test_detects_contact_info_and_external_urls():
    for text in ("Questions? message me", "call me anytime", "email us", "reach jay@example.com",
                 "call (512) 555-0134", "see https://example.com/deal", '<a href="http://shop.io">x</a>'):
        assert find_violations(f"<p>{text}</p>"), text


def test_scrub_removes_only_the_redirect_block_and_sets_exact_footer():
    new, left = scrub(VIOLATING)
    assert left == []
    assert new.startswith(BODY)                                   # body untouched, byte for byte
    assert "Save 10%" not in new and "coming-soon" not in new and "background:#f5f5f7" not in new
    assert new.endswith(f'<p style="font-size:12px;color:#6e6e73;margin:0">{FOOTER}</p> </div>')
    assert new.count("Sold by") == 1


def test_scrub_drops_a_contact_bullet_but_keeps_the_others():
    html = BODY.replace("<li style=\"padding:10px 0\">✓ <b>20-year warranty</b></li>",
                        "<li>✓ Questions? text me at 512-555-0134</li>") + OLD_FOOTER + " </div>"
    new, left = scrub(html)
    assert left == [] and "text me" not in new and "300-lb capacity" in new


def test_scrub_strips_url_in_plain_text_without_dropping_the_wrapper():
    html = '<div style="x"> <h3>Chair</h3> Visit https://example.com/deal today ' + OLD_FOOTER + " </div>"
    new, left = scrub(html)
    assert left == [] and "<h3>Chair</h3>" in new and "example.com" not in new


def test_scrub_appends_footer_when_missing():
    new, left = scrub("<div><p>Nice chair. message me for bulk.</p><p>Solid wood.</p></div>")
    assert left == [] and "Solid wood." in new
    assert new.endswith(f'<p style="font-size:12px;color:#6e6e73;margin:0">{FOOTER}</p> </div>')


def test_scrub_is_idempotent_once_clean():
    new, _ = scrub(VIOLATING)
    assert find_violations(new) == []


def test_eps_size_reads_dimensions_from_url():
    from tools.fix_listing_descriptions import eps_size
    assert eps_size("https://i.ebayimg.com/00/s/ODBYODA=/z/mesAAeSwEcVquWqv/$_1.JPG") == (80, 80)
    assert eps_size("https://i.ebayimg.com/00/s/NzI3WDcyNw==/z/nyg/$_1.JPG") == (727, 727)
    assert eps_size("https://example.com/x.jpg") is None


def test_drop_small_photos_keeps_large_in_order(monkeypatch):
    import tools.ebay_sync as es
    from tools.fix_listing_descriptions import drop_small_photos
    big1 = "https://i.ebayimg.com/00/s/NzI3WDcyNw==/z/A/$_1.JPG"
    small = "https://i.ebayimg.com/00/s/ODBYODA=/z/B/$_1.JPG"
    big2 = "https://i.ebayimg.com/00/s/MTAyNFgxMDI0/z/C/$_1.JPG"
    sent = []
    monkeypatch.setattr(es, "fetch_item_price", lambda iid: {"picture_urls": [big1, small, big2]})
    monkeypatch.setattr(es, "revise_picture",
                        lambda iid, main, gallery, listing_type=None: sent.append([main, *gallery]) or {"ok": True})
    ok, msg = drop_small_photos("318925902421", "FixedPriceItem")
    assert ok and sent == [[big1, big2]] and "dropped 1" in msg


def test_drop_small_photos_refuses_when_nothing_large(monkeypatch):
    import tools.ebay_sync as es
    from tools.fix_listing_descriptions import drop_small_photos
    small = "https://i.ebayimg.com/00/s/ODBYODA=/z/B/$_1.JPG"
    monkeypatch.setattr(es, "fetch_item_price", lambda iid: {"picture_urls": [small]})
    monkeypatch.setattr(es, "revise_picture", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
    assert drop_small_photos("318925902421", "FixedPriceItem")[0] is False
