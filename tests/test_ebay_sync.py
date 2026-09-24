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
    return (f"<Item><ItemID>{item_id}</ItemID><Title>{_esc(title)}</Title><Quantity>{qty}</Quantity>"
            f"<SellingStatus><CurrentPrice currencyID=\"USD\">{price}</CurrentPrice>"
            f"<QuantitySold>{sold}</QuantitySold></SellingStatus>"
            f"<WatchCount>{watch}</WatchCount><HitCount>{hits}</HitCount></Item>")


def _page(items, page=1, total=1, ack="Success"):
    return (f'<?xml version="1.0"?><GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
            f"<Ack>{ack}</Ack><ActiveList><ItemArray>{''.join(items)}</ItemArray>"
            f"<PaginationResult><TotalNumberOfPages>{total}</TotalNumberOfPages></PaginationResult>"
            f"</ActiveList></GetMyeBaySellingResponse>")


def _failure(code, msg="bad"):
    return (f'<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Failure</Ack>'
            f"<Errors><SeverityCode>Error</SeverityCode><ErrorCode>{code}</ErrorCode>"
            f"<ShortMessage>{msg}</ShortMessage></Errors></GetMyeBaySellingResponse>")


def _row(n, item_id="", title="Widget", status="ACTIVE", platform="eBay", price="$29.99", sold="0", url=None):
    if url is None:
        url = f"https://www.ebay.com/itm/{item_id}" if item_id else ""
    return {"row_num": n, "title": title, "status": status, "platform": platform,
            "ebay_price": price, "ebay_listing_url": url, "units_sold": sold}


def _listing(item_id, price=29.99, sold=0, qty=5, title="Widget"):
    return {"item_id": item_id, "title": title, "price": price, "quantity": qty,
            "quantity_sold": sold, "watch_count": 0, "view_count": 0}


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


# ── sync: price mismatch ──────────────────────────────────────────────────────

def test_price_mismatch_detected_and_never_written(writes):
    rows = [_row(4, "111111111111", price="$29.99", sold="0")]
    rep = _sync(rows, [_listing("111111111111", price=34.99, sold=0)])
    assert rep["price_mismatch"] == [{"row_num": 4, "title": "Widget", "item_id": "111111111111",
                                      "ebay_price": 34.99, "sheet_price": 29.99}]
    assert writes == []  # flag-only: no price write, no units change


def test_price_within_a_cent_or_blank_sheet_price_is_not_a_mismatch(writes):
    rows = [_row(4, "111111111111", price="$29.99"),
            _row(5, "222222222222", price=""),
            _row(6, "333333333333", price="$1,299.00")]
    rep = _sync(rows, [_listing("111111111111", price=29.994), _listing("222222222222", price=5.0),
                       _listing("333333333333", price=1299.0)])
    assert rep["price_mismatch"] == []


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
    assert "sold out" in flagged[4] and "never listed" in flagged[7] and "parseable" in flagged[8]


# ── report formatting ─────────────────────────────────────────────────────────

def test_alert_message_none_when_clean(writes):
    rep = _sync([_row(4, "111111111111", sold="0")], [_listing("111111111111", sold=1)])
    assert alert_message(rep) is None                    # only units changed -> silent
    rep2 = _sync([], [_listing("999999999999")])
    assert alert_message(rep2) is None                   # only not-in-sheet -> silent


def test_alert_message_escapes_and_truncates_and_caps(writes):
    rows = [_row(i, f"{100000000000 + i}", title="<b>Tom & Jerry</b> " + "x" * 80) for i in range(4, 30)]
    listings = [_listing(f"{100000000000 + i}", price=50.0) for i in range(4, 30)]
    rep = _sync(rows, listings)
    msg = alert_message(rep)
    assert "&lt;b&gt;Tom &amp; Jerry" in msg and "<b>Tom" not in msg
    assert "…and 11 more" in msg
    assert len(msg) < 4096


# ── run_ebay_sync orchestration ───────────────────────────────────────────────

def _cfg_args():
    return ({}, COL, "svc", "Product Tracker", 4, 500)


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
    rows = [_row(4, "111111111111", price="$29.99", sold="0"),
            _row(5, "222222222222", status="ACTIVE")]
    monkeypatch.setattr(ebay_sync, "load_sheet_rows", lambda *a, **k: rows)
    monkeypatch.setattr(ebay_sync, "_read_titles", lambda *a, **k: {4: "Widget", 5: "Widget"})
    res = run_ebay_sync(*_cfg_args())

    assert writes == [(4, [("U", 2)])]
    assert res["status"] == "ok"
    assert "matched 1" in res["notes"] and "price_mismatch 1" in res["notes"] \
        and "active_not_on_ebay 1" in res["notes"]
    assert res["alert"] and "Price mismatch" in res["alert"]


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
