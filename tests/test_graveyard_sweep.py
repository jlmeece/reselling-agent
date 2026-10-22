"""graveyard-sweep: REVIVE only on a real price drop, and revived items go back on the main
sheet as PENDING unless their Costco URL is already there."""
import contextlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agents.scheduler as sch  # noqa: E402
import tools.graveyard_writer as gw  # noqa: E402

COL = {"costco_url": "R"}
URL_A = "https://www.costco.com/patio-set.product.4000111111.html"
URL_B = "https://www.costco.com/fish-oil.product.4000222222.html"


def _entry(row_num, url, cost="$100.00", title="Item"):
    return {"row_num": row_num, "costco_url": url, "last_checked": "", "cost": cost,
            "title": title, "category": "Outdoor Furniture", "sku": f"SKU{row_num}",
            "reason": "Negative net profit ($-3.00)", "date_removed": "2026-10-01"}


@pytest.fixture
def sweep(monkeypatch):
    st = {"entries": [], "scrapes": {}, "sheet_urls": [], "appended": [], "verdicts": [],
          "sent": [], "read_fails": False}
    monkeypatch.setattr(sch.sys, "platform", "win32")
    monkeypatch.setattr(gw, "read_graveyard", lambda svc: st["entries"])
    monkeypatch.setattr(gw, "setup_graveyard_tab", lambda svc: None)
    monkeypatch.setattr(gw, "write_graveyard_verdict",
                        lambda svc, row, date, verdict: st["verdicts"].append((row, verdict)))
    monkeypatch.setattr(sch, "make_browser", lambda: contextlib.nullcontext("page"))
    monkeypatch.setattr(sch, "scrape_costco", lambda url, page=None: st["scrapes"][url])
    monkeypatch.setattr(sch.time, "sleep", lambda s: None)

    def read_sheet(svc, rng):
        if st["read_fails"]:
            raise RuntimeError("sheet down")
        return [[u] for u in st["sheet_urls"]]

    monkeypatch.setattr(sch, "read_sheet", read_sheet)
    monkeypatch.setattr(sch, "_append_pending_rows",
                        lambda svc, sheet, products, col: st["appended"].extend(products))
    monkeypatch.setattr(sch, "_send_telegram",
                        lambda tok, chat, msg: st["sent"].append(msg) or True)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")

    def run(dry_run=False):
        st["result"] = sch.run_graveyard_sweep({}, COL, "svc", "Product Tracker", 4, 500,
                                               dry_run=dry_run)
        return st
    return st, run


def test_on_sale_badge_without_price_drop_is_alive_not_revive(sweep):
    st, run = sweep
    st["entries"] = [_entry(2, URL_A, cost="$100.00")]
    st["scrapes"] = {URL_A: {"price": 100.00, "on_sale": True}}
    run()
    assert st["verdicts"] == [(2, "ALIVE")]
    assert st["appended"] == [] and st["sent"] == []


def test_price_drop_revives_and_re_adds_pending_row(sweep):
    st, run = sweep
    st["entries"] = [_entry(2, URL_A, cost="$100.00", title="SunVilla Patio Set")]
    st["scrapes"] = {URL_A: {"price": 79.99, "on_sale": True}}
    run()
    assert st["verdicts"] == [(2, "REVIVE")]
    [p] = st["appended"]
    assert (p["title"], p["category"], p["url"], p["sku"]) == (
        "SunVilla Patio Set", "Outdoor Furniture", URL_A, "SKU2")
    assert p["price"] == 79.99                       # col G = current Costco price
    assert "was $100.00" in p["tier_summary"]
    [msg] = st["sent"]
    assert "was $100.00, now $79.99 · on sale · ➕ re-added as PENDING" in msg
    assert "re-added 1" in st["result"]["notes"]


def test_unknown_cost_never_revives(sweep):
    st, run = sweep
    st["entries"] = [_entry(2, URL_A, cost="")]
    st["scrapes"] = {URL_A: {"price": 10.00, "on_sale": True}}
    run()
    assert st["verdicts"] == [(2, "ALIVE")] and st["appended"] == []


def test_dedupe_skips_url_already_in_sheet_even_other_shape(sweep):
    st, run = sweep
    st["entries"] = [_entry(2, URL_A), _entry(3, URL_B, title="Fish Oil")]
    st["scrapes"] = {URL_A: {"price": 80.0}, URL_B: {"price": 80.0}}
    st["sheet_urls"] = ["COSTCO URL", "https://www.costco.com/p/-/patio-set/4000111111?x=1"]
    run()
    assert [p["url"] for p in st["appended"]] == [URL_B]
    assert "already in the sheet, not re-added" in st["sent"][0]


def test_dedupe_within_one_batch(sweep):
    st, run = sweep
    st["entries"] = [_entry(2, URL_A), _entry(3, URL_A)]
    st["scrapes"] = {URL_A: {"price": 80.0}}
    run()
    assert len(st["appended"]) == 1


def test_sheet_read_failure_adds_nothing(sweep):
    st, run = sweep
    st["entries"] = [_entry(2, URL_A)]
    st["scrapes"] = {URL_A: {"price": 80.0}}
    st["read_fails"] = True
    run()
    assert st["appended"] == []
    assert "NOT re-added (sheet unreadable" in st["sent"][0]


def test_dry_run_writes_and_sends_nothing(sweep, capsys):
    st, run = sweep
    st["entries"] = [_entry(2, URL_A)]
    st["scrapes"] = {URL_A: {"price": 80.0}}
    run(dry_run=True)
    assert st["appended"] == [] and st["verdicts"] == [] and st["sent"] == []
    assert "would re-add as PENDING" in capsys.readouterr().out
