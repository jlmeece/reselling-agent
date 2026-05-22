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
    assert result == ["Jewelry", "Watches", "Pharmacy"]


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


# ── get_all_items ─────────────────────────────────────────────────────────────

def test_get_all_items_is_flattening_of_groups():
    from agents.menu import get_all_items, MENU_GROUPS
    items = get_all_items()
    expected_count = sum(len(group["items"]) for group in MENU_GROUPS)
    assert len(items) == expected_count


def test_get_all_items_each_has_required_keys():
    from agents.menu import get_all_items
    for item in get_all_items():
        assert "label" in item
        assert "category_prompt" in item
        assert "args" in item
        # must have either mode or script
        assert item.get("mode") is not None or item.get("script") is not None


def test_get_all_items_category_prompt_items_have_mode():
    from agents.menu import get_all_items
    for item in get_all_items():
        if item["category_prompt"]:
            assert item["mode"] is not None, f"Item '{item['label']}' has category_prompt but no mode"


def test_get_all_items_category_prompt_modes_are_discovery_or_research():
    from agents.menu import get_all_items
    for item in get_all_items():
        if item["category_prompt"]:
            assert item["mode"] in ["discovery", "research"]


# ── researcher --add-limit ────────────────────────────────────────────────────

def test_researcher_argparse_accepts_add_limit():
    """--add-limit is a known arg; --help exits 0 and mentions it."""
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "agents/researcher.py", "--help"],
        capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "--add-limit" in result.stdout


def test_researcher_run_researcher_accepts_add_limit_kwarg():
    """run_researcher signature must accept add_limit without TypeError."""
    import inspect
    from agents.researcher import run_researcher
    sig = inspect.signature(run_researcher)
    assert "add_limit" in sig.parameters
    assert sig.parameters["add_limit"].default is None
