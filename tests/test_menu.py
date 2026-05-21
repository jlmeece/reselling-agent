import sys, json
sys.path.insert(0, ".")

import yaml
import pytest

# ── load_categories ───────────────────────────────────────────────────────────

def test_load_categories_returns_category_names(tmp_path):
    from agents.menu import load_categories
    cfg = tmp_path / "categories.yaml"
    cfg.write_text(yaml.dump({
        "business": {"sheet_name": "Tracker"},
        "categories": {"Jewelry": {}, "Watches": {}, "Pharmacy": {}},
    }, sort_keys=False))
    result = load_categories(path=str(cfg))
    assert sorted(result) == ["Jewelry", "Pharmacy", "Watches"]


def test_load_categories_missing_file_returns_empty():
    from agents.menu import load_categories
    result = load_categories(path="/nonexistent/path.yaml")
    assert result == []


def test_load_categories_no_categories_key(tmp_path):
    from agents.menu import load_categories
    cfg = tmp_path / "categories.yaml"
    cfg.write_text(yaml.dump({"business": {}}))
    result = load_categories(path=str(cfg))
    assert result == []


# ── load_last_run ─────────────────────────────────────────────────────────────

def test_load_last_run_returns_most_recent_for_mode(tmp_path):
    from agents.menu import load_last_run
    hist = tmp_path / "run_history.json"
    hist.write_text(json.dumps({"runs": [
        {"date": "2026-05-18", "time": "07:00", "mode": "discovery", "status": "ok"},
        {"date": "2026-05-19", "time": "10:02", "mode": "research",  "status": "ok"},
        {"date": "2026-05-20", "time": "07:01", "mode": "discovery", "status": "ok"},
    ]}))
    assert load_last_run("discovery", path=str(hist)) == "2026-05-20 07:01"
    assert load_last_run("research",  path=str(hist)) == "2026-05-19 10:02"


def test_load_last_run_missing_mode_returns_empty(tmp_path):
    from agents.menu import load_last_run
    hist = tmp_path / "run_history.json"
    hist.write_text(json.dumps({"runs": [
        {"date": "2026-05-20", "time": "07:01", "mode": "discovery", "status": "ok"},
    ]}))
    assert load_last_run("rotation", path=str(hist)) == ""


def test_load_last_run_missing_file_returns_empty():
    from agents.menu import load_last_run
    assert load_last_run("active", path="/nonexistent/path.json") == ""
