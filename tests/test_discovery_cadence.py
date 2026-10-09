"""Catalog-exhaustion tracker (tools/discovery_cadence.py). Paths go to tmp via conftest."""

import json

from tools import costco_discovery, discovery_cadence as dc

PID_URL = "https://www.costco.com/.product.4000099948.html"
SLUG_URL = "https://www.costco.com/p/-/kirkland-energy-shot/4000099948?langId=-1"


def _p(url, category="Pharmacy"):
    return {"title": "x", "url": url, "price": None, "category": category}


def _url(n):
    return f"https://www.costco.com/.product.{100000 + n}.html"


def _seen():
    with open(dc.SEEN_PATH, encoding="utf-8") as f:
        return json.load(f)


def _cadence():
    with open(dc.CADENCE_PATH, encoding="utf-8") as f:
        return json.load(f)


# ── keys ─────────────────────────────────────────────────────────────────────

def test_both_url_shapes_map_to_same_key():
    assert dc.product_key(PID_URL) == dc.product_key(SLUG_URL) == "4000099948"


def test_unparseable_url_falls_back_to_stripped_url():
    assert dc.product_key("  https://www.costco.com/odd-page.html?x=1#top ") == \
        "https://www.costco.com/odd-page.html"


def test_two_shapes_in_one_run_count_once():
    entry = dc.track_discovery([_p(PID_URL), _p(SLUG_URL)], today="2026-10-01")
    assert entry["last_run_new"] == 1
    assert entry["per_category"]["Pharmacy"]["total"] == 1


# ── first-seen vs repeat ─────────────────────────────────────────────────────

def test_first_seen_then_repeat_is_not_new():
    first = dc.track_discovery([_p(PID_URL)], today="2026-10-01")
    assert first["last_run_new"] == 1 and first["total_seen"] == 1
    second = dc.track_discovery([_p(SLUG_URL)], today="2026-10-02")
    assert second["last_run_new"] == 0 and second["new_today"] == 0
    assert second["total_seen"] == 1
    assert _seen() == {"4000099948": "2026-10-01"}   # first-seen date kept


# ── per-category counting ────────────────────────────────────────────────────

def test_per_category_counts_and_cumulative_seen_total():
    dc.track_discovery([_p(_url(1)), _p(_url(2)), _p(_url(3), "Jewelry")], today="2026-10-01")
    e = dc.track_discovery([_p(_url(1)), _p(_url(4)), _p(_url(3), "Jewelry")],
                           capped={"Jewelry"}, today="2026-10-02")
    assert e["per_category"]["Pharmacy"] == {"total": 2, "new": 1, "seen_total": 3, "capped": False}
    assert e["per_category"]["Jewelry"] == {"total": 1, "new": 0, "seen_total": 1, "capped": True}
    assert e["total_seen"] == 4 and e["new_today"] == 1


def test_same_day_second_run_merges_into_one_row():
    dc.track_discovery([_p(_url(1))], today="2026-10-01")
    e = dc.track_discovery([_p(_url(2)), _p(_url(3), "Jewelry")], today="2026-10-01")
    assert len(_cadence()) == 1
    assert e["new_today"] == 3 and e["last_run_new"] == 2
    assert e["per_category"]["Pharmacy"]["new"] == 2
    assert e["per_category"]["Pharmacy"]["seen_total"] == 2
    assert e["per_category"]["Jewelry"]["seen_total"] == 1


def test_cadence_keeps_last_90_entries():
    for d in range(95):
        dc.track_discovery([_p(_url(d))], today=f"2026-{1 + d // 28:02d}-{1 + d % 28:02d}")
    rows = _cadence()
    assert len(rows) == dc.KEEP_ENTRIES
    assert rows[-1]["per_category"]["Pharmacy"]["seen_total"] == 95   # survives the trim


# ── exhaustion flag ──────────────────────────────────────────────────────────

def _entry(day, new, seen_total=10):
    return {"date": day, "total_seen": seen_total, "new_today": new,
            "per_category": {"Pharmacy": {"total": 10, "new": new, "seen_total": seen_total,
                                          "capped": False}}}


def test_exhausted_after_three_runs_with_no_new_beyond_baseline():
    cadence = [_entry("2026-10-07", 50), _entry("2026-10-08", 0), _entry("2026-10-09", 0)]
    s = dc.category_stats(cadence, today="2026-10-09")["Pharmacy"]
    assert s["runs"] == 3 and s["new_7d"] == 0 and s["exhausted"] is True


def test_not_exhausted_with_only_two_runs():
    cadence = [_entry("2026-10-08", 50), _entry("2026-10-09", 0)]
    assert dc.category_stats(cadence, today="2026-10-09")["Pharmacy"]["exhausted"] is False


def test_not_exhausted_when_new_within_7_days():
    cadence = [_entry("2026-10-01", 50), _entry("2026-10-05", 2), _entry("2026-10-09", 0)]
    s = dc.category_stats(cadence, today="2026-10-09")["Pharmacy"]
    assert s["new_7d"] == 2 and s["exhausted"] is False


def test_exhausted_when_last_new_is_older_than_7_days():
    cadence = [_entry("2026-09-20", 50), _entry("2026-10-02", 4),
               _entry("2026-10-03", 0), _entry("2026-10-09", 0)]
    s = dc.category_stats(cadence, today="2026-10-09")["Pharmacy"]
    assert s["new_7d"] == 0 and s["exhausted"] is True


def test_stats_table_shows_flag_capped_and_caveat():
    cadence = [_entry("2026-10-07", 50), _entry("2026-10-08", 0), _entry("2026-10-09", 0)]
    cadence[-1]["per_category"]["Pharmacy"]["capped"] = True
    out = dc.format_stats_table(dc.category_stats(cadence, today="2026-10-09"))
    assert "YES (capped)" in out and "NOT that Costco has no more products" in out
    assert "No discovery runs tracked yet" in dc.format_stats_table({})


# ── never raises ─────────────────────────────────────────────────────────────

def test_missing_files_do_not_raise():
    assert dc.load_cadence() == []
    assert dc.category_stats(dc.load_cadence()) == {}


def test_corrupt_json_does_not_raise(tmp_path):
    for path in (dc.SEEN_PATH, dc.CADENCE_PATH):
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
    e = dc.track_discovery([_p(PID_URL)], today="2026-10-01")
    assert e["last_run_new"] == 1 and len(_cadence()) == 1


def test_wrong_shape_json_does_not_raise():
    with open(dc.SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(["not", "a", "dict"], f)
    with open(dc.CADENCE_PATH, "w", encoding="utf-8") as f:
        json.dump({"not": "a list"}, f)
    assert dc.track_discovery([_p(PID_URL)], today="2026-10-01")["total_seen"] == 1


def test_unwritable_path_does_not_raise(monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    monkeypatch.setattr(dc, "SEEN_PATH", str(blocker / "seen.json"))
    monkeypatch.setattr(dc, "CADENCE_PATH", str(blocker / "cadence.json"))
    assert dc.track_discovery([_p(PID_URL)], today="2026-10-01")["last_run_new"] == 1


def test_internal_error_returns_none(monkeypatch):
    monkeypatch.setattr(dc, "product_key", lambda url: 1 / 0)
    assert dc.track_discovery([_p(PID_URL)]) is None


# ── discover_all integration ─────────────────────────────────────────────────

def test_discover_all_tracks_and_flags_capped(monkeypatch):
    monkeypatch.setattr(costco_discovery.time, "sleep", lambda s: None)
    monkeypatch.setattr(costco_discovery, "_load_performance", lambda: {})
    monkeypatch.setattr(costco_discovery, "discover_category",
                        lambda page, url, cat: [_p(_url(1), cat), _p(_url(2), cat)]
                        if cat == "Pharmacy" else [_p(_url(3), cat)])
    config = {"Pharmacy": {"discovery_urls": ["u1"], "max_discovery": 1},
              "Jewelry": {"discovery_urls": ["u2"], "max_discovery": 60},
              "Toys": {"discovery_urls": []}}
    products = costco_discovery.discover_all(page=None, categories_config=config)
    assert [p["url"] for p in products] == [_url(1), _url(3)]   # discovery output unchanged
    row = _cadence()[-1]
    assert row["per_category"]["Pharmacy"]["capped"] is True
    assert row["per_category"]["Jewelry"]["capped"] is False
    assert "Toys" not in row["per_category"]
