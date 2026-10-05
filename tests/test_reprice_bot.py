"""Telegram bot one-tap reprice callbacks (reprice:go / reprice:ignore / reprice:offer).
Sheet, eBay and logging are all stubbed — nothing live."""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agents.telegram_bot as tb

COL = {"status": "A", "title": "C", "ebay_price": "H", "price_change": "P", "ebay_listing_url": "Q"}
ITEM_ID = "123456789012"
URL = f"https://www.ebay.com/itm/{ITEM_ID}"


def _row(status="ACTIVE", title="Energy Shot", price="41.48", flag="YES — update listing", url=URL):
    row = [""] * 17
    row[0], row[2], row[7], row[15], row[16] = status, title, price, flag, url
    return row


def _ok_revise(item_id, price):
    return {"ok": True, "item_id": item_id, "price": price, "error_kind": None,
            "error_code": "", "message": ""}


def _live(price=41.48, status="Active", ok=True, kind=None, code=""):
    return {"ok": ok, "item_id": ITEM_ID, "price": price if ok else None, "listing_status": status,
            "listing_type": "FixedPriceItem", "error_kind": kind, "error_code": code,
            "message": "" if ok else "boom"}


@pytest.fixture
def env(monkeypatch):
    st = {"rows": [_row()], "writes": [], "revise": [], "logs": [], "popped": [],
          "live": _live(), "revise_result": None}
    monkeypatch.setattr(tb, "_read_product_rows", lambda: (COL, "svc", "Product Tracker", 4, st["rows"]))
    monkeypatch.setattr(tb, "safe_write_row",
                        lambda svc, sheet, row, pairs: st["writes"].append((row, dict(pairs))))
    monkeypatch.setattr(tb, "fetch_item_price", lambda item_id: st["live"])

    def revise(item_id, price):
        st["revise"].append((item_id, price))
        return st["revise_result"] or _ok_revise(item_id, price)
    monkeypatch.setattr(tb, "revise_fixed_price", revise)
    monkeypatch.setattr(tb, "log_revise", lambda svc, result, **k: st["logs"].append((result, k)))
    monkeypatch.setattr(tb, "log_ignore", lambda item_id, title="": st["logs"].append(("ignore", item_id)))
    monkeypatch.setattr(tb, "pop_pending", lambda item_id: st["popped"].append(item_id))
    monkeypatch.setattr(tb, "get_pending", lambda item_id: None)
    return st


def _tap(arg, ctx=None, action=tb.cb_reprice_go):
    query = SimpleNamespace(edit_message_text=AsyncMock(),
                            message=SimpleNamespace(text="💲 Reprice needed", reply_text=AsyncMock()))
    update = SimpleNamespace(callback_query=query, message=None)
    ctx = ctx or SimpleNamespace(user_data={}, bot_data={"chat_id": 1})
    asyncio.run(action(update, ctx, arg))
    return query


def _final(query):
    args, kwargs = query.edit_message_text.call_args
    return args[0], kwargs.get("reply_markup")


def test_happy_path_revises_writes_h_and_clears_p(env):
    q = _tap(f"{ITEM_ID}:5099")
    assert env["revise"] == [(ITEM_ID, 50.99)]
    assert env["writes"] == [(4, {"H": 50.99, "P": ""})]
    text, kb = _final(q)
    assert "✓ Repriced to $50.99" in text and "was $41.48" in text and kb is None
    assert q.edit_message_text.call_args_list[0].args[0].startswith("⏳")   # progress shown first
    assert env["popped"] == [ITEM_ID]
    (result, kw), = env["logs"]
    assert result["ok"] and kw["old_price"] == 41.48 and kw["row"] == 4


def test_row_resolved_by_item_id_not_row_number(env):
    env["rows"] = [_row(title="Other", url="https://www.ebay.com/itm/999999999999"), [], _row()]
    _tap(f"{ITEM_ID}:5099")
    assert env["writes"][0][0] == 6                 # third data row (start 4)


@pytest.mark.parametrize("code", ["931", "932", "16110"])
def test_auth_failure_is_loud_and_writes_nothing(env, code):
    env["revise_result"] = {"ok": False, "item_id": ITEM_ID, "price": 50.99, "error_kind": "auth",
                            "error_code": code, "message": "token"}
    q = _tap(f"{ITEM_ID}:5099")
    text, kb = _final(q)
    assert "eBay token rejected" in text and code in text and "EBAY_AUTH_TOKEN" in text
    assert kb is None                               # no retry on a dead token
    assert env["writes"] == [] and env["popped"] == []
    assert env["logs"][0][0]["error_kind"] == "auth"


def test_auth_failure_on_live_price_read_never_revises(env):
    env["live"] = _live(ok=False, kind="auth", code="932")
    text, _ = _final(_tap(f"{ITEM_ID}:5099"))
    assert "eBay token rejected" in text
    assert env["revise"] == [] and env["writes"] == []


def test_api_failure_offers_retry_and_writes_nothing(env):
    env["revise_result"] = {"ok": False, "item_id": ITEM_ID, "price": 50.99, "error_kind": "api",
                            "error_code": "21916750", "message": "variation listing"}
    text, kb = _final(_tap(f"{ITEM_ID}:5099"))
    assert "21916750" in text and "still $41.48" in text
    assert [b.callback_data for r in kb.inline_keyboard for b in r] == \
        [f"reprice:go:{ITEM_ID}:5099", f"reprice:ignore:{ITEM_ID}"]
    assert env["writes"] == []


@pytest.mark.parametrize("rows,needle", [
    ([_row(status="ENDED")], "no longer ACTIVE"),
    ([_row(url="")], "not in col Q"),
    ([_row(), _row()], "2 rows"),
])
def test_guards_refuse_without_touching_ebay(env, rows, needle):
    env["rows"] = rows
    text, _ = _final(_tap(f"{ITEM_ID}:5099"))
    assert needle in text
    assert env["revise"] == [] and env["writes"] == []
    assert env["logs"] and env["logs"][0][0]["ok"] is False


def test_never_lowers_when_live_price_already_at_or_above_target(env):
    env["live"] = _live(price=54.99)
    text, _ = _final(_tap(f"{ITEM_ID}:5099"))
    assert env["revise"] == []                      # no eBay write
    assert env["writes"] == [(4, {"H": 54.99, "P": ""})]
    assert "Already $54.99" in text


def test_ended_listing_on_ebay_is_refused(env):
    env["live"] = _live(status="Completed")
    text, _ = _final(_tap(f"{ITEM_ID}:5099"))
    assert "Completed" in text and env["revise"] == []


def test_double_tap_is_ignored(env):
    ctx = SimpleNamespace(user_data={}, bot_data={"chat_id": 1, "reprice_inflight": {ITEM_ID}})
    q = _tap(f"{ITEM_ID}:5099", ctx=ctx)
    q.edit_message_text.assert_not_called()
    assert env["revise"] == []


@pytest.mark.parametrize("arg", [None, "", "123456789012", "abc:5099", f"{ITEM_ID}:x", f"{ITEM_ID}:0"])
def test_malformed_arg_never_revises(env, arg):
    _tap(arg)
    assert env["revise"] == [] and env["writes"] == []


def test_sheet_write_failure_after_ebay_success_is_reported(env, monkeypatch):
    def fail(*a, **k):
        raise RuntimeError("quota")
    monkeypatch.setattr(tb, "safe_write_row", fail)
    text, _ = _final(_tap(f"{ITEM_ID}:5099"))
    assert "eBay is now $50.99" in text and "sheet write failed" in text
    assert "SHEET WRITE FAILED" in env["logs"][0][0]["message"]


def test_ignore_logs_and_leaves_flag(env):
    q = _tap(ITEM_ID, action=tb.cb_reprice_ignore)
    text, _ = _final(q)
    assert "Ignored" in text and "💲 Reprice needed" in text
    assert env["logs"] == [("ignore", ITEM_ID)] and env["writes"] == []


def test_offer_reposts_pending_prompt(env, monkeypatch):
    pending = {"item_id": ITEM_ID, "title": "Energy Shot", "row": 4, "old_cost": 31.99,
               "new_cost": 39.99, "ebay_price": 41.48, "target": 50.99, "fee_rate": 0.1325,
               "ship": 0.0, "ad_rate": 0.0}
    monkeypatch.setattr(tb, "get_pending", lambda item_id: pending)
    q = _tap(ITEM_ID, action=tb.cb_reprice_offer)
    args, kwargs = q.message.reply_text.call_args
    assert "$50.99" in args[0] and kwargs["parse_mode"] == "HTML"
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == f"reprice:go:{ITEM_ID}:5099"


def test_active_card_shows_reprice_button_only_when_pending():
    assert "reprice:offer:1" not in [b.callback_data for r in tb._active_action_kb(7).inline_keyboard for b in r]
    kb = tb._active_action_kb(7, ITEM_ID)
    assert kb.inline_keyboard[0][0].callback_data == f"reprice:offer:{ITEM_ID}"


def test_routes_registered():
    for action in ("go", "ignore", "offer"):
        assert ("reprice", action) in tb._CALLBACK_ROUTES


# ── scheduled reprice / End (pre-stage prompt buttons) ───────────────────────

from datetime import datetime, timedelta, timezone  # noqa: E402

from tools import sale_schedule as sched  # noqa: E402


def _prompt(hours_left=20):
    end = (datetime.now(timezone.utc) + timedelta(hours=hours_left)).replace(microsecond=0)
    return {"item_id": ITEM_ID, "title": "Energy Shot <48>", "sku": "4000099948", "target": 50.99,
            "sale_end_ts": end.isoformat(), "costco_pid": "4000099948", "costco_url": "u",
            "old_cost": 31.99, "new_cost": 39.99, "ebay_price": 41.48}


def test_sched_reprice_stores_action_without_touching_ebay(env):
    prompt = _prompt()
    sched.record_prompt(prompt)
    q = _tap(f"{ITEM_ID}:5099", action=tb.cb_reprice_sched)
    text, kb = _final(q)
    a = sched.get_action(ITEM_ID)
    assert a["action"] == "reprice" and a["target_price"] == 50.99
    assert a["apply_at"] == prompt["sale_end_ts"]                # applies exactly at the sale end
    assert env["revise"] == [] and env["writes"] == []           # nothing on eBay / sheet now
    assert "Scheduled" in text and "$50.99" in text and "&lt;48&gt;" in text
    assert kb.inline_keyboard[0][0].callback_data == f"reprice:cancel:{ITEM_ID}"


def test_sched_end_stores_end_action(env):
    sched.record_prompt(_prompt())
    text, _ = _final(_tap(ITEM_ID, action=tb.cb_reprice_schedend))
    assert sched.get_action(ITEM_ID)["action"] == "end"
    assert "HIDE the listing (quantity 0)" in text


def test_sched_refuses_without_prompt_or_after_end(env):
    text, _ = _final(_tap(f"{ITEM_ID}:5099", action=tb.cb_reprice_sched))
    assert "expired" in text and sched.get_action(ITEM_ID) is None
    sched.record_prompt(_prompt(hours_left=-1))
    text, _ = _final(_tap(f"{ITEM_ID}:5099", action=tb.cb_reprice_sched))
    assert "already ended" in text and sched.get_action(ITEM_ID) is None


@pytest.mark.parametrize("rows,needle", [
    ([_row(status="ENDED")], "no longer ACTIVE"),
    ([_row(url="")], "exactly one row"),
])
def test_sched_row_guards(env, rows, needle):
    sched.record_prompt(_prompt())
    env["rows"] = rows
    text, _ = _final(_tap(ITEM_ID, action=tb.cb_reprice_schedend))
    assert needle in text and sched.get_action(ITEM_ID) is None


def test_sched_stale_price_button_refused(env):
    sched.record_prompt(_prompt())
    text, _ = _final(_tap(f"{ITEM_ID}:4999", action=tb.cb_reprice_sched))
    assert "out of date" in text and sched.get_action(ITEM_ID) is None


def test_cancel_removes_action(env):
    sched.record_prompt(_prompt())
    sched.schedule_action(ITEM_ID, "end", _prompt())
    text, _ = _final(_tap(ITEM_ID, action=tb.cb_reprice_cancel))
    assert "Cancelled the scheduled end" in text and sched.get_action(ITEM_ID) is None
    text, _ = _final(_tap(ITEM_ID, action=tb.cb_reprice_cancel))
    assert "Nothing scheduled" in text


def test_active_card_cancel_button_when_scheduled():
    kb = tb._active_action_kb(7, None, cancel_item_id=ITEM_ID)
    assert kb.inline_keyboard[0][0].callback_data == f"reprice:cancel:{ITEM_ID}"


def test_schedule_routes_registered():
    for action in ("sched", "schedend", "cancel"):
        assert ("reprice", action) in tb._CALLBACK_ROUTES
