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
           "sheet": [], "items": [], "scrapes": {}, "banner": "10/18/26"}

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
    monkeypatch.setattr("tools.costco_scraper.refresh_session", lambda page: None)
    monkeypatch.setattr(cs, "scrape_savings_listing", lambda page, url=None: (ctx["items"], ctx["banner"]))
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
    assert env["writes"] == [] and env["sent"] == [] and "not on sale per API 1" in res["notes"]


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
    assert "1 off-category" in res["notes"] and res["new_products"] == 1


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
