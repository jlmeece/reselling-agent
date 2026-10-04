"""agents/scheduler.py run_apply_scheduled — pre-stage + timed apply (all I/O mocked).

Energy Shot: on sale $31.99 (regular $39.99) until 2026-10-19T06:59Z, eBay $41.48.
State files live in tmp (conftest autouse fixture)."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from agents import scheduler as sch
from tools import sale_schedule as sched

COL = sch.load_col_map()
CONFIG = {"business": {"price_change_threshold": 0.50},
          "categories": {"Pharmacy": {"fee_rate": 0.1325, "ad_rate": 0.0}}}
START = 4
ITEM = "123456789012"
END = datetime(2026, 10, 19, 6, 59, tzinfo=timezone.utc)
AFTER = END + timedelta(minutes=5)
URL = "https://www.costco.com/p/-/kirkland-signature-energy-shot-48-bottles-2-ounces-each/4000099948"


def _row(status="ACTIVE", ebay_url=f"https://www.ebay.com/itm/{ITEM}", G="31.99", H="41.48"):
    row = []
    for key, val in dict(status=status, title="Kirkland Signature Energy Shot", category="Pharmacy",
                         costco_url=URL, costco_cost=G, ebay_price=H, fee_rate="13.25%",
                         ship_cost="0", regular_price="39.99", sale_info="🔥 -$8 ends 10/18/26",
                         price_change="", ebay_listing_url=ebay_url, sku="4000099948").items():
        idx = sch.col_to_idx(COL[key])
        while len(row) <= idx:
            row.append("")
        row[idx] = val
    return row


def _scrape(price=39.99, on_sale=False, end=None):
    return {"price": price, "on_sale": on_sale, "sale_end_ts": end, "error": None,
            "original_price": 39.99 if on_sale else None, "sale_savings": 8.0 if on_sale else None,
            "sale_expires": None}


PROMPT = {"item_id": ITEM, "title": "Kirkland Signature Energy Shot", "sku": "4000099948",
          "target": 50.99, "sale_end_ts": END.isoformat(), "costco_pid": "4000099948",
          "costco_url": URL, "old_cost": 31.99, "new_cost": 39.99, "ebay_price": 41.48}


@pytest.fixture
def env(monkeypatch):
    st = {"rows": [_row()], "scrape": _scrape(), "live": {"ok": True, "price": 41.48,
          "listing_status": "Active", "error_kind": None, "error_code": "", "message": ""},
          "revise": None, "end": None, "calls": [], "writes": [], "notes": [], "prompts": [],
          "logs": [], "lock": True, "browser": 0}

    monkeypatch.setattr(sch, "read_sheet", lambda *a, **k: st["rows"])

    @contextmanager
    def _browser():
        st["browser"] += 1
        yield object()
    monkeypatch.setattr(sch, "make_browser", _browser)
    monkeypatch.setattr(sch, "scrape_costco", lambda url, page=None: st["scrape"])
    monkeypatch.setattr(sch, "fetch_item_price", lambda i: st["live"])

    def revise(i, p):
        st["calls"].append(("revise", i, p))
        return st["revise"] or {"ok": True, "item_id": i, "price": p, "error_kind": None,
                                "error_code": "", "message": ""}

    def end(i):
        st["calls"].append(("end", i))
        return st["end"] or {"ok": True, "item_id": i, "price": None, "error_kind": None,
                             "error_code": "", "message": "ended"}
    monkeypatch.setattr(sch, "revise_fixed_price", revise)
    monkeypatch.setattr(sch, "end_fixed_price", end)
    monkeypatch.setattr(sch, "safe_write_row",
                        lambda svc, sheet, row, pairs: st["writes"].append((row, dict(pairs))))
    monkeypatch.setattr(sch, "log_revise", lambda svc, res, **k: st["logs"].append((res, k)))
    monkeypatch.setattr(sch, "_notify", lambda text: st["notes"].append(text) or True)
    monkeypatch.setattr(sch, "send_reprice_prompt",
                        lambda item, keyboard=None: st["prompts"].append((item, keyboard)) or True)
    monkeypatch.setattr(sch, "_acquire_lock", lambda mode: st["lock"])
    monkeypatch.setattr(sch, "_release_lock", lambda: None)

    def run(now=AFTER):
        return sch.run_apply_scheduled(CONFIG, COL, object(), "Product Tracker", START, 500, now=now)
    st["run"] = run
    return st


def _schedule(action="reprice"):
    sched.record_prompt(PROMPT)
    sched.schedule_action(ITEM, action, PROMPT)


# ── quiet tick ───────────────────────────────────────────────────────────────

def test_nothing_due_is_silent_no_lock_no_chrome_no_run_log(env, monkeypatch):
    monkeypatch.setattr(sch, "_acquire_lock", lambda mode: pytest.fail("must not take the lock"))
    assert env["run"]() is None
    assert env["browser"] == 0 and env["calls"] == [] and env["notes"] == []


def test_scheduled_but_not_yet_due_is_silent(env):
    _schedule()
    assert env["run"](now=END - timedelta(hours=1)) is None
    assert env["browser"] == 0 and sched.get_action(ITEM)


# ── pre-stage ────────────────────────────────────────────────────────────────

def test_prestage_sends_schedule_prompt_once(env):
    sched.record_sale_end("4000099948", END.isoformat(), regular_price=39.99)
    res = env["run"](now=END - timedelta(hours=20))
    (item, kb), = env["prompts"]
    assert item["target"] == 50.99 and item["sale_end_ts"] == END.isoformat()
    datas = [b["callback_data"] for r in kb["inline_keyboard"] for b in r]
    assert datas == [f"reprice:sched:{ITEM}:5099", f"reprice:schedend:{ITEM}", f"reprice:ignore:{ITEM}"]
    assert "prompted" in res["notes"] and sched.get_prompt(ITEM)
    assert env["browser"] == 0                                   # pre-stage never needs Chrome
    env["run"](now=END - timedelta(hours=19))
    assert len(env["prompts"]) == 1                              # not re-sent next tick


def test_prestage_failed_send_retries_next_tick(env, monkeypatch):
    sched.record_sale_end("4000099948", END.isoformat(), regular_price=39.99)
    monkeypatch.setattr(sch, "send_reprice_prompt", lambda item, keyboard=None: False)
    env["run"](now=END - timedelta(hours=20))
    assert sched.get_prompt(ITEM) is None                        # not recorded -> retried


# ── reprice ──────────────────────────────────────────────────────────────────

def test_due_reprice_after_live_check_revises_and_writes(env):
    _schedule()
    res = env["run"]()
    assert env["calls"] == [("revise", ITEM, 50.99)]
    (row, w), = env["writes"]
    assert row == START
    assert w[COL["ebay_price"]] == 50.99 and w[COL["price_change"]] == ""
    assert w[COL["costco_cost"]] == 39.99 and w[COL["sale_info"]] == ""     # sale badge cleared
    assert sched.get_action(ITEM) is None and sched.recently_applied(ITEM, now=AFTER)
    assert env["logs"][0][1]["source"] == "scheduled"
    assert "repriced to $50.99" in env["notes"][0]
    assert "repriced" in res["notes"] and res["status"] == "ok"
    assert env["browser"] == 1


def test_sale_extended_reschedules_and_changes_nothing(env):
    _schedule()
    later = "2026-10-26T06:59:00+00:00"
    env["scrape"] = _scrape(31.99, on_sale=True, end=later)
    env["run"]()
    assert env["calls"] == [] and env["writes"] == []
    assert sched.get_action(ITEM)["apply_at"] == later
    assert sched.get_sale_end("4000099948")["end_ts"] == later
    assert "Sale extended" in env["notes"][0]


def test_still_on_sale_without_later_end_retries(env):
    _schedule()
    env["scrape"] = _scrape(31.99, on_sale=True, end=None)
    env["run"]()
    assert env["calls"] == [] and sched.get_action(ITEM)          # kept for next tick
    assert env["notes"] == []


def test_cost_did_not_rise_skips_and_drops(env):
    _schedule()
    env["scrape"] = _scrape(32.49)                               # sale over but cost < new_cost - 0.50
    env["run"]()
    assert env["calls"] == [] and env["writes"] == []
    assert sched.get_action(ITEM) is None
    assert "skipped" in env["notes"][0]


def test_scrape_miss_retries_then_gives_up_after_12h(env):
    _schedule()
    env["scrape"] = {"price": None, "error": "Timed out"}
    env["run"]()
    assert sched.get_action(ITEM)["last_error"].startswith("Costco re-check got no price")
    assert env["notes"] == []
    env["run"](now=END + timedelta(hours=13))
    assert sched.get_action(ITEM) is None and "dropped" in env["notes"][0]
    assert env["calls"] == []


def test_live_price_already_at_target_does_not_revise(env):
    _schedule()
    env["live"] = {**env["live"], "price": 52.00}
    env["run"]()
    assert env["calls"] == []
    assert env["writes"][0][1][COL["ebay_price"]] == 52.00
    assert sched.get_action(ITEM) is None


def test_auth_error_keeps_action_and_alerts_once(env):
    _schedule()
    env["revise"] = {"ok": False, "item_id": ITEM, "price": 50.99, "error_kind": "auth",
                     "error_code": "932", "message": "expired"}
    env["run"]()
    env["run"](now=AFTER + timedelta(minutes=10))
    assert sched.get_action(ITEM)                                # kept for after the token renewal
    assert len([n for n in env["notes"] if "token rejected" in n]) == 1     # not every 10 min
    assert env["writes"] == []
    assert all(r["error_kind"] == "auth" for r, _ in env["logs"])


def test_row_no_longer_active_drops(env):
    _schedule()
    env["rows"] = [_row(status="PAUSED_OOS")]
    env["run"]()
    assert env["calls"] == [] and sched.get_action(ITEM) is None
    assert "dropped" in env["notes"][0]


def test_lock_busy_keeps_action_for_next_tick(env):
    _schedule()
    env["lock"] = False
    env["run"]()
    assert env["browser"] == 0 and sched.get_action(ITEM)


def test_sheet_write_failure_after_ebay_success_is_reported(env, monkeypatch):
    _schedule()

    def boom(*a, **k):
        raise RuntimeError("quota")
    monkeypatch.setattr(sch, "safe_write_row", boom)
    env["run"]()
    assert env["calls"] == [("revise", ITEM, 50.99)]
    assert "Sheet not updated" in env["notes"][0]
    assert sched.get_action(ITEM) is None                        # eBay is done; not retried
    assert "SHEET WRITE FAILED" in env["logs"][0][0]["message"]


# ── end ──────────────────────────────────────────────────────────────────────

def test_due_end_ends_listing_and_marks_row_ended(env):
    _schedule("end")
    env["run"]()
    assert env["calls"] == [("end", ITEM)]
    (row, w), = env["writes"]
    assert w[COL["status"]] == "ENDED" and w[COL["price_change"]] == ""
    assert sched.get_action(ITEM) is None
    assert env["logs"][0][1]["action"] == "end"
    assert "listing ended" in env["notes"][0]


def test_end_is_also_skipped_when_sale_extended(env):
    _schedule("end")
    env["scrape"] = _scrape(31.99, on_sale=True, end="2026-10-26T06:59:00+00:00")
    env["run"]()
    assert env["calls"] == [] and sched.get_action(ITEM)["action"] == "end"


def test_end_api_failure_retries(env):
    _schedule("end")
    env["end"] = {"ok": False, "item_id": ITEM, "price": None, "error_kind": "api",
                  "error_code": "291", "message": "not allowed"}
    env["run"]()
    assert env["writes"] == [] and sched.get_action(ITEM)["last_error"].startswith("eBay api 291")
