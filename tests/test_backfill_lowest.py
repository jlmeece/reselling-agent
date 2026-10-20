"""--mode backfill_lowest: re-scrape eBay active comps, write ONLY col BF. Sheet, Chrome, eBay and
Telegram are all stubbed — nothing live."""
import os
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents.scheduler as sch
import agents.telegram_bot as tb
from tools import ebay_research
from agents.scheduler import col_to_idx

COL = {"status": "A", "title": "C", "category": "D", "lowest_active": "BF"}


def _row(status, title, bf=""):
    row = [""] * (col_to_idx("BF") + 1)
    row[0], row[2], row[3], row[col_to_idx("BF")] = status, title, "Jewelry", bf
    return row


@pytest.fixture
def env(monkeypatch):
    st = {"rows": [], "writes": [], "comps": {}, "tg": [], "browser": 0, "calls": []}
    monkeypatch.setattr(sch, "read_sheet", lambda svc, rng: st["rows"])
    monkeypatch.setattr(sch, "write_row_partial",
                        lambda svc, sheet, row, pairs: st["writes"].append((row, list(pairs))))

    @contextmanager
    def fake_browser():
        st["browser"] += 1
        yield "page"
    monkeypatch.setattr(sch, "make_browser", fake_browser)

    def fake_comps(title, category=None, page=None, brand=None, model=None, ebay_category_id=None):
        st["calls"].append((title, page, brand, model, ebay_category_id))
        res = st["comps"][title]
        if isinstance(res, Exception):
            raise res
        return {"lowest_active": res}
    monkeypatch.setattr(ebay_research, "get_ebay_comps", fake_comps)
    monkeypatch.setattr(sch.time, "sleep", lambda s: None)
    monkeypatch.setattr(sch.sys, "platform", "win32")
    monkeypatch.setattr(sch, "_send_telegram", lambda tok, chat, text: st["tg"].append(text) or True)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    return st


def _run(**kw):
    return sch.run_backfill_lowest({}, COL, "svc", "Product Tracker", 4, 500, **kw)


def test_writes_only_bf_for_eligible_rows_one_browser(env):
    env["rows"] = [_row("ACTIVE", "Ring A"), _row("PENDING", "Skip me"), _row("SCORED", "Ring B", "9.99"),
                   _row("READY", ""), _row("APPROVED", "Ring C")]
    env["comps"] = {"Ring A": 19.99, "Ring B": None, "Ring C": RuntimeError("eBay down")}
    result = _run()
    assert env["writes"] == [(4, [("BF", 19.99)])]          # Ring B kept its 9.99, Ring C errored
    assert env["browser"] == 1
    assert [c[0] for c in env["calls"]] == ["Ring A", "Ring B", "Ring C"]
    assert all(c[1] == "page" and c[2:] == (None, None, None) for c in env["calls"])
    assert env["tg"] == ["⚡ Backfilled cheapest-competitor price: 1 of 3 rows (1 no-comps, 1 errors)"]
    assert result["status"] == "ok"


def test_limit_caps_rows(env):
    env["rows"] = [_row("ACTIVE", "Ring A"), _row("ACTIVE", "Ring B")]
    env["comps"] = {"Ring A": 10.0, "Ring B": 11.0}
    _run(limit=1)
    assert env["writes"] == [(4, [("BF", 10.0)])]


def test_dry_run_writes_nothing_and_opens_no_browser(env):
    env["rows"] = [_row("ACTIVE", "Ring A")]
    result = _run(dry_run=True)
    assert env["writes"] == [] and env["browser"] == 0 and env["tg"] == [] and env["calls"] == []
    assert "DRY RUN" in result["notes"]


def test_no_eligible_rows(env):
    env["rows"] = [_row("PENDING", "Ring A")]
    assert "no SCORED" in _run()["notes"] and env["browser"] == 0


def test_backfill_lowest_in_ops_menu():
    assert "backfill_lowest" in tb._JOB_MODES
    assert tb._JOB_LABELS["backfill_lowest"] == "⚡ Backfill competitive prices"
    assert all(m in tb._JOB_LABELS for m in tb._JOB_MODES)
