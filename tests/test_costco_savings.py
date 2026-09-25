"""Tests for tools/costco_savings.py (parse / classify / cross-reference matcher / alert text) and
agents/scheduler.py run_savings (mocked browser, sheet, Telegram)."""
import json
import os
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents import scheduler as sch                       # noqa: E402
from tools import costco_savings as cs                    # noqa: E402

COL = sch.load_col_map()
CONFIG = sch.load_config()


# ── parsing (real card text captured from the live page, 2026-09-24) ──────────

def _card(text, href="https://www.costco.com/.product.4000438878.html", title="Some Item", section="Apparel"):
    return {"href": href, "title": title, "text": text, "section": section}


def test_parse_card_after_form_derives_regular_price():
    it = cs.parse_card(_card(
        "Warehouse\n&\nOnline\nBanana Republic Women's Ponte Pant\nItem 2010239\n"
        "Limit 10. Selection varies by location.\n$\n15\n.\n99\nAfter $4 OFF\nBuy Online"))
    assert (it["list_sale"], it["list_savings"], it["list_regular"]) == (15.99, 4.0, 19.99)
    assert it["item_number"] == "2010239" and it["limit"] == 10
    assert it["tag"] == "Warehouse & Online"
    assert it["product_id"] == "4000438878"


def test_parse_card_save_form_has_savings_only_and_decimals():
    it = cs.parse_card(_card("Warehouse\n&\nOnline\nVita Coco\nItem 1218891\nLimit 10.\nSave $\n5\n.\n80\nBuy Online"))
    assert it["list_savings"] == 5.8
    assert it["list_sale"] is None and it["list_regular"] is None


def test_parse_card_without_savings_text():
    it = cs.parse_card(_card("Online Only\nLG 77\" OLED TV\nItem 9577045\nLimit 10.\nBuy Online"))
    assert it["list_savings"] is None and it["tag"] == "Online Only"


@pytest.mark.parametrize("url,pid", [
    ("https://www.costco.com/.product.4000422365.html", "4000422365"),
    ("https://www.costco.com/koa-mana-drink-mix.product.4000389611.html?x=1", "4000389611"),
    ("https://www.costco.com/p/-/airborne-immune-support-36-effervescent-tablets/100217166", "100217166"),
    ("https://www.costco.com/appliances.html?keyword=WPOCT26", None),
    ("", None),
])
def test_product_id(url, pid):
    assert cs.product_id(url) == pid


def test_page_end_date():
    assert cs.page_end_date("Shop now\nValid 9/21/26 - 10/18/26\nPrices in Alaska") == "10/18/26"
    assert cs.page_end_date("no banner here") is None


# ── category classification ──────────────────────────────────────────────────

KW = cs.compile_keywords(CONFIG["business"]["savings"]["keywords"])


@pytest.mark.parametrize("title,cat", [
    ("PAMP Suisse 1 oz Gold Bar", "Precious Metals"),
    ("14kt Yellow Gold Paperclip Bracelet", "Jewelry"),
    ("Citizen Eco-Drive Men's Watch", "Watches"),
    ("Agio Menorca 4-piece Outdoor Patio Deep Seating Set", "Outdoor Furniture"),
    ("Kirkland Signature Fish Oil 1000 mg, 400 Softgels", "Pharmacy"),
    ("Sports Research L-Theanine 150 ct", "Pharmacy"),
    ("Emergen-C Vitamin C Variety pack, 120 ct", "Pharmacy"),
    ("Ninja Creami Deluxe Ice Cream Maker", "Small Appliances"),
    ("Vitamix Ascent X4 Blender", "Small Appliances"),
    ("T-fal Dual Window Air Fryer 7.3 qt", "Small Appliances"),
])
def test_classify_in_category(title, cat):
    assert cs.classify_category(title, KW) == cat


@pytest.mark.parametrize("title", [
    "Samsung 55\" Class M70H Series 4K Smart TV",
    "Tide + Ultra OXI Laundry Detergent PODS",
    "Apple Watch Series 10",                     # smart watch is excluded
    "Cuisinart 12-piece Essential Tool and Gadget Set",
    "KitchenAid Hard Anodized Cookware Set",     # excluded: cookware, not an appliance
    "Kirkland Signature Salmon Dog Food with Fish Oil",   # pet excluded from Pharmacy
    "Ring Video Doorbell",                       # 'ring' alone must not make it Jewelry
    "Bissell Cordless Vacuum",
    "Keurig K-Cup Pods Lavazza Espresso",       # coffee pods are grocery, not an appliance
    "Banana Republic Women's Button-Up Cardigan",
])
def test_classify_off_category(title):
    assert cs.classify_category(title, KW) is None


def test_classify_first_matching_category_wins_and_is_word_bounded():
    kw = cs.compile_keywords({"A": ["ring"], "B": ["ring", "spring"]})
    assert cs.classify_category("Spring Mattress", kw) == "B"      # 'ring' inside 'Spring' is not a hit for A
    assert cs.classify_category("Gold ring", kw) == "A"


# ── cross-reference matcher ──────────────────────────────────────────────────

def _r(row_num, title, url="", **extra):
    base = {"row_num": row_num, "title": title, "costco_url": url, "product_id": cs.product_id(url),
            "norm_url": cs.norm_url(url), "norm_title": cs._norm_title(title), "status": "SCORED",
            "category": "Pharmacy", "costco_cost": "", "sale_info": "", "regular_price": "",
            "ebay_price": "", "fee_rate": "", "ship_cost": ""}
    base.update(extra)
    return base


def _i(title, url):
    return {"title": title, "url": url, "product_id": cs.product_id(url), "list_savings": 5.0, "list_regular": 50.0}


def test_match_by_product_id_ignores_slug_and_query():
    rows = [_r(4, "Energy Shot 24 ct", "https://www.costco.com/kirkland-energy-shot.product.4000099948.html")]
    m = cs.match_items([_i("Energy Shot", "https://www.costco.com/.product.4000099948.html?rf=1")], rows)
    assert [(i["title"], r["row_num"], how) for i, r, how in m["tracked"]] == [("Energy Shot", 4, "product_id")]
    assert not m["new"] and not m["possible"]


def test_match_by_normalised_url():
    rows = [_r(5, "Widget", "https://www.costco.com/widget-page.html")]
    m = cs.match_items([_i("Other Title", "https://www.costco.com/Widget-Page.html?x=1")], rows)
    assert m["tracked"][0][2] == "url"


def test_match_by_exact_normalised_title():
    rows = [_r(6, "Kirkland Signature Fish Oil, 1000 mg - 400 Softgels", "")]
    m = cs.match_items([_i("kirkland signature fish oil 1000 mg 400 softgels", "https://www.costco.com/.product.111.html")], rows)
    assert m["tracked"][0][2] == "title" and m["tracked"][0][1]["row_num"] == 6


def test_close_title_is_held_back_not_new():
    rows = [_r(7, "Kirkland Signature Daily Multi 500 Tablets", "https://www.costco.com/.product.1.html")]
    m = cs.match_items([_i("Kirkland Signature Daily Multi 500 Tablets Bonus", "https://www.costco.com/.product.2.html")], rows)
    assert not m["new"] and not m["tracked"]
    assert m["possible"][0][1]["row_num"] == 7                       # jaccard 6/7 >= 0.8


def test_tie_and_shared_row_are_ambiguous():
    rows = [_r(8, "Ninja Blender Pro 1000 W", "https://www.costco.com/.product.10.html"),
            _r(9, "Ninja Blender Pro 1000 W", "https://www.costco.com/.product.11.html")]
    tie = cs.match_items([_i("Ninja Blender Pro 1000 W Plus", "https://www.costco.com/.product.99.html")], rows)
    assert len(tie["ambiguous"]) == 1 and not tie["possible"] and not tie["new"]

    rows = [_r(8, "Ninja Blender Pro 1000 W", "https://www.costco.com/.product.10.html")]
    two = cs.match_items([_i("Ninja Blender Pro 1000 W Plus", "https://www.costco.com/.product.98.html"),
                          _i("Ninja Blender Pro 1000 W Max", "https://www.costco.com/.product.99.html")], rows)
    assert len(two["ambiguous"]) == 2 and not two["possible"]        # one row can't be two products


def test_dissimilar_is_new_and_duplicate_items_collapse():
    rows = [_r(4, "Kirkland Signature Fish Oil", "https://www.costco.com/.product.1.html")]
    it = _i("Dyson V15 Cordless Vacuum", "https://www.costco.com/.product.7.html")
    m = cs.match_items([it, dict(it)], rows)
    assert len(m["new"]) == 1 and not m["tracked"]


# ── alert text ───────────────────────────────────────────────────────────────

def test_alert_line_format():
    line = cs.alert_line({"title": "Energy Shot", "price": 31.99, "regular": 39.99, "end": "10/18/26",
                          "net": 7.2, "new_row": False})
    assert line == "🔔 Energy Shot on sale — $31.99 (was $39.99, ends 10/18) · net +$7.20"


def test_alert_line_new_row_negative_net_escapes_html():
    line = cs.alert_line({"title": "A & B <b>", "price": 10.0, "regular": None, "end": None,
                          "net": -1.5, "new_row": True})
    assert line == "🆕 A &amp; B &lt;b&gt; on sale — $10.00 · net -$1.50 — added PENDING"


def test_format_alert_none_and_cap():
    assert cs.format_alert([]) is None
    e = {"title": "x", "price": 1.0, "regular": None, "end": None, "net": None, "new_row": False}
    msg = cs.format_alert([e] * 20, top=15)
    assert len(msg.splitlines()) == 16 and msg.endswith("…and 5 more")


def test_net_profit_needs_ebay_price_and_fee():
    assert cs.net_profit(_r(4, "x", ebay_price="60", fee_rate="0.1325", ship_cost="5"), 40.0) == round(60 - 40 - 60 * 0.1325 - 5, 2)
    assert cs.net_profit(_r(4, "x", ebay_price="", fee_rate="0.1325"), 40.0) is None
    assert cs.net_profit(_r(4, "x", ebay_price="60", fee_rate=""), 40.0) is None


# ── run_savings (mocked browser / sheet / Telegram) ──────────────────────────

WIDTH = sch.col_to_idx("AW") + 1


def _row(**cells):
    r = [""] * WIDTH
    for key, val in cells.items():
        r[sch.col_to_idx(COL[key])] = val
    return r


def _scrape(price, orig=None, savings=None, end="10/18/26", on_sale=True, title=None):
    return {"price": price, "on_sale": on_sale, "original_price": orig, "sale_savings": savings,
            "sale_expires": end, "stock_status": "In Stock", "title": title, "coupon_type": "MFR",
            "coupon_label": "Manufacturer Coupon"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    ctx = {"writes": [], "sent": [], "appended": [], "logged": [], "scraped": [],
           "sheet": [], "items": [], "scrapes": {}, "banner": "10/18/26",
           "search": [], "smeta": {"pages": 0, "stopped": ""}}

    @contextmanager
    def fake_browser():
        yield object()

    def fake_scrape(url, page=None):
        ctx["scraped"].append(url)
        return ctx["scrapes"].get(url, {"price": None, "stock_status": "CHECK FAILED"})

    monkeypatch.setattr(sch.sys, "platform", "win32")
    monkeypatch.setattr(sch, "make_browser", fake_browser)
    monkeypatch.setattr(sch, "scrape_costco", fake_scrape)
    monkeypatch.setattr(sch, "read_sheet", lambda svc, rng: ctx["sheet"])
    monkeypatch.setattr(sch, "safe_write_row", lambda svc, name, row, ups: ctx["writes"].append((row, dict(ups))))
    monkeypatch.setattr(sch, "log_sale", lambda *a, **k: ctx["logged"].append(a[1]) or True)
    monkeypatch.setattr(sch, "_append_pending_rows", lambda svc, name, prods, c: ctx["appended"].extend(prods))
    monkeypatch.setattr(sch, "_send_telegram", lambda t, c, text: ctx["sent"].append(text) or True)
    monkeypatch.setattr(sch, "ensure_grid_columns", lambda *a, **k: False)
    monkeypatch.setattr(sch.time, "sleep", lambda s: None)
    monkeypatch.setattr(sch, "SAVINGS_ALERT_STATE", str(tmp_path / "savings_alert.json"))
    monkeypatch.setattr(sch, "SAVINGS_SEEN_STATE", str(tmp_path / "savings_seen.json"))
    monkeypatch.setattr("tools.costco_scraper.refresh_session", lambda page: None)
    monkeypatch.setattr(cs, "scrape_savings_listing", lambda page, url=None: (ctx["items"], ctx["banner"]))
    monkeypatch.setattr(cs, "scrape_search_listing",
                        lambda page, url=None, max_pages=70, refresh=None: (ctx["search"], ctx["smeta"]))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    ctx["run"] = lambda **kw: sch.run_savings(CONFIG, COL, object(), "Product Tracker", 4, 500, **kw)
    return ctx


D3_URL = "https://www.costco.com/kirkland-d3.product.4000100002.html"
LIST_D3 = "https://www.costco.com/.product.4000100002.html"


def test_tracked_new_sale_updates_only_sale_columns_and_alerts_once(env, tmp_path):
    env["sheet"] = [_row(status="ACTIVE", title="Kirkland Signature Extra Strength D3 50 mcg 600 Softgels",
                         category="Pharmacy", costco_url=D3_URL, costco_cost="43.99")]
    env["items"] = [_i("Extra Strength D3", LIST_D3)]
    env["scrapes"][LIST_D3] = _scrape(35.99, orig=43.99, savings=8.0)

    res = env["run"]()
    assert res["status"] == "ok"
    (row, ups), = env["writes"]
    assert row == 4
    assert ups == {COL["costco_cost"]: 35.99, COL["sale_info"]: "🔥 -$8 ends 10/18/26",
                   COL["regular_price"]: 43.99}
    assert COL["status"] not in ups and COL["price_change"] not in ups     # never status, never col P
    assert len(env["sent"]) == 1
    assert "on sale — $35.99 (was $43.99, ends 10/18)" in env["sent"][0]
    assert env["logged"] == ["Kirkland Signature Extra Strength D3 50 mcg 600 Softgels"]
    assert "updated 1" in res["notes"] and "alerted 1" in res["notes"]

    env["run"]()                                    # same sheet again (write not reflected) -> deduped alert
    assert len(env["sent"]) == 1


def test_tracked_already_on_sale_is_silent_and_unwritten(env):
    env["sheet"] = [_row(status="ACTIVE", title="Kirkland Extra Strength D3", costco_url=D3_URL,
                         costco_cost="35.99", sale_info="🔥 -$8 ends 10/18/26", regular_price="43.99")]
    env["items"] = [_i("Extra Strength D3", LIST_D3)]
    env["scrapes"][LIST_D3] = _scrape(35.99, orig=43.99, savings=8.0)
    res = env["run"]()
    assert env["writes"] == [] and env["sent"] == []
    assert "unchanged 1" in res["notes"]


def test_listing_hint_not_confirmed_by_price_api_is_ignored(env):
    env["sheet"] = [_row(status="SCORED", title="Kirkland Extra Strength D3", costco_url=D3_URL, costco_cost="43.99")]
    env["items"] = [_i("Extra Strength D3", LIST_D3)]
    env["scrapes"][LIST_D3] = _scrape(43.99, on_sale=False)
    res = env["run"]()
    assert env["writes"] == [] and env["sent"] == [] and "not_on_sale 1" in res["notes"]


def test_new_in_category_added_pending_off_category_never_scraped(env):
    fish = "https://www.costco.com/.product.4000111111.html"
    tv = "https://www.costco.com/.product.4000222222.html"
    env["items"] = [_i("Kirkland Signature Fish Oil 1000 mg 400 Softgels", fish),
                    _i("Samsung 55\" Class 4K Smart TV", tv)]
    env["scrapes"][fish] = _scrape(19.99, orig=24.99, savings=5.0,
                                   title="Kirkland Signature Fish Oil 1000 mg, 400 Softgels")
    res = env["run"]()
    assert env["scraped"] == [fish]
    (p,) = env["appended"]
    assert (p["category"], p["url"], p["price"]) == ("Pharmacy", fish, 19.99)
    assert p["sale_info"] == "🔥 -$5 ends 10/18/26" and p["regular_price"] == 24.99
    assert env["writes"] == []                        # nothing tracked -> no cell writes
    assert env["sent"][0].startswith("🆕") and "added PENDING" in env["sent"][0]
    assert "off-category 1" in res["notes"] and res["new_products"] == 1


def test_add_limit_caps_new_rows(env):
    urls = [f"https://www.costco.com/.product.40001{i}.html" for i in range(5)]
    env["items"] = [_i(f"Vitamin C Brand{n} 500 Tablets", u) for n, u in enumerate(urls)]
    for n, u in enumerate(urls):
        env["scrapes"][u] = _scrape(10.0, orig=12.0, savings=2.0, title=f"Vitamin C Brand{n} 500 Tablets")
    res = env["run"](add_limit=2)
    assert len(env["appended"]) == 2 and res["new_products"] == 2
    assert len(env["scraped"]) == 2                   # the cap stops scraping too


def test_scrape_limit_caps_pages_opened_tracked_first(env):
    rows, items = [], []
    for n in range(3):
        u = f"https://www.costco.com/.product.400900{n}.html"
        rows.append(_row(status="SCORED", title=f"Tracked Thing {n} unique", costco_url=u, costco_cost="10"))
        items.append(_i(f"Tracked Thing {n} unique", u))
        env["scrapes"][u] = _scrape(8.0, orig=10.0, savings=2.0)
    env["sheet"], env["items"] = rows, items
    res = env["run"](limit=2)
    assert len(env["scraped"]) == 2 and "over budget 1" in res["notes"]


def test_close_match_is_never_scraped_or_added(env):
    env["sheet"] = [_row(status="SCORED", title="Kirkland Signature Daily Multi 500 Tablets",
                         costco_url="https://www.costco.com/.product.1.html")]
    env["items"] = [_i("Kirkland Signature Daily Multi 500 Tablets Bonus", "https://www.costco.com/.product.2.html")]
    res = env["run"]()
    assert env["scraped"] == [] and env["appended"] == [] and "1 possible" in res["notes"]


def test_dry_run_writes_nothing_and_prints_plan(env, capsys, tmp_path):
    env["sheet"] = [_row(status="ACTIVE", title="Kirkland Extra Strength D3", costco_url=D3_URL, costco_cost="43.99")]
    fish = "https://www.costco.com/.product.4000111111.html"
    env["items"] = [_i("Extra Strength D3", LIST_D3), _i("Fish Oil 400 Softgels", fish)]
    env["scrapes"][LIST_D3] = _scrape(35.99, orig=43.99, savings=8.0)
    env["scrapes"][fish] = _scrape(19.99, orig=24.99, savings=5.0, title="Kirkland Fish Oil 400 Softgels")
    res = env["run"](dry_run=True)
    out = capsys.readouterr().out
    assert "UPDATE row 4" in out and "[NEW SALE]" in out and "ADD PENDING [Pharmacy]" in out
    assert "🔔 Kirkland Extra Strength D3 on sale" in out
    assert env["writes"] == [] and env["appended"] == [] and env["sent"] == [] and env["logged"] == []
    assert not os.path.exists(sch.SAVINGS_ALERT_STATE)
    assert res["notes"].startswith("[dry-run] ")


def test_empty_listing_is_an_error_and_alerts(env):
    env["items"] = []
    res = env["run"]()
    assert res["status"] == "error" and "0 items" in res["errors"]
    assert env["sent"] and env["sent"][0].startswith("⚠️")


def test_failed_scrape_is_counted_not_written(env):
    env["sheet"] = [_row(status="SCORED", title="Kirkland Extra Strength D3", costco_url=D3_URL, costco_cost="43.99")]
    env["items"] = [_i("Extra Strength D3", LIST_D3)]                # no scrape result -> CHECK FAILED
    res = env["run"]()
    assert env["writes"] == [] and "failed 1" in res["notes"]


def test_non_windows_is_skipped(env, monkeypatch):
    monkeypatch.setattr(sch.sys, "platform", "linux")
    res = env["run"]()
    assert res["status"] == "ok" and "non-Windows" in res["notes"] and env["scraped"] == []


# ══ Phase 3: the "OFF" search listing ═══════════════════════════════════════════════════════

from datetime import datetime  # noqa: E402

_FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "search_results_sample.json")
SEARCH_RESULTS = json.load(open(_FIX, encoding="utf-8"))["results"]
PATHS = cs.compile_keywords(CONFIG["business"]["savings"]["paths"])


def test_parse_search_result_real_shape():
    it = cs.parse_search_result(SEARCH_RESULTS[0])                 # Ninja Combi, real API payload
    assert it["product_id"] == "4000290289" and it["source"] == "search"
    assert it["url"] == "https://www.costco.com/.product.4000290289.html"       # canonical, not /p/-/slug/id
    assert (it["list_sale"], it["list_regular"], it["list_savings"]) == (159.99, 199.99, 40.0)
    assert it["category_path"] == "Appliances > Small Kitchen Appliances > Air Fryers"
    assert it["section"] == "Appliances" and it["promo_short"] == "$40 OFF"
    assert it["promo_end"] == "9/30/26" and "manufacturer's savings" in it["promo_text"]
    assert "<a" not in it["promo_text"]                                          # html stripped


def test_parse_search_result_uses_store_847_price_not_generic():
    it = cs.parse_search_result(SEARCH_RESULTS[1])                 # generic price == originalPrice here
    assert (it["list_sale"], it["list_regular"]) == (1199.99, 1899.99)


def test_parse_search_result_skips_unusable_entries():
    assert cs.parse_search_result({"id": "", "product": {"title": "x"}}) is None
    assert cs.parse_search_result({"id": "1", "product": {"title": ""}}) is None
    it = cs.parse_search_result({"id": "9", "product": {"title": "No prices", "categories": []}})
    assert it["list_sale"] is None and it["category_path"] == "" and not cs.has_discount_hint(it)


def test_promo_end_picks_earliest_and_handles_absence():
    txt = "$5 savings valid 9/1/26 through 10/18/26. Also valid 9/14/26 through 9/27/26."
    assert cs.promo_end(txt) == "9/27/26"
    assert cs.promo_end("no dates") is None and cs.promo_end(None) is None


@pytest.mark.parametrize("path,cat", [
    ("Health & Personal Care > Vitamins, Herbals & Dietary Supplements > Fish Oil & Omega-3", "Pharmacy"),
    ("Health & Personal Care > Vitamins, Herbals & Dietary Supplements > Energy Drinks", "Pharmacy"),
    ("Health & Personal Care > Health & Medicines > Probiotics", "Pharmacy"),
    ("Appliances > Small Kitchen Appliances > Air Fryers", "Small Appliances"),
    ("Appliances > Small Kitchen Appliances > Coffee, Tea & Espresso Makers > Single Serve Coffee Makers", "Small Appliances"),
    ("Patio, Lawn & Garden > Patio & Outdoor Furniture > Outdoor Patio Conversation Sets", "Outdoor Furniture"),
    ("Jewelry > Rings", "Jewelry"),
    ("Jewelry > Watches", "Watches"),
    ("Gold > Bars & Rounds", "Precious Metals"),
])
def test_classify_by_category_path(path, cat):
    assert cs.classify_category(path, PATHS) == cat


@pytest.mark.parametrize("path", [
    "Appliances > Refrigerators",
    "Appliances > Small Kitchen Appliances > Water Coolers & Dispensers",
    "Grocery & Household Essentials > Coffee > K-Cups, Coffee Pods & Capsules",
    "Health & Personal Care > Health & Medicines > Pain & Fever",
    "Health & Personal Care > Nutrition > Protein",
    "Patio, Lawn & Garden > Patio Covers & Shade Structures > Gazebos",
    "Patio, Lawn & Garden > Outdoor Storage Sheds",
    "Home & Kitchen > Cookware & Bakeware > Cookware Sets",
    "Furniture > Living Room Furniture > Sectional Sofas",
    "Silverware > Flatware",                          # 'silver' prefix must be a whole word
    "",
])
def test_off_category_paths(path):
    assert cs.classify_category(path, PATHS) is None


def test_search_items_are_classified_by_path_not_title():
    kw = cs.compile_keywords(CONFIG["business"]["savings"]["keywords"])
    k_cup = {"source": "search", "title": "Lavazza Espresso Machine Coffee Pods", "category_path":
             "Grocery & Household Essentials > Coffee > K-Cups, Coffee Pods & Capsules"}
    assert cs.classify_category(k_cup["title"], kw) == "Small Appliances"     # the title alone WOULD match
    assert cs.classify_item(k_cup, kw, PATHS) is None                         # the path says grocery
    page_card = {"source": "page", "title": "Kirkland Signature Fish Oil 400 Softgels"}
    assert cs.classify_item(page_card, kw, PATHS) == "Pharmacy"               # no path -> keywords


def test_merge_items_search_version_wins_and_order_is_stable():
    page = {"product_id": "1", "title": "A", "source": "page"}
    search = {"product_id": "1", "title": "A", "source": "search", "category_path": "X > Y"}
    only_page = {"product_id": "2", "title": "B", "source": "page"}
    merged = cs.merge_items([search], [page, only_page])
    assert [i["product_id"] for i in merged] == ["1", "2"] and merged[0]["source"] == "search"
    assert cs.merge_items([page], [search])[0]["source"] == "search"          # order of sources irrelevant


def test_discount_helpers():
    assert cs.discount_pct(80.0, 100.0) == 20.0
    assert cs.discount_pct(100.0, 100.0) == 0.0 and cs.discount_pct(None, 100.0) == 0.0
    assert cs.has_discount_hint({"list_sale": 8.0, "list_regular": 10.0})
    assert not cs.has_discount_hint({"list_sale": 10.0, "list_regular": 10.0})
    assert cs.has_discount_hint({"list_savings": 3.0})                        # 'Save $3' card, no prices


def test_seen_cache_cooldown_and_pruning(tmp_path):
    path = str(tmp_path / "seen.json")
    today = datetime(2026, 9, 24)
    seen = cs.save_seen(path, {}, {"111": "not_on_sale"}, today)
    assert cs.recently_rejected(seen, "111", today, 3)
    assert cs.recently_rejected(cs.load_seen(path), "111", datetime(2026, 9, 26), 3)     # persisted
    assert not cs.recently_rejected(seen, "111", datetime(2026, 9, 27), 3)               # cooldown over
    assert not cs.recently_rejected(seen, "111", today, 0)                               # 0 = disabled
    assert not cs.recently_rejected(seen, "999", today, 3)
    pruned = cs.save_seen(path, seen, {"222": "below_min"}, datetime(2026, 11, 1))
    assert "111" not in pruned and "222" in pruned                                        # >30d dropped


# ── crawler against a fake Playwright page ───────────────────────────────────

class _Req:
    def __init__(self, offset):
        self.method, self.post_data = "POST", json.dumps({"query": "OFF", "offset": offset})


class _Resp:
    def __init__(self, offset, n_results, total, status=200, fail=False):
        self.url, self.request, self.status = "https://gdx-api.costco.com/catalog/search/api/v1/search", _Req(offset), status
        self._body = {"searchResult": {"totalSize": total, "results": [
            {"id": str(offset + i + 1), "product": {"title": f"Item {offset + i + 1}", "categories": ["A > B"]},
             "variantRollupValues": {}} for i in range(n_results)]}}
        self._fail = fail

    def json(self):
        if self._fail:
            raise ValueError("bad json")
        return self._body


class _Ctx:
    def __init__(self, page, pred):
        self.page, self.pred = page, pred

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def value(self):
        if self.page.last is None or not self.pred(self.page.last):
            raise TimeoutError("no matching response")
        return self.page.last


class _FakePage:
    """goto('...&currentPage=N') 'fires' the search response for offset (N-1)*24."""
    def __init__(self, total, bad_pages=(), page_size=24):
        self.total, self.bad, self.last, self.urls, self.size = total, set(bad_pages), None, [], page_size

    def expect_response(self, pred, timeout=0):
        return _Ctx(self, pred)

    def goto(self, url, **kw):
        self.urls.append(url)
        n = int(url.split("currentPage=")[1]) if "currentPage=" in url else 1
        if n in self.bad:
            self.last = None
            return type("R", (), {"status": 200})()
        off = (n - 1) * self.size
        self.last = _Resp(off, max(0, min(self.size, self.total - off)), self.total)
        return type("R", (), {"status": 200})()

    def wait_for_timeout(self, ms):
        pass


def test_crawl_paginates_by_current_page_until_total():
    page = _FakePage(total=50)                                     # 24 + 24 + 2
    items, meta = cs.scrape_search_listing(page, "https://x/s?keyword=OFF&dept=All", max_pages=70)
    assert len(items) == 50 and meta == {"total": 50, "pages": 3, "failed": 0, "stopped": ""}
    assert page.urls == ["https://x/s?keyword=OFF&dept=All", "https://x/s?keyword=OFF&dept=All&currentPage=2",
                         "https://x/s?keyword=OFF&dept=All&currentPage=3"]


def test_crawl_honours_page_budget():
    page = _FakePage(total=500)
    items, meta = cs.scrape_search_listing(page, "https://x/s?k=1", max_pages=2)
    assert len(items) == 48 and meta["pages"] == 2 and "page budget 2 of 21" in meta["stopped"]


def test_crawl_refreshes_session_every_20_pages():
    calls = []
    page = _FakePage(total=24 * 45)
    cs.scrape_search_listing(page, "https://x/s?k=1", max_pages=45, refresh=lambda p: calls.append(1))
    assert len(calls) == 2                                          # before page 21 and page 41


def test_crawl_survives_one_bad_page_but_stops_after_two_in_a_row():
    items, meta = cs.scrape_search_listing(_FakePage(total=24 * 6, bad_pages={3}), "https://x/s?k=1")
    assert meta["failed"] == 1 and meta["pages"] == 5 and len(items) == 24 * 5 and not meta["stopped"]
    items, meta = cs.scrape_search_listing(_FakePage(total=24 * 6, bad_pages={3, 4}), "https://x/s?k=1")
    assert meta["pages"] == 2 and "2 consecutive failures at page 4" in meta["stopped"]


def test_crawl_page_one_failure_returns_nothing_without_raising():
    items, meta = cs.scrape_search_listing(_FakePage(total=100, bad_pages={1, 2}), "https://x/s?k=1")
    assert items == [] and meta["pages"] == 0


# ── run_savings with the search source ───────────────────────────────────────

def _s(pid, title, path, sale=None, regular=None, end=None):
    return {"title": title, "url": cs.canonical_url(pid), "product_id": pid, "source": "search",
            "category_path": path, "section": path.split(" > ")[0], "list_sale": sale, "list_regular": regular,
            "list_savings": round(regular - sale, 2) if sale and regular and regular > sale else None,
            "promo_end": end, "promo_short": "", "promo_text": ""}


PH = "Health & Personal Care > Vitamins, Herbals & Dietary Supplements > Herbal Supplements"


def _arm(env, pid, price, orig, savings, **kw):
    env["scrapes"][cs.canonical_url(pid)] = _scrape(price, orig=orig, savings=savings, **kw)


def test_search_new_item_added_with_category_path_in_tier_summary(env):
    env["search"] = [_s("501", "Zinc Plus 100 ct", PH, 15.99, 19.99)]
    _arm(env, "501", 15.99, 19.99, 4.0, title="Zinc Plus 100 Tablets")
    res = env["run"]()
    (p,) = env["appended"]
    assert p["category"] == "Pharmacy" and p["url"] == cs.canonical_url("501") and p["price"] == 15.99
    assert "Costco savings search" in p["tier_summary"] and "Herbal Supplements" in p["tier_summary"]
    assert "found 1 (0 page + 1 search" in res["notes"] and res["new_products"] == 1


def test_off_category_search_items_are_never_scraped(env):
    env["search"] = [_s("601", "LG Refrigerator", "Appliances > Refrigerators", 999.0, 1299.0),
                     _s("602", "Espresso K-Cups", "Grocery & Household Essentials > Coffee > K-Cups, Coffee Pods & Capsules", 30.0, 40.0)]
    res = env["run"]()
    assert env["scraped"] == [] and env["appended"] == []
    assert "off-category 2" in res["notes"] and "in-category 0" in res["notes"]


def test_items_without_a_listed_discount_or_a_trivial_one_are_dropped_before_scraping(env):
    env["search"] = [_s("701", "Vitamin C 500 ct", PH, None, None),            # listing shows no markdown
                     _s("702", "Vitamin D 500 ct", PH, 19.99, 20.99),          # 4.8% < 5% floor
                     _s("703", "Vitamin E 500 ct", PH, 16.0, 20.0)]            # 20% -> goes through
    _arm(env, "703", 16.0, 20.0, 4.0)
    res = env["run"]()
    assert env["scraped"] == [cs.canonical_url("703")]
    assert "no_hint 1" in res["notes"] and "below_min 1" in res["notes"] and res["new_products"] == 1


def test_api_confirmed_discount_below_floor_is_rejected_and_not_rescraped_within_cooldown(env):
    env["search"] = [_s("801", "Magnesium 250 ct", PH, 15.0, 20.0)]           # hint says 25% ...
    _arm(env, "801", 19.5, 20.0, 0.5)                                         # ... API says 2.5%
    res = env["run"]()
    assert env["appended"] == [] and "below_min 1" in res["notes"]
    env["scraped"].clear()
    res = env["run"]()                                                        # next day: skipped, budget saved
    assert env["scraped"] == [] and "cooldown 1" in res["notes"]


def test_hint_not_confirmed_by_price_api_is_recorded_as_rejected(env):
    env["search"] = [_s("901", "Turmeric 90 ct", PH, 15.0, 20.0)]
    env["scrapes"][cs.canonical_url("901")] = _scrape(20.0, on_sale=False)
    env["run"]()
    assert env["appended"] == [] and "901" in cs.load_seen(sch.SAVINGS_SEEN_STATE)
    env["scraped"].clear()
    env["run"]()
    assert env["scraped"] == []


def test_dry_run_does_not_record_rejects(env):
    env["search"] = [_s("901", "Turmeric 90 ct", PH, 15.0, 20.0)]
    env["scrapes"][cs.canonical_url("901")] = _scrape(20.0, on_sale=False)
    env["run"](dry_run=True)
    assert not os.path.exists(sch.SAVINGS_SEEN_STATE)


def test_search_source_also_finds_tracked_rows_the_event_page_misses(env):
    d3 = "https://www.costco.com/kirkland-d3.product.4000100002.html"
    env["sheet"] = [_row(status="ACTIVE", title="Kirkland Extra Strength D3", costco_url=d3, costco_cost="43.99")]
    env["search"] = [_s("4000100002", "Extra Strength D3", PH, 35.99, 43.99, end="10/2/26")]
    _arm(env, "4000100002", 35.99, 43.99, 8.0, end=None)                       # API carries no end date
    res = env["run"]()
    (row, ups), = env["writes"]
    assert row == 4 and ups[COL["sale_info"]] == "🔥 -$8 ends 10/2/26"       # search promo date is the fallback
    assert "tracked 1" in res["notes"] and len(env["sent"]) == 1


def test_max_pending_guard_stops_adds_and_scrapes(env, monkeypatch):
    monkeypatch.setitem(CONFIG["business"]["savings"], "max_pending", 2)
    env["sheet"] = [_row(status="PENDING", title=f"Pending thing {i} alpha", costco_url=f"https://x/.product.{i}.html")
                    for i in range(2)]
    env["search"] = [_s("1001", "Calcium 500 ct", PH, 15.0, 20.0)]
    res = env["run"]()
    assert env["scraped"] == [] and env["appended"] == [] and "backlog 1" in res["notes"]


def test_max_pending_leaves_only_the_remaining_room(env, monkeypatch):
    monkeypatch.setitem(CONFIG["business"]["savings"], "max_pending", 3)
    env["sheet"] = [_row(status="PENDING", title="Pending thing alpha", costco_url="https://x/.product.1.html")]
    env["search"] = [_s(f"11{i}", f"Multivitamin Brand{i} 100 ct", PH, 15.0, 20.0) for i in range(4)]
    for i in range(4):
        _arm(env, f"11{i}", 15.0, 20.0, 5.0, title=f"Multivitamin Brand{i} 100 ct")
    res = env["run"]()
    assert len(env["appended"]) == 2 and res["new_products"] == 2             # 3 cap - 1 pending


def test_search_only_and_page_only_items_merge_and_search_count_is_reported(env):
    env["items"] = [_i("Kirkland Fish Oil Softgels 400", "https://www.costco.com/.product.2001.html")]
    env["search"] = [_s("2002", "Biotin 5000 mcg 100 ct", PH, 8.0, 10.0)]
    env["smeta"] = {"pages": 67, "stopped": "page budget 60 of 67"}
    _arm(env, "2001", 15.0, 20.0, 5.0, title="Kirkland Fish Oil Softgels 400")
    _arm(env, "2002", 8.0, 10.0, 2.0, title="Biotin 5000 mcg 100 ct")
    res = env["run"]()
    assert res["new_products"] == 2
    assert "found 2 (1 page + 1 search [search 67 pages, page budget 60 of 67])" in res["notes"]


def test_search_listing_failure_degrades_to_the_event_page(env):
    d3 = "https://www.costco.com/kirkland-d3.product.4000100002.html"
    env["sheet"] = [_row(status="SCORED", title="Kirkland Extra Strength D3", costco_url=d3, costco_cost="43.99")]
    env["items"] = [_i("Extra Strength D3", LIST_D3)]
    env["search"], env["smeta"] = [], {"pages": 0, "stopped": "2 consecutive failures at page 2"}
    env["scrapes"][LIST_D3] = _scrape(35.99, orig=43.99, savings=8.0)
    res = env["run"]()
    assert res["status"] == "ok" and len(env["writes"]) == 1
    assert "2 consecutive failures at page 2" in res["notes"]


def test_never_priced_pending_row_is_updated_but_not_alerted(env):
    env["sheet"] = [_row(status="PENDING", title="Kirkland Extra Strength D3", costco_url=D3_URL, costco_cost="")]
    env["search"] = [_s("4000100002", "Extra Strength D3", PH, 35.99, 43.99)]
    _arm(env, "4000100002", 35.99, 43.99, 8.0)
    res = env["run"]()
    assert len(env["writes"]) == 1 and env["sent"] == [] and "alerted 0" in res["notes"]
    env["sheet"] = [_row(status="ACTIVE", title="Kirkland Extra Strength D3", costco_url=D3_URL, costco_cost="")]
    env["run"]()                                     # a blank-G ACTIVE row is still worth a heads-up
    assert len(env["sent"]) == 1
