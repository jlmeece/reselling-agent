"""--mode health: read-only sheet report. Sheet/Graveyard/Telegram/Chrome all stubbed."""
import os
import sys
from contextlib import contextmanager
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents.scheduler as sch
from agents.scheduler import build_health_report, col_to_idx
from tools import graveyard_writer

COL = {"status": "A", "title": "C", "costco_cost": "G", "ebay_price": "H", "last_checked": "O",
       "ebay_listing_url": "Q", "costco_url": "R", "tier_summary": "T"}
NOW = datetime(2026, 10, 20, 12, 0)


def _row(status, title, cost="10", price="20", checked="2026-10-19 08:00", q="", url="", reason=""):
    row = [""] * (col_to_idx("T") + 1)
    for col, val in (("A", status), ("C", title), ("G", cost), ("H", price), ("O", checked),
                     ("Q", q), ("R", url), ("T", reason)):
        row[col_to_idx(col)] = val
    return row


ROWS = [
    _row("ACTIVE", "Linked Ring", q="123456789012"),
    _row("ACTIVE", "Unlinked Ring"),
    _row("AUDIT_REVIEW", "Patio <Set>", reason="[AUDIT_REVIEW] LIVE listing", checked=""),
    _row("PAUSED_OOS", "Old Kettle", checked="2026-10-01 09:00"),
    _row("PENDING", "New Vitamin", cost="", price=""),
    [],
]
GY = [{"title": "Calcium", "date_removed": "2026-10-09", "reason": "Below floor", "row_num": 2},
      {"title": "City Heights", "date_removed": "2026-10-17", "reason": "Zero velocity", "row_num": 3}]


def _section(lines, header_start):
    i = next(n for n, l in enumerate(lines) if l.startswith(header_start))
    out = []
    for l in lines[i + 1:]:
        if not l:
            break
        out.append(l)
    return lines[i], out


def test_status_counts_and_sections_in_order():
    lines = build_health_report(ROWS, GY, COL, 4, now=NOW)
    assert "5 rows" in lines[0]
    text = "\n".join(lines)
    assert "ACTIVE 2 · AUDIT_REVIEW 1 · PAUSED_OOS 1 · PENDING 1" in text
    order = ["<b>By status", "<b>🟡 AUDIT_REVIEW", "<b>⏸ PAUSED", "<b>🕰 Stale",
             "<b>⚠️ Blank critical", "<b>🪦 Graveyard"]
    idx = [text.index(h) for h in order]
    assert idx == sorted(idx)
    assert "Cost mismatch" not in text          # only with --check-costs


def test_audit_paused_stale_blank_rows():
    lines = build_health_report(ROWS, GY, COL, 4, now=NOW)
    hdr, audit = _section(lines, "<b>🟡 AUDIT_REVIEW")
    assert hdr.endswith("(1)</b>")
    assert audit == ["r6 Patio &lt;Set&gt; · [AUDIT_REVIEW] LIVE listing · never"]   # HTML-escaped
    _, paused = _section(lines, "<b>⏸ PAUSED")
    assert paused == ["r7 Old Kettle · PAUSED_OOS · 2026-10-01 09:00"]
    _, stale = _section(lines, "<b>🕰 Stale")
    assert [s.split(" · ")[0] for s in stale] == ["r6 Patio &lt;Set&gt;", "r7 Old Kettle"]
    _, blank = _section(lines, "<b>⚠️ Blank critical")
    assert blank == ["r5 Unlinked Ring · ACTIVE · blank col Q (unlinked live listing)",
                     "r8 New Vitamin · PENDING · blank cost G, eBay price H"]


def test_graveyard_most_recent_first():
    lines = build_health_report(ROWS, GY, COL, 4, now=NOW)
    hdr, gy = _section(lines, "<b>🪦 Graveyard")
    assert hdr.startswith("<b>🪦 Graveyard (2)</b>")
    assert gy[0] == "City Heights · removed 2026-10-17 · Zero velocity"


def test_cost_mismatch_section_shows_zero():
    lines = build_health_report(ROWS, GY, COL, 4, now=NOW, cost_mismatches=[])
    assert _section(lines, "<b>⚠️ Cost mismatch")[0] == "<b>⚠️ Cost mismatch (0)</b>"


def _env(monkeypatch, sent):
    monkeypatch.setattr(sch, "read_sheet", lambda svc, rng: ROWS)
    monkeypatch.setattr(graveyard_writer, "read_graveyard", lambda svc: GY)
    monkeypatch.setattr(sch, "_send_telegram", lambda tok, chat, text: sent.append(text) or True)
    monkeypatch.setattr(sch, "write_row_partial", lambda *a, **k: (_ for _ in ()).throw(AssertionError("wrote")))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")


def test_dry_run_prints_and_sends_nothing(monkeypatch, capsys):
    sent = []
    _env(monkeypatch, sent)
    result = sch.run_health({}, COL, "svc", "Product Tracker", 4, 500, dry_run=True)
    assert sent == [] and result["status"] == "ok"
    assert "Sheet health" in capsys.readouterr().out


def test_real_run_sends_one_message(monkeypatch):
    sent = []
    _env(monkeypatch, sent)
    sch.run_health({}, COL, "svc", "Product Tracker", 4, 500)
    assert len(sent) == 1 and "Sheet health" in sent[0]


def test_check_costs_lists_mismatch_and_never_writes(monkeypatch):
    sent = []
    _env(monkeypatch, sent)
    rows = [_row("ACTIVE", "Ring A", cost="10.00", url="https://c/a"),
            _row("ACTIVE", "Ring B", cost="20.00", url="https://c/b")]
    monkeypatch.setattr(sch, "read_sheet", lambda svc, rng: rows)

    @contextmanager
    def browser():
        yield "page"
    monkeypatch.setattr(sch, "make_browser", browser)
    monkeypatch.setattr(sch.time, "sleep", lambda s: None)
    prices = {"https://c/a": 10.25, "https://c/b": 24.99}
    monkeypatch.setattr(sch, "scrape_costco", lambda url, page=None: {"price": prices[url]})
    sch.run_health({}, COL, "svc", "Product Tracker", 4, 500, check_costs=True)
    assert "<b>⚠️ Cost mismatch (1)</b>" in sent[0]
    assert "r5 Ring B · sheet $20.00 → live $24.99" in sent[0]


def test_rows_without_title_or_status_are_ignored_and_counted():
    filler = [""] * (col_to_idx("U") + 1)
    filler[col_to_idx("U")] = "0"
    lines = build_health_report(ROWS + [filler, filler], GY, COL, 4, now=NOW)
    assert "5 rows (ignored 2 rows with no title or status)" in lines[0]
    assert "(blank)" not in "\n".join(lines)
