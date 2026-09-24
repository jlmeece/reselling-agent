"""run_sale_digest + run_sale_refresh (scheduler modes), mocked sheet / Telegram / browser."""
from contextlib import contextmanager
from datetime import datetime

import pytest

from agents import scheduler as sch

COL = sch.load_col_map()
CONFIG = {"business": {"sale_warn_hours": 48}}
NOW = datetime(2026, 9, 24, 12, 0)


class _FixedDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def _row(**cells):
    row = []
    for key, val in cells.items():
        idx = sch.col_to_idx(COL[key])
        while len(row) <= idx:
            row.append("")
        row[idx] = val
    return row


def _sale_row(title="Kirkland Signature Energy Shot", status="ACTIVE", **kw):
    base = dict(title=title, status=status, costco_cost="31.99", regular_price="39.99",
                sale_info="🔥 -$8 ends 10/18/26", ebay_price="41.48", fee_rate="0.1325",
                ship_cost="0", last_checked="2026-09-24 10:00",
                costco_url="https://www.costco.com/x.product.1.html", category="Pharmacy")
    base.update(kw)
    return _row(**base)


@pytest.fixture
def env(monkeypatch):
    sent = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setattr(sch, "datetime", _FixedDT)
    monkeypatch.setattr(sch, "_send_telegram", lambda t, c, m: sent.append((t, c, m)) or True)
    return sent


def _digest(monkeypatch, rows, dry_run=False):
    monkeypatch.setattr(sch, "read_sheet", lambda *a, **k: rows)
    return sch.run_sale_digest(CONFIG, COL, object(), "Product Tracker", 4, 500, dry_run=dry_run)


def test_live_run_sends_one_message(env, monkeypatch):
    res = _digest(monkeypatch, [_sale_row(), _sale_row(sale_info="")])
    assert len(env) == 1 and env[0][:2] == ("tok", "chat")
    assert env[0][2].startswith("🛒 <b>Sale Radar — 1 item on sale</b>")
    assert res["status"] == "ok" and "1 on sale" in res["notes"] and "errors" not in res


def test_dry_run_prints_and_never_sends(env, monkeypatch, capsys):
    res = _digest(monkeypatch, [_sale_row()], dry_run=True)
    out = capsys.readouterr().out
    assert env == []
    assert "Sale Radar — 1 item on sale" in out and "$31.99 (was $39.99, ends 10/18)" in out
    assert res["notes"].startswith("[dry-run] 1 on sale")


def test_nothing_on_sale_is_silent(env, monkeypatch, capsys):
    res = _digest(monkeypatch, [_sale_row(sale_info=""), _sale_row(costco_cost="", status="PENDING")])
    assert env == [] and capsys.readouterr().out == ""
    assert res["status"] == "ok" and res["notes"].startswith("0 on sale")


def test_only_bogus_badges_is_silent_but_reported(env, monkeypatch):
    res = _digest(monkeypatch, [_sale_row(costco_cost="14.99", regular_price="", sale_info="🔥 -$100"),
                                _sale_row(sale_info="🔥 -$8 ends 7/19/26")])
    assert env == []
    assert "expired 1" in res["notes"] and "implausible 1" in res["notes"]


def test_send_failure_or_missing_token_is_reported_not_raised(env, monkeypatch):
    monkeypatch.setattr(sch, "_send_telegram", lambda t, c, m: False)
    res = _digest(monkeypatch, [_sale_row()])
    assert res["status"] == "error" and "not delivered" in res["errors"]
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    assert _digest(monkeypatch, [_sale_row()])["status"] == "error"


# ── sale-refresh ─────────────────────────────────────────────────────────────

@pytest.fixture
def refresh(monkeypatch):
    calls = {"writes": [], "sales": [], "scraped": []}
    state = {"rows": [], "scrape": {}}
    monkeypatch.setattr(sch, "datetime", _FixedDT)
    monkeypatch.setattr(sch, "read_sheet", lambda *a, **k: state["rows"])

    @contextmanager
    def _browser():
        yield object()
    monkeypatch.setattr(sch, "make_browser", _browser)

    def _scrape(url, page=None):
        calls["scraped"].append(url)
        return state["scrape"]
    monkeypatch.setattr(sch, "scrape_costco", _scrape)
    monkeypatch.setattr(sch, "write_row_partial",
                        lambda svc, sheet, row, pairs: calls["writes"].append((row, dict(pairs))))
    monkeypatch.setattr(sch, "log_sale", lambda *a, **k: calls["sales"].append((a, k)) or True)
    monkeypatch.setattr(sch.time, "sleep", lambda s: None)
    monkeypatch.setattr(sch.sys, "platform", "win32")

    def run(rows, scrape, **kw):
        state["rows"], state["scrape"] = rows, scrape
        return sch.run_sale_refresh(CONFIG, COL, object(), "Product Tracker", 4, 500, **kw)
    return run, calls


def test_refresh_heals_bogus_badge_and_writes_only_g_x_aw(refresh):
    run, calls = refresh
    bogus = _sale_row(status="SCORED", costco_cost="14.99", regular_price="", sale_info="🔥 -$100 ends 9/30/26")
    res = run([bogus], {"price": 12.99, "stock_status": "In Stock", "on_sale": True, "original_price": 14.99,
                        "sale_savings": 2.0, "sale_expires": "9/30/26", "coupon_type": "MFR",
                        "coupon_label": "Manufacturer Coupon"})
    (row, w), = calls["writes"]
    assert row == 4 and set(w) == {COL["costco_cost"], COL["sale_info"], COL["regular_price"]}
    assert w[COL["costco_cost"]] == 12.99 and w[COL["regular_price"]] == 14.99
    assert w[COL["sale_info"]] == "🔥 -$2 ends 9/30/26"
    assert len(calls["sales"]) == 1 and calls["sales"][0][1]["coupon_type"] == "MFR"
    assert "1 on sale" in res["notes"]


def test_refresh_clears_a_false_positive_badge(refresh):
    run, calls = refresh
    res = run([_sale_row(status="READY", costco_cost="199.99", regular_price="", sale_info="🔥 -$60")],
              {"price": 199.99, "stock_status": "In Stock", "on_sale": False})
    (_r, w), = calls["writes"]
    assert w == {COL["costco_cost"]: 199.99, COL["sale_info"]: "", COL["regular_price"]: ""}
    assert calls["sales"] == [] and "1 badges cleared" in res["notes"]


def test_refresh_skips_verified_active_junk_and_urlless_rows(refresh):
    run, calls = refresh
    rows = [_sale_row(status="SCORED"),                              # verified (AW>G, future end)
            _sale_row(status="ACTIVE", sale_info="🔥 -$100"),        # ACTIVE belongs to the monitor
            _sale_row(status="AUDIT_REVIEW", sale_info="🔥 -$100"),
            _sale_row(status="SCORED", sale_info="🔥 -$100", costco_url=""),
            _sale_row(status="SCORED", sale_info="")]
    res = run(rows, {"price": 1.0, "on_sale": False})
    assert calls["scraped"] == [] and calls["writes"] == []
    assert res["notes"] == "sale-refresh: no unverified badges"


def test_refresh_respects_limit_and_statuses_and_skips_failed_scrapes(refresh):
    run, calls = refresh
    rows = [_sale_row(title=f"r{n}", status="WATCH", sale_info="🔥 -$100") for n in range(5)]
    run(rows, {"price": None, "stock_status": "CHECK FAILED", "on_sale": False}, limit=3)
    assert len(calls["scraped"]) == 3 and calls["writes"] == []             # failures never write
    calls["scraped"].clear()
    run(rows, {"price": 5.0, "on_sale": False}, limit=12, statuses={"SCORED"})
    assert calls["scraped"] == []                                            # WATCH not in the statuses
