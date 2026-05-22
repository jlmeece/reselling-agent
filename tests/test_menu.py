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


def test_researcher_add_limit_rejects_zero_and_negative():
    """--add-limit 0 and --add-limit -1 should exit non-zero."""
    import subprocess, sys
    for bad_val in ["0", "-1"]:
        result = subprocess.run(
            [sys.executable, "agents/researcher.py", "--add-limit", bad_val, "--help"],
            capture_output=True, text=True
        )
        assert result.returncode != 0, f"--add-limit {bad_val} should be rejected"


# ── scheduler run_discovery add_limit ────────────────────────────────────────

def test_run_discovery_passes_add_limit_to_subprocess(monkeypatch):
    """run_discovery includes --add-limit N in the subprocess command when set."""
    import subprocess as sp
    captured = []

    class FakeResult:
        returncode = 0

    monkeypatch.setattr(sp, "run", lambda cmd, **kw: captured.append(cmd) or FakeResult())

    from agents.scheduler import run_discovery
    run_discovery({}, {}, None, "Sheet", 4, 1000, category="Jewelry", add_limit=10)

    assert len(captured) == 1
    cmd = captured[0]
    assert "--add-limit" in cmd
    assert "10" in cmd


def test_run_discovery_omits_add_limit_when_none(monkeypatch):
    """run_discovery does not include --add-limit when add_limit is None."""
    import subprocess as sp
    captured = []

    class FakeResult:
        returncode = 0

    monkeypatch.setattr(sp, "run", lambda cmd, **kw: captured.append(cmd) or FakeResult())

    from agents.scheduler import run_discovery
    run_discovery({}, {}, None, "Sheet", 4, 1000)

    assert "--add-limit" not in captured[0]


def test_scheduler_argparse_accepts_add_limit():
    """scheduler.py --help exits 0 and lists --add-limit."""
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "agents/scheduler.py", "--help"],
        capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "--add-limit" in result.stdout


# ── prompt_limit ─────────────────────────────────────────────────────────────

def test_prompt_limit_returns_int_on_valid_input(monkeypatch):
    from agents.menu import prompt_limit
    monkeypatch.setattr("builtins.input", lambda _: "10")
    assert prompt_limit() == 10


def test_prompt_limit_returns_none_on_blank(monkeypatch):
    from agents.menu import prompt_limit
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert prompt_limit() is None


def test_prompt_limit_retries_on_invalid_then_accepts(monkeypatch):
    from agents.menu import prompt_limit
    responses = iter(["abc", "0", "-3", "5"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    assert prompt_limit() == 5


# ── limit_prompt field on discovery items ────────────────────────────────────

def test_discovery_items_have_limit_prompt(monkeypatch):
    from agents.menu import get_all_items
    discovery_items = [i for i in get_all_items() if i.get("mode") == "discovery"]
    assert len(discovery_items) == 2, "Expected exactly 2 discovery items"
    for item in discovery_items:
        assert item.get("limit_prompt") is True, f"Item '{item['label']}' missing limit_prompt"


def test_non_discovery_items_have_no_limit_prompt():
    from agents.menu import get_all_items
    non_discovery = [i for i in get_all_items() if i.get("mode") != "discovery"]
    for item in non_discovery:
        assert not item.get("limit_prompt"), f"Item '{item['label']}' should not have limit_prompt"


# ── run_item passes add_limit ─────────────────────────────────────────────────

def test_run_item_appends_add_limit_to_command(monkeypatch):
    import subprocess
    captured = []

    class FakeResult:
        returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: captured.append(cmd) or FakeResult())
    monkeypatch.setattr("builtins.input", lambda _: "")  # dismiss "Press Enter" prompt

    from agents.menu import run_item
    item = {"mode": "discovery", "args": [], "label": "Discover"}
    run_item(item, add_limit=7)

    assert "--add-limit" in captured[0]
    assert "7" in captured[0]


def test_run_item_omits_add_limit_when_none(monkeypatch):
    import subprocess
    captured = []

    class FakeResult:
        returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: captured.append(cmd) or FakeResult())
    monkeypatch.setattr("builtins.input", lambda _: "")

    from agents.menu import run_item
    item = {"mode": "discovery", "args": [], "label": "Discover"}
    run_item(item, add_limit=None)

    assert "--add-limit" not in captured[0]
