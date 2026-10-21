"""Tests for tools/ebay_sync.py — every eBay HTTP call is mocked, never live."""
import os
import sys
import urllib.error
from xml.sax.saxutils import escape as _esc

import pytest
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import ebay_sync
from tools.ebay_sync import (
    alert_message, extract_item_id, fetch_active_listings, run_ebay_sync, summarize, sync,
)
from tools.sheet_writer import PROTECTED_COLS

COL = {"status": "A", "title": "C", "platform": "E", "ebay_price": "H",
       "ebay_listing_url": "Q", "units_sold": "U"}


# ── helpers ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(ebay_sync, "_sleep", lambda s: None)


@pytest.fixture(autouse=True)
def _alert_state(monkeypatch, tmp_path):
    """Never touch the real data/.ebay_sync_alert.json."""
    monkeypatch.setattr(ebay_sync, "ALERT_STATE_PATH", str(tmp_path / "alert.json"))


@pytest.fixture(autouse=True)
def _no_real_revise_log(monkeypatch):
    """Auto-reprice logs via tools.reprice.log_revise (Run Log tab) — never for real in tests."""
    import tools.reprice as reprice
    monkeypatch.setattr(reprice, "log_revise", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _digest_state(monkeypatch, tmp_path):
    """Never touch the real data/.ebay_sync_digest.json."""
    monkeypatch.setattr(ebay_sync, "DIGEST_STATE_PATH", str(tmp_path / "digest.json"))


@pytest.fixture
def logs():
    msgs = []
    hid = logger.add(lambda m: msgs.append(str(m)), level="DEBUG")
    yield msgs
    logger.remove(hid)


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("EBAY_APP_ID", "app")
    monkeypatch.setenv("EBAY_DEV_ID", "dev")
    monkeypatch.setenv("EBAY_CERT_ID", "cert")
    monkeypatch.setenv("EBAY_AUTH_TOKEN", "tok+en=")


class FakeResp:
    def __init__(self, body):
        self._body = body.encode() if isinstance(body, str) else body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _item(item_id, title="Item", qty=5, sold=1, price="29.99", watch=2, hits=10):
    """hits=None / watch=None omit the tag, as eBay does in the real ActiveList."""
    watch_xml = f"<WatchCount>{watch}</WatchCount>" if watch is not None else ""
    hits_xml = f"<HitCount>{hits}</HitCount>" if hits is not None else ""
    return (f"<Item><ItemID>{item_id}</ItemID><Title>{_esc(title)}</Title><Quantity>{qty}</Quantity>"
            f"<SellingStatus><CurrentPrice currencyID=\"USD\">{price}</CurrentPrice>"
            f"<QuantitySold>{sold}</QuantitySold></SellingStatus>"
            f"{watch_xml}{hits_xml}</Item>")


def _page(items, page=1, total=1, ack="Success"):
    return (f'<?xml version="1.0"?><GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
            f"<Ack>{ack}</Ack><ActiveList><ItemArray>{''.join(items)}</ItemArray>"
            f"<PaginationResult><TotalNumberOfPages>{total}</TotalNumberOfPages></PaginationResult>"
            f"</ActiveList></GetMyeBaySellingResponse>")


def _failure(code, msg="bad"):
    return (f'<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Failure</Ack>'
            f"<Errors><SeverityCode>Error</SeverityCode><ErrorCode>{code}</ErrorCode>"
            f"<ShortMessage>{msg}</ShortMessage></Errors></GetMyeBaySellingResponse>")


def _row(n, item_id="", title="Widget", status="ACTIVE", platform="eBay", price="$29.99", sold="0", url=None,
         **margin):
    """margin kwargs: buy_cost, costco_cost, fee_rate, ship_cost, ad_cost, sold_90d (all optional)."""
    if url is None:
        url = f"https://www.ebay.com/itm/{item_id}" if item_id else ""
    return {"row_num": n, "title": title, "status": status, "platform": platform,
            "ebay_price": price, "ebay_listing_url": url, "units_sold": sold, **margin}


def _listing(item_id, price=29.99, sold=0, qty=5, title="Widget"):
    return {"item_id": item_id, "title": title, "price": price, "quantity": qty,
            "quantity_sold": sold, "watch_count": 0, "view_count": None}


@pytest.fixture
def writes(monkeypatch):
    calls = []
    monkeypatch.setattr(ebay_sync, "safe_write_row",
                        lambda svc, sheet, row, pairs: calls.append((row, list(pairs))))
    return calls


def _sync(rows, listings, **kw):
    return sync("svc", rows, listings, sheet_name="Product Tracker", col_map=COL, **kw)


# ── extract_item_id ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("https://www.ebay.com/itm/123456789012", "123456789012"),
    ("http://www.ebay.com/itm/123456789012", "123456789012"),
    ("ebay.com/itm/Gold-Bar-1-oz/123456789012?hash=item1cbd&_trksid=p1", "123456789012"),
    ("https://www.ebay.com/itm/Some-Slug/123456789012", "123456789012"),
    ("https://www.ebay.com/itm/123456789012?hash=item1", "123456789012"),
    ("https://www.ebay.co.uk/itm/123456789012", "123456789012"),
    ("https://www.ebay.com/itm?item=123456789012", "123456789012"),
    ("123456789012", "123456789012"),
    ("  123456789012\n", "123456789012"),
])
def test_extract_item_id_valid(value, expected):
    assert extract_item_id(value) == expected


@pytest.mark.parametrize("value", [
    None, "", "   ", "garbage", "12345", "1234567890123456",          # too short / too long
    "https://www.amazon.com/dp/123456789012",                          # not eBay
    "=HYPERLINK(\"https://ebay.com/itm/123456789012\")",               # formula
    "https://www.ebay.com/str/mystore",                                # eBay, no item
    "https://ebay.us/abc123",                                          # short link
])
def test_extract_item_id_unparseable(value):
    assert extract_item_id(value) is None


# ── fetch_active_listings ─────────────────────────────────────────────────────

def test_no_token_returns_empty_cleanly_without_http(monkeypatch, logs):
    monkeypatch.setenv("EBAY_AUTH_TOKEN", "")
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("must not hit the network"))
    assert fetch_active_listings() == []
    assert any("ebay_sync skipped — no EBAY_AUTH_TOKEN" in m for m in logs)
    assert ebay_sync.get_last_error()["kind"] == "no_token"


def test_missing_keyset_skips(monkeypatch, creds):
    monkeypatch.setenv("EBAY_CERT_ID", "")
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("must not hit the network"))
    assert fetch_active_listings() == []
    err = ebay_sync.get_last_error()
    assert err["kind"] == "no_credentials" and "EBAY_CERT_ID" in err["message"]


def test_fetch_parses_pages_and_sends_correct_request(monkeypatch, creds):
    sent = []
    pages = iter([_page([_item("111111111111", "A & B", qty=5, sold=2)], total=2),
                  _page([_item("222222222222", "C", qty=1, sold=0, price="9.50"),
                         _item("111111111111", "dup")], total=2)])

    def fake_urlopen(req, timeout=None):
        sent.append(req)
        return FakeResp(next(pages))

    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen", fake_urlopen)
    out = fetch_active_listings()

    assert [l["item_id"] for l in out] == ["111111111111", "222222222222"]  # deduped, both pages
    assert out[0] == {"item_id": "111111111111", "title": "A & B", "price": 29.99, "quantity": 5,
                      "quantity_sold": 2, "watch_count": 2, "view_count": 10}
    assert out[1]["price"] == 9.5
    assert ebay_sync.get_last_error()["kind"] is None

    h = {k.lower(): v for k, v in sent[0].header_items()}
    assert h["x-ebay-api-call-name"] == "GetMyeBaySelling"
    assert h["x-ebay-api-siteid"] == "0"
    assert (h["x-ebay-api-app-name"], h["x-ebay-api-dev-name"], h["x-ebay-api-cert-name"]) == ("app", "dev", "cert")
    assert sent[0].full_url == "https://api.ebay.com/ws/api.dll"
    assert b"<eBayAuthToken>tok+en=</eBayAuthToken>" in sent[0].data
    assert b"<PageNumber>1</PageNumber>" in sent[0].data and b"<PageNumber>2</PageNumber>" in sent[1].data


def test_missing_view_and_watch_tags_give_none_and_zero(monkeypatch, creds):
    # Real ActiveList XML has neither HitCount nor (with 0 watchers) WatchCount.
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_page([_item("111111111111", watch=None, hits=None)])))
    (l,) = fetch_active_listings()
    assert l["view_count"] is None      # unknown, NOT 0
    assert l["watch_count"] == 0        # no tag = no watchers


def test_api_failure_returns_empty_and_never_raises(monkeypatch, creds, logs):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_failure("518", "call limit")))
    assert fetch_active_listings() == []
    err = ebay_sync.get_last_error()
    assert err["kind"] == "api" and "518" in err["message"]


def test_expired_token_sets_auth_kind(monkeypatch, creds):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_failure("932", "Auth token is hard expired")))
    assert fetch_active_listings() == []
    assert ebay_sync.get_last_error()["kind"] == "auth"


def test_network_error_retries_once_then_returns_empty(monkeypatch, creds):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise urllib.error.URLError("down")

    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen", boom)
    assert fetch_active_listings() == []
    assert len(calls) == 2
    assert ebay_sync.get_last_error()["kind"] == "network"


def test_http_4xx_is_not_retried(monkeypatch, creds):
    calls = []

    def bad(*a, **k):
        calls.append(1)
        raise urllib.error.HTTPError("u", 403, "forbidden", {}, None)

    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen", bad)
    assert fetch_active_listings() == []
    assert len(calls) == 1


def test_malformed_xml_returns_empty(monkeypatch, creds):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen", lambda *a, **k: FakeResp("<not xml"))
    assert fetch_active_listings() == []
    assert ebay_sync.get_last_error()["kind"] == "api"


def test_failure_on_page_two_discards_partial_results(monkeypatch, creds):
    pages = iter([FakeResp(_page([_item("111111111111")], total=2)),
                  FakeResp(_failure("518"))])
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen", lambda *a, **k: next(pages))
    assert fetch_active_listings() == []  # a partial list would flag everything else "removed"


# ── sync: matching ────────────────────────────────────────────────────────────

def test_match_updates_units_sold_only_when_changed(writes):
    rows = [_row(4, "111111111111", sold="1"),    # changes 1 -> 3
            _row(5, "222222222222", sold="2"),    # unchanged
            _row(6, "333333333333", sold="")]     # blank -> 0, eBay says 0: unchanged
    listings = [_listing("111111111111", sold=3), _listing("222222222222", sold=2),
                _listing("333333333333", sold=0)]
    rep = _sync(rows, listings)

    assert len(rep["matched"]) == 3
    assert [u["row_num"] for u in rep["updated"]] == [4]
    assert writes == [(4, [("U", 3)])]


def test_only_units_sold_is_ever_written(writes):
    rows = [_row(4, "111111111111", sold="0", price="$1.00")]
    _sync(rows, [_listing("111111111111", price=99.0, sold=4)])
    assert writes
    for _, pairs in writes:
        cols = {c for c, _ in pairs}
        assert cols == {"U"} and not (cols & PROTECTED_COLS)


def test_matches_across_url_formats(writes):
    rows = [_row(4, url="ebay.com/itm/Slug-Here/111111111111?hash=item1"),
            _row(5, url="222222222222")]
    rep = _sync(rows, [_listing("111111111111", sold=1), _listing("222222222222", sold=1)])
    assert len(rep["matched"]) == 2 and len(writes) == 2
    assert rep["on_ebay_not_in_sheet"] == []


def test_dry_run_reports_but_does_not_write(writes):
    rep = _sync([_row(4, "111111111111", sold="0")], [_listing("111111111111", sold=2)], dry_run=True)
    assert writes == []
    assert [u["row_num"] for u in rep["updated"]] == [4]
    assert rep["dry_run"] and summarize(rep).startswith("[dry-run]")


def test_duplicate_item_id_first_row_wins(writes):
    rows = [_row(4, "111111111111"), _row(9, "111111111111")]
    rep = _sync(rows, [_listing("111111111111", sold=1)])
    assert [m["row_num"] for m in rep["matched"]] == [4]
    assert [d["row_num"] for d in rep["duplicate_url"]] == [9]
    assert [w[0] for w in writes] == [4]


def test_one_failed_write_does_not_abort_the_rest(monkeypatch):
    done = []

    def flaky(svc, sheet, row, pairs):
        if row == 4:
            raise RuntimeError("boom")
        done.append(row)

    monkeypatch.setattr(ebay_sync, "safe_write_row", flaky)
    rows = [_row(4, "111111111111"), _row(5, "222222222222")]
    rep = _sync(rows, [_listing("111111111111", sold=1), _listing("222222222222", sold=1)])
    assert done == [5]
    assert [w["row_num"] for w in rep["write_errors"]] == [4]


def test_stale_row_title_mismatch_is_skipped(writes):
    rows = [_row(4, "111111111111", title="Gold Bar"), _row(5, "222222222222", title="Silver Bar")]
    rep = _sync(rows, [_listing("111111111111", sold=1), _listing("222222222222", sold=1)],
                title_reader=lambda: {4: "Gold Bar", 5: "Something Else Now"})
    assert [w[0] for w in writes] == [4]
    assert [s["row_num"] for s in rep["stale_rows"]] == [5]


def test_title_recheck_failure_skips_all_writes(writes):
    def bad():
        raise RuntimeError("sheets down")

    rep = _sync([_row(4, "111111111111")], [_listing("111111111111", sold=1)], title_reader=bad)
    assert writes == [] and len(rep["write_errors"]) == 1


# ── margin_flag (pure) ────────────────────────────────────────────────────────

_flag = ebay_sync.margin_flag


@pytest.mark.parametrize("price,cost,fee,ship,ad,sold,expected", [
    (100.0, 70.0, 0.10, 0, 0, 5, None),        # net 13.13 -> 13.1% margin -> healthy
    (100.0, 95.0, 0.10, 0, 0, 5, "hard"),      # net -13.94
    (100.0, 95.0, 0.10, 0, 0, 0, "hard"),      # negative is hard regardless of velocity
])
def test_margin_flag_hard_and_healthy(price, cost, fee, ship, ad, sold, expected):
    assert _flag(price, cost, fee, ship, ad, sold) == expected


def test_margin_flag_floor():
    # price >= 70 and margin < 9% -> "floor" (beats soft/silent; velocity doesn't matter)
    assert _flag(100.0, 80.0, 0.10, 0, 0, 0) == "floor"      # net 2.30 -> 2.3% margin
    assert _flag(100.0, 80.0, 0.10, 0, 0, 3) == "floor"
    assert _flag(100.0, 80.0, 0.10, 0, 0, None) == "floor"    # blank sold_90d still floor
    assert _flag(100.0, 70.0, 0.10, 0, 0, 0) is None          # net 13.13 -> 13.1% margin, no floor
    assert _flag(60.0, 48.0, 0.10, 0, 0, 0) == "soft"         # below $70 -> soft, not floor


def test_margin_flag_soft_needs_zero_sales():
    # net = 60 - 48 - (60*0.10*1.08 + 0.30) - 48*0.0825 = 1.26  (price 60 < 70, no floor)
    assert _flag(60.0, 48.0, 0.10, 0, 0, 0) == "soft"
    assert _flag(60.0, 48.0, 0.10, 0, 0, 3) is None          # high velocity rides silently
    assert _flag(60.0, 48.0, 0.10, 0, 0, None) is None       # blank sold_90d = unknown, not 0


def test_margin_flag_boundaries():
    # below $70 (no floor applies) — boundary between silent / soft / hard.
    # price 60, fee 0.10: net = 53.22 - 1.0825*cost
    assert _flag(60.0, 45.0, 0.10, 0, 0, 0) is None           # net 4.51 -> silent (>= soft floor)
    assert _flag(60.0, 48.0, 0.10, 0, 0, 0) == "soft"         # net 1.26 -> soft, not hard
    assert _flag(60.0, 50.0, 0.10, 0, 0, 9) == "hard"         # net -0.91 -> hard


def test_margin_flag_subtracts_ship_and_ad():
    # price 60 < 70 (no floor); net = 53.22 - 0.30 - 1.0825*cost - ship - ad
    assert _flag(60.0, 45.0, 0.10, 0, 0, 5) is None           # net 4.51 -> healthy
    assert _flag(60.0, 45.0, 0.10, 6.0, 0, 5) == "hard"       # ship pushes net to -1.49
    assert _flag(60.0, 45.0, 0.10, 0, 5.0, 5) == "hard"       # ad pushes net to -0.49
    assert _flag(60.0, 45.0, 0.10, None, None, 5) is None     # missing ship/ad count as 0


@pytest.mark.parametrize("price,cost,fee", [(None, 5.0, 0.1), (10.0, None, 0.1), (10.0, 5.0, None)])
def test_margin_flag_unknown_inputs_are_never_judged(price, cost, fee):
    assert _flag(price, cost, fee, 0, 0, 0) is None            # unknown != $0


def test_cost_basis_prefers_buy_cost_else_costco():
    cb = ebay_sync._cost_basis
    assert cb("$45.00", "$60.00") == 45.0
    assert cb("", "$60.00") == 60.0
    assert cb("junk", "1,060.00") == 1060.0
    assert cb("0", "60") == 60.0                               # a 0 buy_cost is "not filled in"
    assert cb("", "") is None


@pytest.mark.parametrize("raw,expected", [
    (0.1325, 0.1325), ("0.1325", 0.1325), ("13.25%", 0.1325), (13.25, 0.1325), ("", None), ("x", None),
])
def test_to_rate(raw, expected):
    got = ebay_sync._to_rate(raw)
    assert got == pytest.approx(expected) if expected is not None else got is None


# ── sync: margin breaches ─────────────────────────────────────────────────────

def test_margin_breach_hard_uses_live_price_and_is_never_written(writes):
    rows = [_row(4, "111111111111", price="$29.99", costco_cost="$30.00", fee_rate="0.10", sold_90d="4")]
    rep = _sync(rows, [_listing("111111111111", price=29.0)])   # net 29 - 30 - 3.43 - 2.48 = -6.91
    (b,) = rep["margin_breach"]
    assert b["severity"] == "hard" and b["net"] == -6.91 and b["cost_basis"] == 30.0
    assert b["row_num"] == 4 and b["item_id"] == "111111111111"
    assert b["break_even"] == pytest.approx(32.775 / 0.892)
    assert "price_mismatch" not in rep
    assert writes == []                                          # flag-only


def test_buy_cost_overrides_costco_cost(writes):
    # Costco says $30 (would lose money at $29), but Jay paid $20 -> healthy
    rows = [_row(4, "111111111111", buy_cost="$20", costco_cost="$30", fee_rate="0.10", sold_90d="4")]
    rep = _sync(rows, [_listing("111111111111", price=29.0)])
    assert rep["margin_breach"] == [] and rep["margin_unchecked"] == []


def test_raw_price_delta_no_longer_alerts(writes):
    # eBay price differs from sheet H by $5 and by a cent; margin is healthy -> silent
    rows = [_row(4, "111111111111", price="$29.99", costco_cost="$10", fee_rate="0.10", sold_90d="4"),
            _row(5, "222222222222", price="$29.99", costco_cost="$10", fee_rate="0.10", sold_90d="4")]
    rep = _sync(rows, [_listing("111111111111", price=34.99), _listing("222222222222", price=29.98)])
    assert rep["margin_breach"] == []
    assert alert_message(rep) is None


def test_soft_breach_in_report_but_not_in_alert(writes):
    rows = [_row(4, "111111111111", costco_cost="$23", fee_rate="0.10", sold_90d="0")]
    rep = _sync(rows, [_listing("111111111111", price=29.0)])   # net 29 - 23 - 3.43 - 1.90 = 0.67
    assert [b["severity"] for b in rep["margin_breach"]] == ["soft"]
    assert alert_message(rep) is None                            # digest only, never per-run
    d = ebay_sync.digest_message(rep)
    assert d and "Widget" in d and "$0.67" in d


def test_high_velocity_thin_margin_is_silent(writes):
    rows = [_row(4, "111111111111", costco_cost="$23", fee_rate="0.10", sold_90d="12")]
    rep = _sync(rows, [_listing("111111111111", price=29.0)])
    assert rep["margin_breach"] == [] and ebay_sync.digest_message(rep) is None


def test_rows_missing_inputs_counted_unchecked_not_flagged(writes):
    rows = [_row(4, "111111111111"),                                       # no cost / fee at all
            _row(5, "222222222222", costco_cost="$30", fee_rate="")]       # fee unknown
    rep = _sync(rows, [_listing("111111111111", price=1.0), _listing("222222222222", price=1.0)])
    assert rep["margin_breach"] == [] and len(rep["margin_unchecked"]) == 2
    assert "margin_unchecked 2" in summarize(rep)


def test_summary_and_alert_text_for_hard_breach(writes):
    rows = [_row(4, "111111111111", title="Tom & Jerry <b>", costco_cost="$30", fee_rate="0.10", sold_90d="4")]
    rep = _sync(rows, [_listing("111111111111", price=29.0)])
    assert "margin_breach hard 1 soft 0" in summarize(rep)
    msg = alert_message(rep)
    assert "losing money on Tom &amp; Jerry &lt;b&gt; — net -$6.91" in msg
    assert "break-even $36.74" in msg and "row 4" in msg


def test_floor_breach_reported_in_alert_not_digest(writes):
    rows = [_row(4, "111111111111", title="Big Ticket", costco_cost="$80", fee_rate="0.10", sold_90d="5")]
    rep = _sync(rows, [_listing("111111111111", price=100.0)])   # net 2.30 → 2.3% margin → floor
    (b,) = rep["margin_breach"]
    assert b["severity"] == "floor" and b["margin"] == pytest.approx(2.3)
    assert "margin_breach hard 0 soft 0 floor 1" in summarize(rep)
    msg = alert_message(rep)
    assert "margin floor" in msg and "Big Ticket" in msg and "2.3% margin" in msg
    assert ebay_sync.digest_message(rep) is None                  # floor is an alert, not a digest


def test_digest_is_once_per_calendar_day():
    due = ebay_sync._digest_due
    assert due(today="2026-09-24") is True
    assert due(today="2026-09-24") is False
    assert due(today="2026-09-25") is True
    assert due(dry_run=True, today="2026-09-25") is True         # dry run never consumes the slot
    assert due(today="2026-09-25") is False


# ── sync: presence flags ──────────────────────────────────────────────────────

def test_on_ebay_not_in_sheet(writes):
    rep = _sync([_row(4, "111111111111")], [_listing("111111111111"), _listing("999999999999", title="Stray")])
    assert [l["item_id"] for l in rep["on_ebay_not_in_sheet"]] == ["999999999999"]


def test_active_not_on_ebay_only_flags_active_ebay_rows(writes):
    rows = [
        _row(4, "111111111111", status="ACTIVE"),                        # gone from eBay -> flagged
        _row(5, "222222222222", status="PAUSED_OOS"),                    # not ACTIVE -> ignored
        _row(6, "333333333333", status="ACTIVE", platform="Site"),       # site-only -> ignored
        _row(7, "", status="ACTIVE", platform="eBay"),                   # no URL -> flagged
        _row(8, url="https://www.ebay.com/str/x", status="ACTIVE"),      # unparseable -> flagged
        _row(9, "444444444444", status="ACTIVE"),                        # still listed -> fine
    ]
    rep = _sync(rows, [_listing("444444444444")])
    flagged = {r["row_num"]: r["reason"] for r in rep["active_not_on_ebay"]}
    assert set(flagged) == {4, 7, 8}
    assert "sold out" in flagged[4] and "no URL in col Q" in flagged[7] and "parseable" in flagged[8]
    assert "never listed" not in flagged[7]
    assert "likely" not in flagged[7]          # no matching eBay listing -> no suggestion


def test_matched_entries_carry_no_view_count(writes):
    rep = _sync([_row(4, "111111111111")], [_listing("111111111111", sold=1)])
    assert "view_count" not in rep["matched"][0]


# ── sync: col Q link suggestions (flag-only) ──────────────────────────────────

def test_exact_title_suggests_item_id_and_never_writes_col_q(writes):
    rows = [_row(14, "", title="Soy Beverage Vanilla")]
    rep = _sync(rows, [_listing("318547418616", title="soy beverage - VANILLA!")])
    (g,) = rep["active_not_on_ebay"]
    assert g["reason"] == "no URL in col Q — likely = eBay item 318547418616, fill col Q"
    assert g["suggested_item_id"] == "318547418616" and g["match_kind"] == "exact"
    assert rep["on_ebay_not_in_sheet"][0]["suggested_row"] == 14
    assert writes == []                        # never auto-writes col Q
    assert "link_suggestions 1" in summarize(rep)


def test_close_title_is_hedged(writes):
    rows = [_row(5, "", title="Kirkland Signature Fish Oil 1000 mg 400 Softgels")]
    rep = _sync(rows, [_listing("111111111111", title="Kirkland Signature Fish Oil 1000 mg 400 Softgels Bottle")])
    (g,) = rep["active_not_on_ebay"]
    assert g["match_kind"] == "close" and "possibly = eBay item 111111111111" in g["reason"]


def test_dissimilar_title_gets_no_suggestion(writes):
    rep = _sync([_row(5, "", title="Gold Bar 1 oz")], [_listing("111111111111", title="Air Fryer 7 Qt")])
    (g,) = rep["active_not_on_ebay"]
    assert "suggested_item_id" not in g and g["reason"] == "no URL in col Q"
    assert "link_suggestions" not in summarize(rep)


def test_ambiguous_suggestions_are_dropped(writes):
    # one row, two identical-title listings -> ambiguous
    rep = _sync([_row(5, "", title="Widget")],
                [_listing("111111111111", title="Widget"), _listing("222222222222", title="Widget")])
    assert "suggested_item_id" not in rep["active_not_on_ebay"][0]
    # two rows, one listing -> neither row gets it
    rep2 = _sync([_row(5, "", title="Widget"), _row(6, "", title="Widget")],
                 [_listing("111111111111", title="Widget")])
    assert all("suggested_item_id" not in g for g in rep2["active_not_on_ebay"])
    assert "suggested_row" not in rep2["on_ebay_not_in_sheet"][0]


def test_already_linked_listing_is_never_suggested(writes):
    rows = [_row(4, "111111111111", title="Widget"), _row(5, "", title="Widget")]
    rep = _sync(rows, [_listing("111111111111", title="Widget")])
    (g,) = rep["active_not_on_ebay"]
    assert g["row_num"] == 5 and "suggested_item_id" not in g


def test_suggestion_text_is_html_escaped_in_alert(writes):
    rep = _sync([_row(5, "", title="<i>Tom & Jerry</i>")],
                [_listing("111111111111", title="<i>Tom & Jerry</i>")])
    msg = alert_message(rep)
    assert "likely = eBay item 111111111111" in msg
    assert "<i>Tom" not in msg and "&lt;i&gt;Tom &amp; Jerry" in msg


# ── report formatting ─────────────────────────────────────────────────────────

def test_alert_message_none_when_clean(writes):
    rep = _sync([_row(4, "111111111111", sold="0")], [_listing("111111111111", sold=1)])
    assert alert_message(rep) is None                    # only units changed -> silent
    assert rep["on_ebay_not_in_sheet"] == [] and rep["active_not_on_ebay"] == []


def test_alert_message_fires_on_orphans_alone(writes):
    rep = _sync([], [_listing("999999999999", title="Stray <Patio> & Set")])
    msg = alert_message(rep)
    assert msg and "On eBay but not in the sheet (1)" in msg
    assert "Stray &lt;Patio&gt; &amp; Set" in msg and "999999999999" in msg
    assert "no matching sheet row, end it or link it by hand" in msg
    assert writes == []


def test_orphan_with_exact_match_gets_fill_col_q_hint(writes):
    rep = _sync([_row(14, "", title="Soy Beverage Vanilla")],
                [_listing("318547418616", title="soy beverage - VANILLA!")])
    msg = alert_message(rep)
    assert "eBay 318547418616 (row 14, blank col Q, fill col Q to link)" in msg
    assert writes == []


def test_orphan_with_close_match_says_verify(writes):
    rep = _sync([_row(5, "", title="Kirkland Signature Fish Oil 1000 mg 400 Softgels")],
                [_listing("111111111111", title="Kirkland Signature Fish Oil 1000 mg 400 Softgels Bottle")])
    assert rep["on_ebay_not_in_sheet"][0]["match_kind"] == "close"
    assert "(row 5, close title match, verify then fill col Q)" in alert_message(rep)


def test_hidden_orphans_do_not_alert_alone_and_are_counted(writes):
    hidden = _listing("999999999999", title="Hidden Stray", qty=3, sold=3)     # 0 available
    assert alert_message(_sync([], [hidden])) is None
    msg = alert_message(_sync([], [hidden, _listing("888888888888", title="Selling Stray")]))
    assert "On eBay but not in the sheet (1)" in msg and "Selling Stray" in msg
    assert "Hidden Stray" not in msg and "1 more not in the sheet, hidden at quantity 0" in msg


def test_orphan_section_is_capped(writes):
    rep = _sync([], [_listing(f"{900000000000 + i}", title=f"Stray {i}") for i in range(30)])
    msg = alert_message(rep)
    assert "On eBay but not in the sheet (30)" in msg
    assert f"…and {30 - ebay_sync.MAX_REPORT_LINES} more" in msg


def test_alert_message_escapes_and_truncates_and_caps(writes):
    rows = [_row(i, f"{100000000000 + i}", title="<b>Tom & Jerry</b> " + "x" * 80,
                 costco_cost="$100", fee_rate="0.10") for i in range(4, 30)]
    listings = [_listing(f"{100000000000 + i}", price=50.0) for i in range(4, 30)]
    rep = _sync(rows, listings)
    msg = alert_message(rep)
    assert "&lt;b&gt;Tom &amp; Jerry" in msg and "<b>Tom" not in msg
    assert "…and 11 more" in msg
    assert len(msg) < 4096


# ── run_ebay_sync orchestration ───────────────────────────────────────────────

_ALERT_ONLY = {"business": {"ebay_sync": {"auto_reprice": False}}}


def _cfg_args(config=None):
    """Default = auto-reprice OFF (the alert-only path); auto-reprice tests pass their own."""
    return (_ALERT_ONLY if config is None else config, COL, "svc", "Product Tracker", 4, 500)


def test_run_skips_cleanly_without_token(monkeypatch):
    monkeypatch.setenv("EBAY_AUTH_TOKEN", "")
    res = run_ebay_sync(*_cfg_args())
    assert res["status"] == "skipped" and "no EBAY_AUTH_TOKEN" in res["notes"] and res["alert"] is None


def test_run_api_failure_does_not_flag_active_rows(monkeypatch, creds):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_failure("518")))
    monkeypatch.setattr(ebay_sync, "load_sheet_rows",
                        lambda *a, **k: pytest.fail("must not read rows / compute removals on API failure"))
    res = run_ebay_sync(*_cfg_args())
    assert res["status"] == "error" and res["alert"] is None


def test_run_expired_token_alerts(monkeypatch, creds):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_failure("931", "Auth token is invalid")))
    res = run_ebay_sync(*_cfg_args())
    assert res["status"] == "error" and "auth token rejected" in res["alert"]


def test_run_happy_path_writes_and_summarizes(monkeypatch, creds, writes):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_page([_item("111111111111", price="40.00", sold=2)])))
    rows = [_row(4, "111111111111", price="$29.99", sold="0", costco_cost="$50", fee_rate="0.10"),
            _row(5, "222222222222", status="ACTIVE")]
    monkeypatch.setattr(ebay_sync, "load_sheet_rows", lambda *a, **k: rows)
    monkeypatch.setattr(ebay_sync, "_read_titles", lambda *a, **k: {4: "Widget", 5: "Widget"})
    res = run_ebay_sync(*_cfg_args())

    assert writes == [(4, [("U", 2)]), (4, [("H", 40.0)])]       # units, then col H mirrors eBay
    assert res["status"] == "ok"
    assert "matched 1" in res["notes"] and "margin_breach hard 1 soft 0" in res["notes"] \
        and "active_not_on_ebay 1" in res["notes"]
    assert res["alert"] and "Losing money" in res["alert"] and "Price mismatch" not in res["alert"]
    assert res["digest"] is None


def test_run_price_change_alone_is_silent(monkeypatch, creds, writes):
    # live price 40 vs sheet 29.99, but cost $10 -> healthy -> no alert, no digest
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_page([_item("111111111111", price="40.00", sold=0)])))
    monkeypatch.setattr(ebay_sync, "load_sheet_rows", lambda *a, **k: [
        _row(4, "111111111111", price="$29.99", sold="0", costco_cost="$10", fee_rate="0.10", sold_90d="3")])
    res = run_ebay_sync(*_cfg_args())
    assert res["alert"] is None and res["digest"] is None


def test_run_repeated_alert_is_suppressed_but_notes_stay(monkeypatch, creds, writes):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_page([_item("111111111111", price="40.00", sold=0)])))
    monkeypatch.setattr(ebay_sync, "load_sheet_rows", lambda *a, **k: [
        _row(4, "111111111111", price="$29.99", sold="0", costco_cost="$50", fee_rate="0.10")])
    first = run_ebay_sync(*_cfg_args())
    second = run_ebay_sync(*_cfg_args())
    assert first["alert"] and second["alert"] is None
    assert "margin_breach hard 1" in second["notes"]


def test_run_soft_digest_sent_once_per_day_and_never_in_dry_run(monkeypatch, creds, writes):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_page([_item("111111111111", price="29.00", sold=0)])))
    monkeypatch.setattr(ebay_sync, "load_sheet_rows", lambda *a, **k: [
        _row(4, "111111111111", sold="0", costco_cost="$23", fee_rate="0.10", sold_90d="0")])
    dry = run_ebay_sync(*_cfg_args(), dry_run=True)
    assert dry["alert"] is None and dry["digest"]                # dry run shows it, records nothing
    first = run_ebay_sync(*_cfg_args())
    second = run_ebay_sync(*_cfg_args())
    assert first["digest"] and "thin-margin" in first["digest"]
    assert first["alert"] is None                                # soft never becomes a per-run alert
    assert second["digest"] is None                              # already sent today


# ── alert de-duplication ──────────────────────────────────────────────────────

def test_dedupe_alert_window_and_reset():
    d = ebay_sync._dedupe_alert
    assert d("A", now=1000) == "A"                                   # first send passes
    assert d("A", now=1000 + 3600) is None                           # identical, inside 24h
    assert d("B", now=1000 + 3600) == "B"                            # changed text passes
    assert d("B", now=1000 + 3600 + ebay_sync.ALERT_REPEAT_HOURS * 3600) == "B"   # window over
    assert d(None, now=5000) is None                                 # clean run clears state
    assert d("B", now=5001) == "B"                                   # recurrence alerts again


def test_dedupe_alert_dry_run_neither_suppresses_nor_writes():
    d = ebay_sync._dedupe_alert
    assert d("A", dry_run=True, now=1) == "A"
    assert not os.path.exists(ebay_sync.ALERT_STATE_PATH)
    assert d("A", now=2) == "A"                                      # dry run recorded nothing
    assert d("A", dry_run=True, now=3) == "A"                        # ...and isn't suppressed


def test_dedupe_alert_unwritable_state_still_sends(monkeypatch, tmp_path):
    monkeypatch.setattr(ebay_sync, "ALERT_STATE_PATH", str(tmp_path / "no_such_dir" / "a.json"))
    assert ebay_sync._dedupe_alert("A", now=1) == "A"


# ── scheduler wiring ──────────────────────────────────────────────────────────

def test_scheduler_alerts_only_when_there_is_an_alert(monkeypatch):
    from agents import scheduler
    sent = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(scheduler, "_send_telegram", lambda tok, chat, text: sent.append(text))

    monkeypatch.setattr(scheduler.ebay_sync, "run_ebay_sync",
                        lambda *a, **k: {"status": "ok", "notes": "matched 3", "alert": None})
    out = scheduler.run_ebay_sync_mode({}, COL, "svc", "Tab", 4, 500)
    assert sent == [] and out == {"status": "ok", "notes": "matched 3"}   # silent; alert key stripped

    monkeypatch.setattr(scheduler.ebay_sync, "run_ebay_sync",
                        lambda *a, **k: {"status": "ok", "notes": "n", "alert": "⚠️ mismatch"})
    scheduler.run_ebay_sync_mode({}, COL, "svc", "Tab", 4, 500)
    assert sent == ["⚠️ mismatch"]

    sent.clear()
    monkeypatch.setattr(scheduler.ebay_sync, "run_ebay_sync",
                        lambda *a, **k: {"status": "ok", "notes": "n", "alert": None, "digest": "🐢 thin"})
    out = scheduler.run_ebay_sync_mode({}, COL, "svc", "Tab", 4, 500)
    assert sent == ["🐢 thin"] and "digest" not in out


def test_scheduler_passes_dry_run_through(monkeypatch):
    from agents import scheduler
    seen = {}
    monkeypatch.setattr(scheduler.ebay_sync, "run_ebay_sync",
                        lambda *a, **k: seen.update(k) or {"status": "ok", "alert": None})
    scheduler.run_ebay_sync_mode({}, COL, "svc", "Tab", 4, 500, dry_run=True)
    assert seen["dry_run"] is True


def test_scheduler_registers_ebay_sync_mode_and_dry_run_flag(monkeypatch):
    from agents import scheduler
    monkeypatch.setattr(scheduler, "_acquire_lock", lambda mode: False)  # stop main() right after parsing
    monkeypatch.setattr(sys, "argv", ["scheduler.py", "--mode", "ebay_sync", "--dry-run"])
    scheduler.main()  # would SystemExit(2) on an unknown mode / flag


# ── revise_fixed_price / fetch_item_price (one-tap reprice) ──────────────────

def _revise_resp(ack="Success", errors=""):
    return (f'<ReviseFixedPriceItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>{ack}</Ack>'
            f"{errors}<ItemID>123456789012</ItemID></ReviseFixedPriceItemResponse>")


def _err(code, msg="bad", severity="Error"):
    return (f"<Errors><SeverityCode>{severity}</SeverityCode><ErrorCode>{code}</ErrorCode>"
            f"<ShortMessage>{msg}</ShortMessage></Errors>")


def test_build_revise_xml_changes_only_start_price():
    xml = ebay_sync._build_revise_xml("a<b&c", "123456789012", 50.9)
    assert xml.startswith('<?xml version="1.0" encoding="utf-8"?>')
    assert '<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">' in xml
    assert "<eBayAuthToken>a&lt;b&amp;c</eBayAuthToken>" in xml          # token escaped
    assert "<Item><ItemID>123456789012</ItemID>" in xml
    assert '<StartPrice currencyID="USD">50.90</StartPrice></Item>' in xml  # 2dp
    for other in ("<Quantity>", "<Title>", "<Description>", "<PictureDetails>"):
        assert other not in xml                                             # nothing else touched


def test_headers_default_call_name_unchanged_and_overridable():
    assert ebay_sync._headers("a", "d", "c")["X-EBAY-API-CALL-NAME"] == "GetMyeBaySelling"
    h = ebay_sync._headers("a", "d", "c", "ReviseFixedPriceItem")
    assert h["X-EBAY-API-CALL-NAME"] == "ReviseFixedPriceItem"
    assert h["X-EBAY-API-COMPATIBILITY-LEVEL"] == "1193"


def test_revise_success_posts_correct_request(monkeypatch, creds):
    sent = []

    def fake(req, timeout=None):
        sent.append(req)
        return FakeResp(_revise_resp())

    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen", fake)
    out = ebay_sync.revise_fixed_price("https://www.ebay.com/itm/123456789012", 50.99)
    assert out["ok"] is True and out["price"] == 50.99 and out["item_id"] == "123456789012"
    assert out["error_kind"] is None
    (req,) = sent
    h = {k.lower(): v for k, v in req.header_items()}
    assert h["x-ebay-api-call-name"] == "ReviseFixedPriceItem"
    assert req.full_url == "https://api.ebay.com/ws/api.dll"
    assert b'<StartPrice currencyID="USD">50.99</StartPrice>' in req.data
    assert b"<eBayAuthToken>tok+en=</eBayAuthToken>" in req.data


def test_revise_warning_ack_is_success(monkeypatch, creds):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_revise_resp("Warning", _err("21917", "fyi", "Warning"))))
    out = ebay_sync.revise_fixed_price("123456789012", 10)
    assert out["ok"] is True and "21917" in out["message"]


@pytest.mark.parametrize("code", ["931", "932", "16110"])
def test_revise_auth_error_is_loud(monkeypatch, creds, logs, code):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_revise_resp("Failure", _err(code, "token bad"))))
    out = ebay_sync.revise_fixed_price("123456789012", 10)
    assert out["ok"] is False and out["error_kind"] == "auth" and out["error_code"] == code
    assert any("CRITICAL" in m and "FAILED" in m for m in logs)


def test_revise_api_error_returns_code(monkeypatch, creds):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_revise_resp("Failure", _err("21916750", "variation"))))
    out = ebay_sync.revise_fixed_price("123456789012", 10)
    assert out == {**out, "ok": False, "error_kind": "api", "error_code": "21916750"}
    assert "variation" in out["message"]


def test_revise_network_error_never_raises(monkeypatch, creds):
    def boom(*a, **k):
        raise urllib.error.URLError("down")
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen", boom)
    out = ebay_sync.revise_fixed_price("123456789012", 10)
    assert out["ok"] is False and out["error_kind"] == "network"


@pytest.mark.parametrize("item_id,price", [
    ("", 10), ("not-an-id", 10), ("https://evil.com/itm/123456789012", 10),
    ("123456789012", 0), ("123456789012", -5), ("123456789012", None), ("123456789012", "abc"),
])
def test_revise_invalid_input_never_posts(monkeypatch, creds, item_id, price):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("must not hit the network"))
    out = ebay_sync.revise_fixed_price(item_id, price)
    assert out["ok"] is False and out["error_kind"] == "invalid"


def test_revise_without_token_never_posts(monkeypatch, creds):
    monkeypatch.setenv("EBAY_AUTH_TOKEN", "")
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("must not hit the network"))
    out = ebay_sync.revise_fixed_price("123456789012", 10)
    assert out["ok"] is False and out["error_kind"] == "no_token"


def test_fetch_item_price_reads_live_price_and_status(monkeypatch, creds):
    sent = []
    body = ('<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack><Item>'
            "<ItemID>123456789012</ItemID><ListingType>FixedPriceItem</ListingType>"
            '<SellingStatus><CurrentPrice currencyID="USD">41.48</CurrentPrice>'
            "<ListingStatus>Active</ListingStatus></SellingStatus></Item></GetItemResponse>")

    def fake(req, timeout=None):
        sent.append(req)
        return FakeResp(body)

    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen", fake)
    out = ebay_sync.fetch_item_price("123456789012")
    assert out["ok"] and out["price"] == 41.48 and out["listing_status"] == "Active"
    assert out["listing_type"] == "FixedPriceItem"
    assert {k.lower(): v for k, v in sent[0].header_items()}["x-ebay-api-call-name"] == "GetItem"
    assert b"<ItemID>123456789012</ItemID>" in sent[0].data


def test_fetch_item_price_auth_error(monkeypatch, creds):
    body = ('<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Failure</Ack>'
            f'{_err("932", "expired")}</GetItemResponse>')
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen", lambda *a, **k: FakeResp(body))
    out = ebay_sync.fetch_item_price("123456789012")
    assert out["ok"] is False and out["error_kind"] == "auth" and out["error_code"] == "932"


# ── end_fixed_price (scheduled "End listing") ────────────────────────────────

def _end_resp(ack="Success", errors=""):
    return (f'<EndItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>{ack}</Ack>'
            f"{errors}<EndTime>2026-10-19T07:01:00.000Z</EndTime></EndItemResponse>")


def test_build_end_xml():
    xml = ebay_sync._build_end_xml("a<b", "123456789012")
    assert '<EndItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">' in xml
    assert "<eBayAuthToken>a&lt;b</eBayAuthToken>" in xml
    assert "<ItemID>123456789012</ItemID>" in xml
    assert "<EndingReason>NotAvailable</EndingReason>" in xml
    assert "StartPrice" not in xml


def test_end_success_posts_enditem(monkeypatch, creds):
    sent = []

    def fake(req, timeout=None):
        sent.append(req)
        return FakeResp(_end_resp())

    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen", fake)
    out = ebay_sync.end_fixed_price("https://www.ebay.com/itm/123456789012")
    assert out["ok"] is True and out["item_id"] == "123456789012" and out["price"] is None
    assert "2026-10-19" in out["message"]
    h = {k.lower(): v for k, v in sent[0].header_items()}
    assert h["x-ebay-api-call-name"] == "EndItem" and h["x-ebay-api-compatibility-level"] == "1193"


def test_end_already_ended_counts_as_ok(monkeypatch, creds):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_end_resp("Failure", _err("1047", "already closed"))))
    out = ebay_sync.end_fixed_price("123456789012")
    assert out["ok"] is True and "already ended" in out["message"]


@pytest.mark.parametrize("code", ["931", "932", "16110"])
def test_end_auth_error_is_loud(monkeypatch, creds, logs, code):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_end_resp("Failure", _err(code, "token"))))
    out = ebay_sync.end_fixed_price("123456789012")
    assert out["ok"] is False and out["error_kind"] == "auth" and out["error_code"] == code
    assert any("CRITICAL" in m for m in logs)


def test_end_api_error(monkeypatch, creds):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_end_resp("Failure", _err("291", "not allowed"))))
    out = ebay_sync.end_fixed_price("123456789012")
    assert out["ok"] is False and out["error_kind"] == "api" and out["error_code"] == "291"


@pytest.mark.parametrize("item_id", ["", None, "abc", "https://evil.com/itm/123456789012"])
def test_end_invalid_id_never_posts(monkeypatch, creds, item_id):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("must not hit the network"))
    out = ebay_sync.end_fixed_price(item_id)
    assert out["ok"] is False and out["error_kind"] == "invalid"


# ── set_quantity (OOS hide / restock restore via ReviseInventoryStatus) ──────

def _qty_resp(ack="Success", errors=""):
    return (f'<ReviseInventoryStatusResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>{ack}</Ack>'
            f"{errors}<InventoryStatus><ItemID>123456789012</ItemID><Quantity>0</Quantity>"
            "</InventoryStatus></ReviseInventoryStatusResponse>")


@pytest.mark.parametrize("qty", [0, 7])
def test_build_qty_xml_changes_only_quantity(qty):
    xml = ebay_sync._build_qty_xml("a<b", "123456789012", qty)
    assert '<ReviseInventoryStatusRequest xmlns="urn:ebay:apis:eBLBaseComponents">' in xml
    assert "<eBayAuthToken>a&lt;b</eBayAuthToken>" in xml
    assert (f"<InventoryStatus><ItemID>123456789012</ItemID><Quantity>{qty}</Quantity>"
            "</InventoryStatus>") in xml
    assert "StartPrice" not in xml and "EndingReason" not in xml


def test_set_quantity_zero_posts_revise_inventory_status(monkeypatch, creds):
    sent = []

    def fake(req, timeout=None):
        sent.append(req)
        return FakeResp(_qty_resp())

    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen", fake)
    out = ebay_sync.set_quantity_zero("https://www.ebay.com/itm/123456789012")
    assert out["ok"] is True and out["item_id"] == "123456789012" and out["price"] is None
    assert out["message"] == "quantity 0"
    h = {k.lower(): v for k, v in sent[0].header_items()}
    assert h["x-ebay-api-call-name"] == "ReviseInventoryStatus"
    assert b"<Quantity>0</Quantity>" in sent[0].data


@pytest.mark.parametrize("code", ["931", "932", "16110"])
def test_set_quantity_auth_error_is_loud(monkeypatch, creds, logs, code):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_qty_resp("Failure", _err(code, "token"))))
    out = ebay_sync.set_quantity("123456789012", 5)
    assert out["ok"] is False and out["error_kind"] == "auth" and out["error_code"] == code
    assert any("CRITICAL" in m for m in logs)


def test_set_quantity_api_error(monkeypatch, creds):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_qty_resp("Failure", _err("21916799", "SKU needed"))))
    out = ebay_sync.set_quantity("123456789012", 0)
    assert out["ok"] is False and out["error_kind"] == "api" and out["error_code"] == "21916799"


@pytest.mark.parametrize("item_id,qty", [
    ("", 0), ("abc", 0), ("https://evil.com/itm/123456789012", 0),
    ("123456789012", -1), ("123456789012", None), ("123456789012", "x"), ("123456789012", 2.5),
])
def test_set_quantity_invalid_input_never_posts(monkeypatch, creds, item_id, qty):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("must not hit the network"))
    out = ebay_sync.set_quantity(item_id, qty)
    assert out["ok"] is False and out["error_kind"] == "invalid"


def test_fetch_item_price_reads_quantity_duration_and_oos_control(monkeypatch, creds):
    sent = []
    body = ('<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack><Item>'
            "<ItemID>123456789012</ItemID><ListingType>FixedPriceItem</ListingType>"
            "<ListingDuration>GTC</ListingDuration><OutOfStockControl>true</OutOfStockControl>"
            "<Quantity>12</Quantity>"
            '<SellingStatus><CurrentPrice currencyID="USD">41.48</CurrentPrice>'
            "<QuantitySold>5</QuantitySold><ListingStatus>Active</ListingStatus></SellingStatus>"
            "</Item></GetItemResponse>")

    def fake(req, timeout=None):
        sent.append(req)
        return FakeResp(body)

    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen", fake)
    out = ebay_sync.fetch_item_price("123456789012")
    assert out["quantity_available"] == 7
    assert out["listing_duration"] == "GTC" and out["out_of_stock_control"] is True
    for sel in (b"Item.Quantity", b"Item.ListingDuration", b"Item.OutOfStockControl"):
        assert b"<OutputSelector>" + sel + b"</OutputSelector>" in sent[0].data


def test_fetch_item_price_missing_quantity_fields_are_unknown(monkeypatch, creds):
    body = ('<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack><Item>'
            '<SellingStatus><CurrentPrice currencyID="USD">41.48</CurrentPrice>'
            "<ListingStatus>Active</ListingStatus></SellingStatus></Item></GetItemResponse>")
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen", lambda *a, **k: FakeResp(body))
    out = ebay_sync.fetch_item_price("123456789012")
    assert out["ok"] and out["quantity_available"] is None
    assert out["listing_duration"] == "" and out["out_of_stock_control"] is None


# ── auto-reprice hard breaches to break-even ──────────────────────────────────

@pytest.fixture
def ebay(monkeypatch):
    """Stub revise_fixed_price + log_revise; record calls."""
    import tools.reprice as reprice
    state = {"revise": [], "logged": [], "result": None}

    def fake_revise(item_id, price):
        state["revise"].append((item_id, price))
        return state["result"] or {"ok": True, "item_id": item_id, "price": price,
                                   "error_kind": None, "error_code": "", "message": ""}

    monkeypatch.setattr(ebay_sync, "revise_fixed_price", fake_revise)
    monkeypatch.setattr(reprice, "log_revise",
                        lambda svc, result, **kw: state["logged"].append((result, kw)))
    return state


def _breach_report(*rows_and_prices):
    rows = [r for r, _ in rows_and_prices]
    listings = [_listing(extract_item_id(r["ebay_listing_url"]), price=p) for r, p in rows_and_prices]
    return _sync(rows, listings, dry_run=True)   # sync's own units_sold writes are irrelevant here


def _auto(rep, titles=None, **kw):
    titles = titles if titles is not None else {4: "Widget", 5: "Widget"}
    return ebay_sync.auto_reprice_breaches(rep, "svc", COL, "Product Tracker",
                                           title_reader=lambda: titles, **kw)


# eBay $29, cost $30, fee 10% -> net -6.91, break-even 36.74 -> 36.99 (+28%)
_HARD = dict(costco_cost="$30", fee_rate="0.10", sold_90d="4")


def test_auto_reprice_hard_breach_revises_to_break_even_and_syncs_col_h(ebay, writes):
    rep = _breach_report((_row(4, "111111111111", **_HARD), 29.0))
    (r,) = _auto(rep)
    assert ebay["revise"] == [("111111111111", 36.99)]
    assert r["ok"] and r["outcome"] == "repriced" and r["old_price"] == 29.0 and r["new_price"] == 36.99
    assert writes == [(4, [("H", 36.99)])]
    (logged,) = ebay["logged"]
    assert logged[1]["source"] == "auto" and logged[1]["old_price"] == 29.0 and logged[1]["row"] == 4
    assert rep["margin_breach"][0]["auto"] == "repriced"
    assert alert_message(rep) is None                         # fixed -> no "Losing money" line
    assert "auto_repriced 1" in summarize(rep)


def test_auto_reprice_skips_when_break_even_not_above_price(ebay, writes):
    rep = _breach_report((_row(4, "111111111111", **_HARD), 29.0))
    rep["margin_breach"][0]["break_even"] = 28.50             # round_up_99 -> 28.99 <= 29.00
    assert _auto(rep) == [] and ebay["revise"] == [] and writes == []


def test_auto_reprice_ignores_soft_breach(ebay, writes):
    rep = _breach_report((_row(4, "111111111111", costco_cost="$21", fee_rate="0.10", sold_90d="0"), 29.0))
    assert rep["margin_breach"][0]["severity"] == "soft"
    assert _auto(rep) == [] and ebay["revise"] == []


def test_auto_reprice_ignores_non_active_rows(ebay, writes):
    rep = _breach_report((_row(4, "111111111111", status="PAUSED_OOS", **_HARD), 29.0))
    assert rep["margin_breach"][0]["severity"] == "hard"
    assert _auto(rep) == [] and ebay["revise"] == []


def test_auto_reprice_dry_run_never_calls_ebay(ebay, writes):
    rep = _breach_report((_row(4, "111111111111", **_HARD), 29.0))
    (r,) = _auto(rep, dry_run=True)
    assert r["outcome"] == "dry_run" and r["ok"] and ebay["revise"] == [] and writes == []
    assert ebay["logged"] == []


def test_auto_reprice_disabled_does_nothing(ebay, writes):
    rep = _breach_report((_row(4, "111111111111", **_HARD), 29.0))
    assert _auto(rep, enabled=False) == [] and ebay["revise"] == []
    assert "Losing money" in alert_message(rep)


def test_auto_reprice_holds_big_jump_as_likely_bad_cost(ebay, writes):
    # eBay $47.99 vs cost $189.99 (the Tramontina case) -> +300% -> held, alert stays
    rep = _breach_report((_row(4, "111111111111", costco_cost="$189.99", fee_rate="0.13",
                               sold_90d="1"), 47.99))
    (r,) = _auto(rep)
    assert r["outcome"] == "held" and not r["ok"] and ebay["revise"] == [] and writes == []
    assert "check cost" in r["error"]
    assert "Losing money" in alert_message(rep)
    assert "auto_held 1" in summarize(rep)


def test_auto_reprice_row_moved_revises_but_skips_sheet_write(ebay, writes):
    rep = _breach_report((_row(4, "111111111111", **_HARD), 29.0))
    (r,) = _auto(rep, titles={4: "Some Other Item"})
    assert ebay["revise"] and r["outcome"] == "repriced" and writes == []
    assert "row moved" in r["sheet_note"]


def test_auto_reprice_failure_keeps_alert_and_writes_nothing(ebay, writes):
    ebay["result"] = {"ok": False, "item_id": "111111111111", "price": 33.99, "error_kind": "api",
                      "error_code": "21916", "message": "Variation listing"}
    rep = _breach_report((_row(4, "111111111111", **_HARD), 29.0))
    (r,) = _auto(rep)
    assert r["outcome"] == "failed" and "21916" in r["error"] and writes == []
    assert len(ebay["logged"]) == 1                           # failures are logged too
    assert "Losing money" in alert_message(rep)


def test_auto_reprice_auth_error_stops_the_loop(ebay, writes):
    ebay["result"] = {"ok": False, "item_id": "x", "price": 1, "error_kind": "auth",
                      "error_code": "931", "message": "token"}
    rep = _breach_report((_row(4, "111111111111", **_HARD), 29.0),
                         (_row(5, "222222222222", **_HARD), 29.0))
    results = _auto(rep)
    assert len(ebay["revise"]) == 1 and len(results) == 1
    assert "EBAY_AUTH_TOKEN" in results[0]["error"]


def test_format_auto_reprice_summary_variants():
    fmt = ebay_sync.format_auto_reprice_summary
    assert fmt([]) is None
    ok = fmt([{"title": "Tom & Jerry", "row_num": 4, "old_price": 29.0, "new_price": 33.99,
               "outcome": "repriced", "ok": True}])
    assert "Auto-repriced" in ok and "$29.00→$33.99" in ok and "Tom &amp; Jerry" in ok and "row 4" in ok
    bad = fmt([{"title": "W", "old_price": 29.0, "new_price": 33.99, "outcome": "failed",
                "error": "21916: <bad>"}])
    assert "❌" in bad and "21916: &lt;bad&gt;" in bad
    held = fmt([{"title": "W", "old_price": 47.99, "new_price": 249.99, "outcome": "held",
                 "error": "raise >50% — check cost"}])
    assert "⏸" in held and "NOT repriced" in held
    dry = fmt([{"title": "W", "old_price": 29.0, "new_price": 33.99, "outcome": "dry_run", "ok": True}])
    assert "dry run" in dry


def test_run_auto_reprices_and_alert_says_so(monkeypatch, creds, writes, ebay):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_page([_item("111111111111", price="29.00", sold=0)])))
    monkeypatch.setattr(ebay_sync, "load_sheet_rows", lambda *a, **k: [
        _row(4, "111111111111", price="$29.00", sold="0", **_HARD)])
    monkeypatch.setattr(ebay_sync, "_read_titles", lambda *a, **k: {4: "Widget"})
    res = run_ebay_sync(*_cfg_args({}))                       # no config key -> enabled by default
    assert ebay["revise"] == [("111111111111", 36.99)]
    assert writes == [(4, [("H", 36.99)])]
    assert res["alert"].startswith("🛒 <b>eBay sync</b>") and "Auto-repriced to break-even" in res["alert"]
    assert "Losing money" not in res["alert"]
    assert "auto_repriced 1" in res["notes"]


def test_run_auto_reprice_crash_never_breaks_the_sync(monkeypatch, creds, writes):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_page([_item("111111111111", price="29.00", sold=0)])))
    monkeypatch.setattr(ebay_sync, "load_sheet_rows", lambda *a, **k: [
        _row(4, "111111111111", price="$29.00", sold="0", **_HARD)])

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(ebay_sync, "auto_reprice_breaches", boom)
    res = run_ebay_sync(*_cfg_args({}))
    assert res["status"] == "ok" and "Losing money" in res["alert"]


# ── true-sale reconciliation (GetItemTransactions) ────────────────────────────

_TXN_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<GetItemTransactionsResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <TransactionArray>
    <Transaction>
      <TransactionID>111</TransactionID>
      <TransactionPrice currencyID="USD">389.99</TransactionPrice>
      <FinalValueFee currencyID="USD">58.41</FinalValueFee>
      <QuantityPurchased>1</QuantityPurchased>
      <CreatedDate>2026-10-07T10:00:00.000Z</CreatedDate>
    </Transaction>
    <Transaction>
      <TransactionID>222</TransactionID>
      <TransactionPrice currencyID="USD">100.00</TransactionPrice>
      <FinalValueFee currencyID="USD">10.00</FinalValueFee>
      <QuantityPurchased>2</QuantityPurchased>
      <CreatedDate>2026-10-08T10:00:00.000Z</CreatedDate>
    </Transaction>
  </TransactionArray>
</GetItemTransactionsResponse>"""


def test_fetch_item_transactions_parses_real_fee(monkeypatch):
    monkeypatch.setattr(ebay_sync, "_load_credentials", lambda: ("TOKEN", "A", "D", "C", None, ""))
    monkeypatch.setattr(ebay_sync, "_post", lambda body, headers: _TXN_XML)
    txns = ebay_sync.fetch_item_transactions("123456789012")
    assert len(txns) == 2
    assert txns[0]["price"] == 389.99 and txns[0]["fee"] == 58.41 and txns[0]["quantity"] == 1
    assert txns[1]["price"] == 100.0 and txns[1]["fee"] == 10.0 and txns[1]["quantity"] == 2


def test_actual_net_per_unit_reconciles_real_sale():
    # The SimplyGood coffee maker: price 389.99, real fee 58.41, cost 299.99.
    # payout = 389.99 - 58.41 = 331.58; cost+tax = 299.99*1.0825 = 324.74 -> net 6.84.
    assert ebay_sync.actual_net_per_unit({"price": 389.99, "fee": 58.41, "quantity": 1}, 299.99) == 6.84
    # Multi-unit: fee is split across the qty.
    # payout/unit = 100.00 - 10.00/2 = 95.00; cost+tax = 80*1.0825 = 86.60 -> net 8.40.
    assert ebay_sync.actual_net_per_unit({"price": 100.0, "fee": 10.0, "quantity": 2}, 80.0) == 8.40


def test_actual_net_per_unit_unknown_inputs_are_none():
    assert ebay_sync.actual_net_per_unit({"price": None, "fee": 1.0, "quantity": 1}, 10.0) is None
    assert ebay_sync.actual_net_per_unit({"price": 1.0, "fee": None, "quantity": 1}, 10.0) is None
    assert ebay_sync.actual_net_per_unit({"price": 1.0, "fee": 1.0, "quantity": 1}, None) is None


# ── sheet-driven bulk reprice (col BD target_price) + manual-wins re-arm (col BE) ──

COLT = {**COL, "costco_cost": "G", "buy_cost": "BB", "target_price": "BD", "manual_cost": "BE"}


@pytest.fixture
def live(monkeypatch):
    """Stub fetch_item_price (GetItem). state["price"] = live eBay price for every item."""
    state = {"price": 29.0, "status": "Active", "has_variations": False, "calls": [], "result": None}

    def fake(item_id):
        state["calls"].append(item_id)
        return state["result"] or {
            "ok": True, "item_id": item_id, "price": state["price"],
            "listing_status": state["status"], "listing_type": "FixedPriceItem",
            "has_variations": state["has_variations"],
            "error_kind": None, "error_code": "", "message": ""}

    monkeypatch.setattr(ebay_sync, "fetch_item_price", fake)
    return state


def _trow(n, item_id="111111111111", target="34.99", **kw):
    """ACTIVE row, cost $30, fee 10% (break-even 36.74), with a col BD target."""
    base = dict(costco_cost="$30", fee_rate="0.10", sold_90d="4", target_price=target, manual_cost="")
    base.update(kw)
    return _row(n, item_id, **base)


def _apply(rows, fresh=None, **kw):
    """fresh: {row_num: {title, target_price}} as re-read just before the write (default = unchanged)."""
    if fresh is None:
        fresh = {r["row_num"]: {"title": r["title"], "target_price": r.get("target_price", "")}
                 for r in rows}
    return ebay_sync.apply_target_prices("svc", COLT, "Product Tracker", 4, 500, rows=rows,
                                         row_reader=lambda n: fresh.get(n), **kw)


def test_apply_target_revises_syncs_h_clears_bd_and_snapshots_cost(live, ebay, writes):
    rep = _apply([_trow(4, target="$39.99")])
    assert live["calls"] == ["111111111111"]
    assert ebay["revise"] == [("111111111111", 39.99)]
    assert writes == [(4, [("H", 39.99), ("BD", ""), ("BE", 30.0)])]
    (r,) = rep["results"]
    assert r["outcome"] == "applied" and r["old_price"] == 29.0 and r["target"] == 39.99
    (logged,) = ebay["logged"]
    assert logged[1]["source"] == "manual" and logged[1]["old_price"] == 29.0 and logged[1]["row"] == 4
    assert "applied 1" in ebay_sync.summarize_targets(rep)
    msg = ebay_sync.format_target_summary(rep)
    assert "$29.00→$39.99" in msg and "row 4" in msg


def test_apply_target_equal_to_live_is_noop_that_clears_bd(live, ebay, writes):
    live["price"] = 34.99
    rep = _apply([_trow(4, target="34.99", price="$31.00")])
    assert ebay["revise"] == []
    assert writes == [(4, [("H", 34.99), ("BD", ""), ("BE", 30.0)])]
    assert rep["results"][0]["outcome"] == "noop"


def test_apply_target_failure_keeps_bd_and_alerts(live, ebay, writes):
    ebay["result"] = {"ok": False, "item_id": "111111111111", "price": 39.99, "error_kind": "api",
                      "error_code": "21916", "message": "nope"}
    rep = _apply([_trow(4, target="39.99")])
    assert writes == []                                       # BD stays filled -> retried next run
    (r,) = rep["results"]
    assert r["outcome"] == "failed" and "21916" in r["error"]
    assert len(ebay["logged"]) == 1 and ebay["logged"][0][1]["source"] == "manual"
    assert "❌" in ebay_sync.format_target_summary(rep)


def test_apply_target_auth_error_stops_the_sweep(live, ebay, writes):
    ebay["result"] = {"ok": False, "item_id": "x", "price": 1, "error_kind": "auth",
                      "error_code": "931", "message": "token"}
    rep = _apply([_trow(4, target="39.99"), _trow(5, "222222222222", target="39.99")])
    assert len(ebay["revise"]) == 1 and rep["stopped"]
    assert "EBAY_AUTH_TOKEN" in rep["results"][0]["error"]
    assert writes == []


def test_apply_target_getitem_auth_error_stops_before_revise(live, ebay, writes):
    live["result"] = {"ok": False, "item_id": "x", "price": None, "listing_status": "",
                      "error_kind": "auth", "error_code": "932", "message": "token"}
    rep = _apply([_trow(4), _trow(5, "222222222222")])
    assert ebay["revise"] == [] and len(live["calls"]) == 1 and rep["stopped"]


def test_apply_target_skips_non_active_rows(live, ebay, writes):
    rep = _apply([_trow(4, status="PAUSED_MARGIN")])
    assert live["calls"] == [] and ebay["revise"] == [] and writes == []
    (r,) = rep["results"]
    assert r["outcome"] == "skipped" and "not ACTIVE" in r["error"]


def test_apply_target_ignores_blank_bd(live, ebay, writes):
    rep = _apply([_trow(4, target=""), _trow(5, "222222222222", target="  ")])
    assert rep["results"] == [] and live["calls"] == [] and writes == []
    assert ebay_sync.format_target_summary(rep) is None


@pytest.mark.parametrize("url,needle", [("", "fill col Q"), ("https://example.com/x", "fill col Q")])
def test_apply_target_skips_blank_or_unparseable_col_q(live, ebay, writes, url, needle):
    rep = _apply([_trow(4, item_id="", url=url)])
    assert live["calls"] == [] and writes == []
    assert rep["results"][0]["outcome"] == "skipped" and needle in rep["results"][0]["error"]


@pytest.mark.parametrize("target", ["abc", "0", "-5"])
def test_apply_target_bad_value_is_flagged_not_sent(live, ebay, writes, target):
    rep = _apply([_trow(4, target=target)])
    assert live["calls"] == [] and writes == []
    assert rep["results"][0]["outcome"] == "skipped" and "not a valid price" in rep["results"][0]["error"]


def test_apply_target_duplicate_col_q_is_skipped(live, ebay, writes):
    rep = _apply([_trow(4), _trow(5, target="")])          # same item id in two rows
    assert live["calls"] == [] and "2 rows" in rep["results"][0]["error"]


def test_apply_target_below_break_even_warns_but_applies(live, ebay, writes):
    rep = _apply([_trow(4, target="31.00")])                 # break-even 36.74 -> loses money
    assert ebay["revise"] == [("111111111111", 31.0)]
    (r,) = rep["results"]
    assert r["outcome"] == "applied" and any("break-even" in w for w in r["warnings"])
    assert "break-even" in ebay_sync.format_target_summary(rep)


def test_apply_target_big_change_gets_typo_warning(live, ebay, writes):
    rep = _apply([_trow(4, target="99.99")])                 # $29 -> $99.99 = +245%
    (r,) = rep["results"]
    assert r["outcome"] == "applied" and any("typo" in w for w in r["warnings"])


def test_apply_target_multi_variation_is_flagged(live, ebay, writes):
    live["has_variations"] = True
    rep = _apply([_trow(4)])
    assert ebay["revise"] == [] and writes == []
    assert "multi-variation" in rep["results"][0]["error"]


def test_apply_target_listing_not_active_on_ebay_is_skipped(live, ebay, writes):
    live["status"] = "Completed"
    rep = _apply([_trow(4)])
    assert ebay["revise"] == [] and writes == []
    assert "Completed" in rep["results"][0]["error"]


def test_apply_target_dry_run_never_calls_ebay(live, ebay, writes):
    rep = _apply([_trow(4, target="39.99")], dry_run=True)
    assert live["calls"] == [] and ebay["revise"] == [] and writes == [] and ebay["logged"] == []
    assert rep["results"][0]["outcome"] == "dry_run"


def test_apply_target_row_moved_revises_but_writes_nothing(live, ebay, writes):
    rep = _apply([_trow(4)], fresh={4: {"title": "Other", "target_price": "34.99"}})
    assert ebay["revise"] and writes == []
    assert "row moved" in rep["results"][0]["sheet_note"]


def test_apply_target_bd_edited_mid_run_is_kept_for_next_run(live, ebay, writes):
    rep = _apply([_trow(4, target="39.99")], fresh={4: {"title": "Widget", "target_price": "44.99"}})
    assert writes == [(4, [("H", 39.99), ("BE", 30.0)])]     # BD NOT cleared: Jay's new number waits
    assert "changed" in rep["results"][0]["sheet_note"]


def test_apply_target_updates_rows_in_memory(live, ebay, writes):
    row = _trow(4, target="39.99")
    _apply([row])
    assert row["target_price"] == "" and row["manual_cost"] == 30.0 and row["ebay_price"] == 39.99


def test_fetch_item_price_flags_variations(monkeypatch, creds):
    sent = []
    body = ('<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack><Item>'
            '<SellingStatus><CurrentPrice currencyID="USD">41.48</CurrentPrice>'
            "<ListingStatus>Active</ListingStatus></SellingStatus>"
            "<Variations><Variation><SKU>A</SKU></Variation></Variations></Item></GetItemResponse>")
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda req, **k: sent.append(req) or FakeResp(body))
    out = ebay_sync.fetch_item_price("123456789012")
    assert out["ok"] and out["has_variations"] is True
    assert b"<OutputSelector>Item.Variations</OutputSelector>" in sent[0].data


def test_load_sheet_rows_reads_bd_and_be(monkeypatch):
    seen = {}
    raw = [[""] * 57]
    raw[0][0], raw[0][2], raw[0][55], raw[0][56] = "ACTIVE", "Widget", "39.99", "30"

    def fake_read(svc, rng):
        seen["rng"] = rng
        return raw
    monkeypatch.setattr(ebay_sync, "read_sheet", fake_read)
    (row,) = ebay_sync.load_sheet_rows("svc", COLT, "Tab", 4, 500)
    assert seen["rng"] == "'Tab'!A4:BE500"
    assert row["target_price"] == "39.99" and row["manual_cost"] == "30"


# manual-wins / re-arm on the break-even safety net

def test_auto_reprice_skips_manual_price_set_against_current_cost(ebay, writes):
    rep = _breach_report((_row(4, "111111111111", manual_cost="30", **_HARD), 29.0))
    assert _auto(rep) == [] and ebay["revise"] == [] and writes == []
    m = rep["margin_breach"][0]
    assert m["auto"] == "manual"
    alert = alert_message(rep)
    assert "Losing money" in alert and "manual price" in alert   # the loss still surfaces daily
    assert "auto_manual 1" in summarize(rep)


def test_auto_reprice_rearms_when_cost_moved_since_manual_price(ebay, writes):
    # Jay priced at cost $25; Costco now $30 -> his decision is stale -> safety net fires
    rep = _breach_report((_row(4, "111111111111", manual_cost="$25.00", **_HARD), 29.0))
    (r,) = ebay_sync.auto_reprice_breaches(rep, "svc", COLT, "Product Tracker",
                                           title_reader=lambda: {4: "Widget"})
    assert ebay["revise"] == [("111111111111", 36.99)]
    assert writes == [(4, [("H", 36.99), ("BE", "")])]          # BE cleared -> back to unprotected
    assert r["outcome"] == "repriced" and r["rearmed"]
    assert "Costco cost changed" in ebay_sync.format_auto_reprice_summary([r])


def test_auto_reprice_without_manual_cost_never_touches_be(ebay, writes):
    rep = _breach_report((_row(4, "111111111111", **_HARD), 29.0))
    ebay_sync.auto_reprice_breaches(rep, "svc", COLT, "Product Tracker", title_reader=lambda: {4: "Widget"})
    assert writes == [(4, [("H", 36.99)])]


def test_auto_reprice_manual_with_unknown_cost_respects_manual(ebay, writes):
    # G blank (cost basis from BB) -> can't tell the cost moved -> Jay's price stands
    rep = _breach_report((_row(4, "111111111111", costco_cost="", buy_cost="$30", fee_rate="0.10",
                               sold_90d="4", manual_cost="30"), 29.0))
    assert _auto(rep) == [] and ebay["revise"] == []


# run_ebay_sync folds the sweep in

def _run_setup(monkeypatch, rows, ebay_price="29.00"):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_page([_item("111111111111", price=ebay_price, sold=0)])))
    monkeypatch.setattr(ebay_sync, "load_sheet_rows", lambda *a, **k: rows)
    monkeypatch.setattr(ebay_sync, "_read_titles", lambda *a, **k: {r["row_num"]: r["title"] for r in rows})
    monkeypatch.setattr(ebay_sync, "_read_row",
                        lambda svc, COL, sheet, n: {"title": "Widget", "target_price": "31.00"})


def test_run_applies_targets_before_auto_reprice_and_manual_price_survives(monkeypatch, creds, writes,
                                                                          ebay, live):
    rows = [_trow(4, target="31.00", price="$29.00", sold="0")]
    _run_setup(monkeypatch, rows)
    res = run_ebay_sync({}, COLT, "svc", "Product Tracker", 4, 500)
    # Jay's below-break-even $31 is applied and NOT immediately undone by the safety net
    assert ebay["revise"] == [("111111111111", 31.0)]
    assert writes == [(4, [("H", 31.0), ("BD", ""), ("BE", 30.0)])]
    assert "Price changes from the sheet" in res["alert"] and "Losing money" in res["alert"]
    assert "targets applied 1" in res["notes"] and "auto_manual 1" in res["notes"]


def test_run_target_sweep_can_be_disabled(monkeypatch, creds, writes, ebay, live):
    _run_setup(monkeypatch, [_trow(4, target="31.00", price="$29.00", sold="0")])
    run_ebay_sync({"business": {"ebay_sync": {"apply_target_prices": False, "auto_reprice": False}}},
                  COLT, "svc", "Product Tracker", 4, 500)
    assert live["calls"] == [] and ebay["revise"] == []


def test_run_dry_run_never_calls_ebay_for_targets(monkeypatch, creds, writes, ebay, live):
    _run_setup(monkeypatch, [_trow(4, target="31.00", price="$29.00", sold="0")])
    res = run_ebay_sync({}, COLT, "svc", "Product Tracker", 4, 500, dry_run=True)
    assert live["calls"] == [] and ebay["revise"] == [] and writes == []
    assert "targets dry_run 1" in res["notes"]


def test_run_target_sweep_crash_never_breaks_the_sync(monkeypatch, creds, writes, ebay, live):
    _run_setup(monkeypatch, [_trow(4, target="31.00", price="$29.00", sold="0")])

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(ebay_sync, "apply_target_prices", boom)
    res = run_ebay_sync(_ALERT_ONLY, COLT, "svc", "Product Tracker", 4, 500)
    assert res["status"] == "ok"


def test_scheduler_registers_apply_prices_mode(monkeypatch):
    from agents import scheduler
    monkeypatch.setattr(scheduler, "_acquire_lock", lambda mode: False)
    monkeypatch.setattr(sys, "argv", ["scheduler.py", "--mode", "apply_prices", "--dry-run"])
    scheduler.main()


def test_scheduler_apply_prices_mode_sends_summary_and_flags_failures(monkeypatch):
    from agents import scheduler
    sent = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(scheduler, "_send_telegram", lambda tok, chat, text: sent.append(text))

    monkeypatch.setattr(scheduler.ebay_sync, "apply_target_prices",
                        lambda *a, **k: {"results": [], "stopped": False, "dry_run": False})
    out = scheduler.run_apply_prices_mode({}, COLT, "svc", "Tab", 4, 500)
    assert sent == [] and out == {"status": "ok", "notes": "no col BD target prices"}

    failed = {"row_num": 4, "title": "W", "item_id": "1", "target": 9.99, "old_price": 8.0,
              "outcome": "failed", "error": "21916: nope", "warnings": [], "sheet_note": None}
    monkeypatch.setattr(scheduler.ebay_sync, "apply_target_prices",
                        lambda *a, **k: {"results": [failed], "stopped": False, "dry_run": False})
    out = scheduler.run_apply_prices_mode({}, COLT, "svc", "Tab", 4, 500)
    assert len(sent) == 1 and "NOT applied" in sent[0]
    assert out["status"] == "error" and "row 4" in out["errors"] and "targets failed 1" in out["notes"]


# ── sales log (data/sales_log.json) — chunked backfill + recording ────────────

def test_fetch_item_transactions_chunks_90_days_into_30_day_windows(monkeypatch):
    """GetItemTransactions rejects a ModTime window over 30 days — 90 days = 3 calls."""
    import re
    from datetime import datetime as _dt
    bodies = []
    monkeypatch.setattr(ebay_sync, "_load_credentials", lambda: ("TOKEN", "A", "D", "C", None, ""))
    monkeypatch.setattr(ebay_sync, "_post", lambda body, headers: bodies.append(body) or _TXN_XML)
    txns = ebay_sync.fetch_item_transactions("123456789012", days_back=90)
    assert len(bodies) == 3
    for b in bodies:
        fr = _dt.fromisoformat(re.search(r"<ModTimeFrom>(.*?)</ModTimeFrom>", b).group(1).replace("Z", ""))
        to = _dt.fromisoformat(re.search(r"<ModTimeTo>(.*?)</ModTimeTo>", b).group(1).replace("Z", ""))
        assert 0 < (to - fr).total_seconds() <= 30 * 86400
    assert len(txns) == 2                       # same 2 transactions in every window -> deduped


def test_fetch_item_transactions_failed_chunk_returns_empty(monkeypatch):
    """A partial history would look like 'no sales' in those weeks — all or nothing."""
    calls = []

    def post(body, headers):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("boom")
        return _TXN_XML
    monkeypatch.setattr(ebay_sync, "_load_credentials", lambda: ("TOKEN", "A", "D", "C", None, ""))
    monkeypatch.setattr(ebay_sync, "_post", post)
    assert ebay_sync.fetch_item_transactions("123456789012", days_back=90) == []


@pytest.fixture
def txns(monkeypatch):
    st = {"calls": [], "txns": [{"transaction_id": "T1", "price": 100.0, "fee": 15.0, "quantity": 1,
                                 "created": "2026-10-07T10:00:00.000Z"}]}

    def fake(item_id, days_back=30):
        st["calls"].append((item_id, days_back))
        return list(st["txns"])
    monkeypatch.setattr(ebay_sync, "fetch_item_transactions", fake)
    return st


_SALES_COL = {**COL, "actual_net": "BC"}


def test_sync_actual_sales_records_log_and_backfills_90_days_first(txns, writes):
    from tools import sales_log
    rows = [_row(4, "111111111111", costco_cost="$50")]
    stats = ebay_sync.sync_actual_sales("svc", "Tab", _SALES_COL, rows, [_listing("111111111111", sold=1)])
    assert txns["calls"] == [("111111111111", 90)]           # first sight -> 90-day backfill
    log = sales_log.load()
    assert log["111111111111"][0]["transaction_id"] == "T1" and log["111111111111"][0]["net"] == 30.88
    assert stats["logged"] == 1 and writes == [(4, [("BC", 30.88)])]

    ebay_sync.sync_actual_sales("svc", "Tab", _SALES_COL, rows, [_listing("111111111111", sold=1)])
    assert txns["calls"][-1] == ("111111111111", 30)          # known item -> normal 30-day window
    assert len(sales_log.load()["111111111111"]) == 1          # deduped


def test_sync_actual_sales_logs_sale_even_without_cost(txns, writes):
    from tools import sales_log
    rows = [_row(4, "111111111111")]                           # no costco_cost / buy_cost
    ebay_sync.sync_actual_sales("svc", "Tab", _SALES_COL, rows, [_listing("111111111111", sold=1)])
    assert sales_log.load()["111111111111"][0]["net"] is None
    assert writes == []                                         # no actual_net without a cost


def test_sync_actual_sales_dry_run_writes_no_log(txns, writes):
    from tools import sales_log
    rows = [_row(4, "111111111111", costco_cost="$50")]
    ebay_sync.sync_actual_sales("svc", "Tab", _SALES_COL, rows, [_listing("111111111111", sold=1)],
                                dry_run=True)
    assert sales_log.load() == {} and writes == []


def test_sync_actual_sales_skips_unsold_listings(txns, writes):
    rows = [_row(4, "111111111111", costco_cost="$50")]
    ebay_sync.sync_actual_sales("svc", "Tab", _SALES_COL, rows, [_listing("111111111111", sold=0)])
    assert txns["calls"] == []


def test_missing_final_value_fee_is_estimated_from_the_real_sale_price(txns, writes):
    """Managed payments: GetItemTransactions sends no FinalValueFee (verified live 2026-10-16) —
    estimate it like col I (price x fee_rate x 1.08 + $0.30) and flag it."""
    from tools import sales_log
    txns["txns"] = [{"transaction_id": "T1", "price": 389.99, "fee": None, "quantity": 1,
                     "created": "2026-10-08T19:33:04.000Z"}]
    rows = [_row(4, "111111111111", costco_cost="$299.99", fee_rate="0.1325")]
    ebay_sync.sync_actual_sales("svc", "Tab", _SALES_COL, rows, [_listing("111111111111", sold=1)])
    (e,) = sales_log.load()["111111111111"]
    fee = round(389.99 * 0.1325 * 1.08 + 0.30, 2)                 # 56.11
    assert e["fee"] == fee and e["fee_estimated"] is True
    assert e["net"] == round(389.99 - fee - 299.99 * 1.0825, 2)
    assert writes == [(4, [("BC", e["net"])])]


def test_missing_fee_and_unknown_fee_rate_stays_unknown(txns, writes):
    from tools import sales_log
    txns["txns"] = [{"transaction_id": "T1", "price": 100.0, "fee": None, "quantity": 1,
                     "created": "2026-10-08T19:33:04.000Z"}]
    rows = [_row(4, "111111111111", costco_cost="$50")]          # no fee_rate in col AB
    ebay_sync.sync_actual_sales("svc", "Tab", _SALES_COL, rows, [_listing("111111111111", sold=1)])
    assert sales_log.load()["111111111111"][0]["net"] is None and writes == []


def test_estimate_fee_multi_quantity():
    assert ebay_sync.estimate_fee(100.0, 2, 0.10) == round(200 * 0.10 * 1.08 + 0.30, 2)
    assert ebay_sync.estimate_fee(None, 1, 0.10) is None and ebay_sync.estimate_fee(10.0, 1, None) is None


_REFUND_TXN_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<GetItemTransactionsResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <TransactionArray>
    <Transaction>
      <TransactionID>10085581108719</TransactionID>
      <TransactionPrice currencyID="USD">103.99</TransactionPrice>
      <QuantityPurchased>1</QuantityPurchased>
      <CreatedDate>2026-10-15T11:44:35.000Z</CreatedDate>
      <MonetaryDetails><Payments><Payment><PaymentStatus>Succeeded</PaymentStatus>
        <PaymentAmount currencyID="USD">103.99</PaymentAmount></Payment></Payments>
        <Refunds><Refund><RefundStatus>Succeeded</RefundStatus><RefundType>PaymentRefund</RefundType>
          <RefundAmount currencyID="USD">-88.07</RefundAmount></Refund>
        <Refund><RefundStatus>Failed</RefundStatus><RefundAmount currencyID="USD">-5.00</RefundAmount></Refund>
        </Refunds></MonetaryDetails>
    </Transaction>
    <Transaction>
      <TransactionID>10085028185904</TransactionID>
      <TransactionPrice currencyID="USD">41.48</TransactionPrice>
      <QuantityPurchased>3</QuantityPurchased>
      <CreatedDate>2026-10-06T22:05:07.000Z</CreatedDate>
      <ShippedTime>2026-10-08T15:00:00.000Z</ShippedTime>
    </Transaction>
  </TransactionArray>
</GetItemTransactionsResponse>"""


def test_fetch_item_transactions_reads_refunds_and_shipping(monkeypatch):
    bodies = []
    monkeypatch.setattr(ebay_sync, "_load_credentials", lambda: ("TOKEN", "A", "D", "C", None, ""))
    monkeypatch.setattr(ebay_sync, "_post", lambda body, headers: bodies.append(body) or _REFUND_TXN_XML)
    gp, es = ebay_sync.fetch_item_transactions("123456789012")
    assert gp["refunded"] == 88.07 and gp["shipped"] is False      # failed refund ignored
    assert es["refunded"] == 0.0 and es["shipped"] is True and es["quantity"] == 3
    for sel in ("MonetaryDetails", "ShippedTime"):
        assert f"<OutputSelector>TransactionArray.Transaction.{sel}</OutputSelector>" in bodies[0]
    assert "<DetailLevel>ReturnAll</DetailLevel>" in bodies[0]


def test_sync_actual_sales_rechecks_logged_items_that_left_the_active_list(txns, writes):
    """A sold-out listing drops off ActiveList — its logged sales still get refund updates."""
    from tools import sales_log
    rows = [_row(4, "111111111111", costco_cost="$50")]
    ebay_sync.sync_actual_sales("svc", "Tab", _SALES_COL, rows, [_listing("111111111111", sold=1)])
    ebay_sync.sync_actual_sales("svc", "Tab", _SALES_COL, rows, [])          # listing gone
    assert len(txns["calls"]) == 2 and len(sales_log.load()["111111111111"]) == 1


def test_actual_net_skips_cancelled_latest_sale(txns, writes):
    txns["txns"] = [
        {"transaction_id": "T1", "price": 100.0, "fee": 15.0, "quantity": 1,
         "created": "2026-10-07T10:00:00.000Z", "refunded": 0.0, "shipped": True},
        {"transaction_id": "T2", "price": 200.0, "fee": 30.0, "quantity": 1,
         "created": "2026-10-09T10:00:00.000Z", "refunded": 200.0, "shipped": False}]
    rows = [_row(4, "111111111111", costco_cost="$50")]
    ebay_sync.sync_actual_sales("svc", "Tab", _SALES_COL, rows, [_listing("111111111111", sold=2)])
    assert writes == [(4, [("BC", 30.88)])]                  # from T1, not the cancelled T2


# ── col U excludes cancelled / refunded units (sales log) ──────────────────────

def _log_with_cancel(item_id="111111111111"):
    from tools import sales_log
    log = {}
    sales_log.add_transactions(log, item_id, [
        {"transaction_id": "T1", "price": 100.0, "fee": 15.0, "quantity": 1,
         "created": "2026-10-07T10:00:00Z", "refunded": 0.0, "shipped": True},
        {"transaction_id": "T2", "price": 100.0, "fee": 15.0, "quantity": 1,
         "created": "2026-10-09T10:00:00Z", "refunded": 88.07, "shipped": False},   # cancelled
    ], 50.0)
    sales_log.save(log)
    return log


def test_col_u_excludes_cancelled_units_from_the_sales_log(writes):
    _log_with_cancel()
    rep = _sync([_row(4, "111111111111", sold="0")], [_listing("111111111111", sold=2)])
    assert writes == [(4, [("U", 1)])]                           # 2 sold on eBay - 1 cancelled
    (m,) = rep["matched"]
    assert m["quantity_sold"] == 1 and m["cancelled_units"] == 1


def test_col_u_already_net_of_cancellations_is_not_rewritten(writes):
    _log_with_cancel()
    _sync([_row(4, "111111111111", sold="1")], [_listing("111111111111", sold=2)])
    assert writes == []


def test_col_u_without_log_history_uses_raw_quantity_sold(writes):
    _log_with_cancel(item_id="999999999999")                     # cancellation on another item
    _sync([_row(4, "111111111111", sold="0")], [_listing("111111111111", sold=2)])
    assert writes == [(4, [("U", 2)])]


def test_cancelled_units_never_push_col_u_below_zero(writes):
    from tools import sales_log
    log = {}
    sales_log.add_transactions(log, "111111111111", [
        {"transaction_id": "T9", "price": 50.0, "fee": 5.0, "quantity": 3,
         "created": "2026-10-09T10:00:00Z", "refunded": 150.0, "shipped": True}], 20.0)
    sales_log.save(log)
    _sync([_row(4, "111111111111", sold="1")], [_listing("111111111111", sold=1)])
    assert writes == [(4, [("U", 0)])]


def test_cancelled_units_helper():
    from tools import sales_log
    log = _log_with_cancel()
    assert sales_log.cancelled_units(log) == {"111111111111": 1}
    assert sales_log.cancelled_units({}) == {}


def test_run_reconciles_sales_before_writing_col_u(monkeypatch, creds, writes, txns):
    """A cancellation discovered this run lands in col U in the SAME run."""
    txns["txns"] = [{"transaction_id": "T2", "price": 103.99, "fee": 15.0, "quantity": 1,
                     "created": "2026-10-15T11:44:35Z", "refunded": 88.07, "shipped": False}]
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_page([_item("111111111111", price="40.00", sold=1)])))
    rows = [_row(4, "111111111111", price="$40.00", sold="1", costco_cost="$20", fee_rate="0.10")]
    monkeypatch.setattr(ebay_sync, "load_sheet_rows", lambda *a, **k: rows)
    monkeypatch.setattr(ebay_sync, "_read_titles", lambda *a, **k: {4: "Widget"})
    run_ebay_sync(_ALERT_ONLY, COL, "svc", "Product Tracker", 4, 500)
    assert (4, [("U", 0)]) in writes


# ── col H mirrors eBay's live price ───────────────────────────────────────────

def _mirror(rep, titles=None, **kw):
    titles = titles if titles is not None else {4: "Widget", 5: "Widget"}
    return ebay_sync.mirror_live_prices(rep, "svc", COL, "Product Tracker",
                                        title_reader=lambda: titles, **kw)


def test_mirror_writes_col_h_when_ebay_price_differs(writes):
    rep = _sync([_row(4, "111111111111", price="$29.99")], [_listing("111111111111", price=40.0)],
                dry_run=True)
    out = _mirror(rep)
    assert writes == [(4, [("H", 40.0)])]
    assert out == [{"row_num": 4, "title": "Widget", "item_id": "111111111111",
                    "old_price": 29.99, "new_price": 40.0, "outcome": "mirrored"}]
    assert "h_mirrored 1" in ebay_sync.summarize({**rep, "h_mirror": out})


def test_mirror_no_write_when_col_h_already_matches(writes):
    rep = _sync([_row(4, "111111111111", price="$40.00")], [_listing("111111111111", price=40.0)],
                dry_run=True)
    assert _mirror(rep) == [] and writes == []


def test_mirror_fills_a_blank_col_h(writes):
    rep = _sync([_row(4, "111111111111", price="")], [_listing("111111111111", price=40.0)],
                dry_run=True)
    _mirror(rep)
    assert writes == [(4, [("H", 40.0)])]


def test_mirror_skips_non_active_rows(writes):
    rep = _sync([_row(4, "111111111111", status="PAUSED_OOS", price="$29.99")],
                [_listing("111111111111", price=40.0)], dry_run=True)
    assert _mirror(rep) == [] and writes == []


def test_mirror_skips_items_repriced_this_run(writes):
    rep = _sync([_row(4, "111111111111", price="$29.99")], [_listing("111111111111", price=40.0)],
                dry_run=True)
    assert _mirror(rep, skip_ids={"111111111111"}) == [] and writes == []


def test_mirror_row_moved_writes_nothing(writes):
    rep = _sync([_row(4, "111111111111", price="$29.99")], [_listing("111111111111", price=40.0)],
                dry_run=True)
    (r,) = _mirror(rep, titles={4: "Something Else"})
    assert r["outcome"] == "row_moved" and writes == []


def test_mirror_dry_run_writes_nothing(writes):
    rep = _sync([_row(4, "111111111111", price="$29.99")], [_listing("111111111111", price=40.0)],
                dry_run=True)
    (r,) = _mirror(rep, dry_run=True)
    assert r["outcome"] == "dry_run" and writes == []


def test_mirror_never_calls_ebay_or_touches_manual_cost(monkeypatch, writes):
    monkeypatch.setattr(ebay_sync, "revise_fixed_price", lambda *a, **k: pytest.fail("eBay write"))
    monkeypatch.setattr(ebay_sync, "fetch_item_price", lambda *a, **k: pytest.fail("eBay call"))
    rep = _sync([_row(4, "111111111111", price="$29.99", manual_cost="25")],
                [_listing("111111111111", price=40.0)], dry_run=True)
    _mirror(rep)
    assert writes == [(4, [("H", 40.0)])]                       # H only — never BE


def test_run_auto_repriced_row_keeps_break_even_in_col_h(monkeypatch, creds, writes, ebay):
    """The mirror runs after auto-reprice, and the listing data is pre-revision: the repriced
    row must keep the break-even price, not get the stale eBay price written back."""
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_page([_item("111111111111", price="29.00", sold=0)])))
    monkeypatch.setattr(ebay_sync, "load_sheet_rows", lambda *a, **k: [
        _row(4, "111111111111", price="$27.00", sold="0", **_HARD)])
    monkeypatch.setattr(ebay_sync, "_read_titles", lambda *a, **k: {4: "Widget"})
    run_ebay_sync({}, COL, "svc", "Product Tracker", 4, 500)
    assert ebay["revise"] == [("111111111111", 36.99)]
    assert writes == [(4, [("H", 36.99)])]                      # no stale H=29.00 afterwards


def test_run_mirrors_bulk_edited_ebay_price(monkeypatch, creds, writes):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(_page([_item("111111111111", price="44.99", sold=0)])))
    monkeypatch.setattr(ebay_sync, "load_sheet_rows", lambda *a, **k: [
        _row(4, "111111111111", price="$39.99", sold="0", costco_cost="$10", fee_rate="0.10",
             sold_90d="3")])
    monkeypatch.setattr(ebay_sync, "_read_titles", lambda *a, **k: {4: "Widget"})
    res = run_ebay_sync(_ALERT_ONLY, COL, "svc", "Product Tracker", 4, 500)
    assert writes == [(4, [("H", 44.99)])] and "h_mirrored 1" in res["notes"]


# ── revise_picture (branded-photo backfill) ──────────────────────────────────

EPS_MAIN = "https://i.ebayimg.com/00/s/MTI4MFgxMjAw/z/EBIAAeSw~6pqxut7/$_1.JPG?set_id=2"
EPS_G1 = "https://i.ebayimg.com/00/s/NzI3WDcyNw==/z/AWIAAeSw--5qyQiF/$_1.JPG?set_id=2"


def test_fetch_item_price_returns_picture_urls(monkeypatch, creds):
    sent = []
    body = ('<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack><Item>'
            '<SellingStatus><CurrentPrice currencyID="USD">41.48</CurrentPrice>'
            "<ListingStatus>Active</ListingStatus></SellingStatus>"
            f"<PictureDetails><PictureURL>{EPS_MAIN.replace('&', '&amp;')}</PictureURL>"
            f"<PictureURL>{EPS_G1}</PictureURL></PictureDetails></Item></GetItemResponse>")
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda req, **k: sent.append(req) or FakeResp(body))
    out = ebay_sync.fetch_item_price("123456789012")
    assert out["picture_urls"] == [EPS_MAIN, EPS_G1]
    assert b"<OutputSelector>Item.PictureDetails</OutputSelector>" in sent[0].data


def test_revise_picture_sends_main_then_gallery_only(monkeypatch, creds):
    sent = []
    ok = '<ReviseFixedPriceItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack></ReviseFixedPriceItemResponse>'
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda req, **k: sent.append(req) or FakeResp(ok))
    out = ebay_sync.revise_picture("123456789012", EPS_MAIN, [EPS_G1, EPS_MAIN],
                                   listing_type="FixedPriceItem")
    assert out["ok"] and out["pictures"] == [EPS_MAIN, EPS_G1]          # deduped, main first
    body = sent[0].data.decode()
    assert {k.lower(): v for k, v in sent[0].header_items()}["x-ebay-api-call-name"] == "ReviseFixedPriceItem"
    assert body.index("z/EBIAAeSw") < body.index("z/AWIAAeSw")
    assert "<PictureDetails>" in body and "StartPrice" not in body and "<Quantity>" not in body


@pytest.mark.parametrize("main,gallery,ltype,why", [
    ("https://www.costco.com/x.jpg", [], None, "main picture must be EPS"),
    (EPS_MAIN, ["https://images.costco-static.com/a.jpg"], None, "non-EPS"),
    (EPS_MAIN, [], "Chinese", "not FixedPriceItem"),
])
def test_revise_picture_guards_never_post(monkeypatch, creds, main, gallery, ltype, why):
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("must not hit the network"))
    out = ebay_sync.revise_picture("123456789012", main, gallery, listing_type=ltype)
    assert out["ok"] is False and out["error_kind"] == "invalid" and why in out["message"]


def test_revise_picture_caps_at_24(monkeypatch, creds):
    sent = []
    ok = '<ReviseFixedPriceItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack></ReviseFixedPriceItemResponse>'
    monkeypatch.setattr(ebay_sync.urllib.request, "urlopen",
                        lambda req, **k: sent.append(req) or FakeResp(ok))
    gallery = [f"https://i.ebayimg.com/00/s/x/z/G{i}/$_1.JPG" for i in range(30)]
    out = ebay_sync.revise_picture("123456789012", EPS_MAIN, gallery)
    assert len(out["pictures"]) == 24 and out["pictures"][0] == EPS_MAIN
