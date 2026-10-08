"""tools/reprice.py — restore-margin price math, prompt/keyboard, pending store, audit log."""
import json
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import reprice
from tools.ebay_sync import compute_net
from tools.reprice import (
    format_reprice_prompt, reprice_keyboard, restore_margin_price, round_up_99,
)


# ── rounding ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (47.20, 47.99), (47.99, 47.99), (48.00, 48.99), (47.991, 48.99), (0.5, 0.99), (50.7019, 50.99),
])
def test_round_up_99(raw, expected):
    assert round_up_99(raw) == expected
    assert round_up_99(raw) >= round(raw, 2)


# ── restore_margin_price ─────────────────────────────────────────────────────

def test_energy_shot_sale_end_restores_previous_net():
    # Hand calc: prev net = 41.48 - 31.99 - (41.48*0.1325*1.08 + 0.30) - 31.99*0.0825 = 0.615
    #            raw = (0.615 + 39.99*1.0825 + 0.30) / (1 - 0.1325*1.08) = 51.586 -> 51.99
    target = restore_margin_price(31.99, 39.99, 41.48, 0.1325, 0.0)
    assert target == 51.99
    prev = compute_net(41.48, 31.99, 0.1325)
    assert compute_net(target, 39.99, 0.1325) >= prev                  # margin restored
    assert compute_net(target - 1.00, 39.99, 0.1325) < prev            # and not overshot by $1+


def test_exact_raw_price_restores_net_to_the_cent():
    old, new, h, fee, ship = 20.00, 25.00, 40.00, 0.13, 5.00
    prev = compute_net(h, old, fee, ship)
    raw = (prev + new * 1.0825 + 0.30 + ship) / (1 - fee * 1.08)
    assert compute_net(raw, new, fee, ship) == pytest.approx(prev)
    assert restore_margin_price(old, new, h, fee, ship) == round_up_99(raw)


def test_ad_rate_scales_with_price():
    old, new, h, fee, ad = 30.0, 36.0, 60.0, 0.13, 0.05
    target = restore_margin_price(old, new, h, fee, 0.0, ad)
    prev = compute_net(h, old, fee, 0.0, h * ad)
    assert compute_net(target, new, fee, 0.0, target * ad) >= prev
    assert target > restore_margin_price(old, new, h, fee, 0.0, 0.0)  # ads cost more


@pytest.mark.parametrize("old,new", [(39.99, 31.99), (39.99, 39.99)])
def test_never_reprices_on_a_cost_drop_or_flat(old, new):
    # Sale START: Jay keeps the eBay price and pockets the margin.
    assert restore_margin_price(old, new, 41.48, 0.1325) is None


@pytest.mark.parametrize("old,new,h,fee,ship", [
    (10.0, 10.01, 100.0, 0.10, 0.0), (31.99, 39.99, 41.48, 0.1325, 0.0), (45.0, 50.0, 40.0, 0.10, 3.0),
    (100.0, 100.6, 101.0, 0.04, 0.0), (5.0, 9.0, 4.0, 0.15, 6.0),
])
def test_target_is_always_a_raise(old, new, h, fee, ship):
    target = restore_margin_price(old, new, h, fee, ship)
    assert target is not None and target > h


def test_never_lowers_when_target_not_above_current(monkeypatch):
    # Guard path: if rounding/inputs ever produce a target <= H, nothing is suggested.
    monkeypatch.setattr(reprice, "round_up_99", lambda p: 41.48)
    assert restore_margin_price(31.99, 39.99, 41.48, 0.1325) is None


def test_underwater_listing_is_restored_to_break_even_not_to_its_loss():
    # prev net is negative (losing money). Restore floors at $0 net.
    target = restore_margin_price(45.0, 50.0, 40.0, 0.10, 0.0)
    assert target == round_up_99((50.0 * 1.0825 + 0.30) / (1 - 0.10 * 1.08))   # 61.01 -> 61.99
    assert compute_net(target, 50.0, 0.10) >= 0


@pytest.mark.parametrize("args", [
    (None, 39.99, 41.48, 0.1325), (31.99, None, 41.48, 0.1325), (31.99, 39.99, None, 0.1325),
    (31.99, 39.99, 41.48, None), (31.99, 39.99, 0, 0.1325), (31.99, 39.99, 41.48, 1.0),
    ("abc", 39.99, 41.48, 0.1325),
])
def test_unknown_or_impossible_inputs_give_none(args):
    assert restore_margin_price(*args) is None


# ── prompt / keyboard ────────────────────────────────────────────────────────

ITEM = {"item_id": "123456789012", "title": "Energy Shot <48 ct> & more", "row": 9, "sku": "1711796",
        "old_cost": 31.99, "new_cost": 39.99, "ebay_price": 41.48, "target": 51.99,
        "fee_rate": 0.1325, "ship": 0.0, "ad_rate": 0.0}


def test_keyboard_callback_data_and_limit():
    kb = reprice_keyboard("123456789012", 50.99)
    (go, ignore), = kb["inline_keyboard"]
    assert go["callback_data"] == "reprice:go:123456789012:5099"
    assert ignore["callback_data"] == "reprice:ignore:123456789012"
    assert "$50.99" in go["text"]
    longest = reprice_keyboard("12345678901234", 99999.99)["inline_keyboard"][0][0]["callback_data"]
    assert len(longest.encode()) <= 64


def test_prompt_shows_numbers_and_escapes_title():
    text = format_reprice_prompt(ITEM)
    assert "&lt;48 ct&gt; &amp; more" in text and "<48 ct>" not in text
    assert "$31.99 → $39.99" in text
    assert "$41.48" in text and "<b>$51.99</b>" in text
    assert "net was $0.62" in text                    # previous net
    assert "net $0.96" in text                        # net at target
    assert "#1711796" in text and "row 9" in text
    assert "⚠️" not in text                           # 23% raise — no big-jump warning


def test_prompt_warns_on_big_jump():
    assert "⚠️" in format_reprice_prompt({**ITEM, "target": 70.99})


def test_send_prompt_uses_the_bot_token_and_buttons(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "BOT123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    sent = []
    monkeypatch.setattr(reprice.urllib.request, "urlopen", lambda req, timeout=None: sent.append(req))
    assert reprice.send_reprice_prompt(ITEM) is True
    (req,) = sent
    assert req.full_url == "https://api.telegram.org/botBOT123/sendMessage"   # same token the bot polls
    body = json.loads(req.data)
    assert body["chat_id"] == "42" and body["parse_mode"] == "HTML"
    assert body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "reprice:go:123456789012:5099"


def test_send_prompt_never_raises(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")

    def boom(*a, **k):
        raise OSError("down")
    monkeypatch.setattr(reprice.urllib.request, "urlopen", boom)
    assert reprice.send_reprice_prompt(ITEM) is False


# ── pending store ────────────────────────────────────────────────────────────

def test_pending_save_get_pop_and_prune(tmp_path):
    path = str(tmp_path / "p.json")
    reprice.save_pending({**ITEM, "item_id": "111111111111"}, path=path, now=datetime(2026, 8, 1))
    reprice.save_pending(ITEM, path=path, now=datetime(2026, 10, 4))
    assert reprice.get_pending("111111111111", path=path) is None           # >30 days, pruned
    assert reprice.get_pending("123456789012", path=path)["target"] == 50.99
    reprice.pop_pending("123456789012", path=path)
    assert reprice.get_pending("123456789012", path=path) is None


# ── audit log ────────────────────────────────────────────────────────────────

def test_log_revise_writes_file_and_run_log(monkeypatch, tmp_path):
    runs = []
    import tools.run_logger as rl
    monkeypatch.setattr(rl, "log_run_end", lambda mode, start, results, service=None, dedup=True:
                        runs.append((mode, results, dedup)))
    log = tmp_path / "ebay_sync.log"
    ok = {"ok": True, "item_id": "123456789012", "price": 50.99, "message": ""}
    reprice.log_revise("svc", ok, title="Energy Shot", row=9, old_price=41.48, log_path=str(log))
    bad = {"ok": False, "item_id": "123456789012", "price": 50.99, "error_kind": "auth",
           "error_code": "932", "message": "expired"}
    reprice.log_revise("svc", bad, title="Energy Shot", row=9, old_price=41.48, log_path=str(log))

    lines = log.read_text(encoding="utf-8").splitlines()
    assert "OK" in lines[0] and "$41.48→$50.99" in lines[0]
    assert "ERROR" in lines[1] and "932: expired" in lines[1]
    assert [r[0] for r in runs] == ["reprice", "reprice"]
    assert runs[0][1]["status"] == "ok" and runs[1][1]["status"] == "error"
    assert "932" in runs[1][1]["errors"]
    assert all(r[2] is False for r in runs)              # every tap gets its own Run Log row
