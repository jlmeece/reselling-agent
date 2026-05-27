# Startup Menu Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `.\run.ps1 menu` to launch a grouped interactive menu that lets you pick any agent mode, with category sub-prompts and last-run timestamps.

**Architecture:** New `agents/menu.py` contains all menu logic as pure functions (testable) plus a `main()` interactive loop. `run.ps1` gets one new `menu` case that delegates to it. No changes to `scheduler.py` or any tool.

**Tech Stack:** Python 3, `yaml`, `json`, `subprocess`, `colorama` (optional), `pytest`

---

## File Map

| Action | Path |
|--------|------|
| Create | `agents/menu.py` |
| Create | `tests/test_menu.py` |
| Modify | `run.ps1` |

---

## Task 1: Tests for `load_categories()` and `load_last_run()`

**Files:**
- Create: `tests/test_menu.py`

- [ ] **Step 1: Create `tests/test_menu.py` with failing tests**

```python
import sys, os, json, tempfile
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
    }))
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
```

- [ ] **Step 2: Run tests to confirm they all fail (module doesn't exist yet)**

```
pytest tests/test_menu.py -v
```

Expected: `ModuleNotFoundError` or `ImportError` — that's correct, we haven't written `menu.py` yet.

---

## Task 2: Implement `agents/menu.py` helper functions

**Files:**
- Create: `agents/menu.py`

- [ ] **Step 1: Create `agents/menu.py` with helper functions only (no interactive loop yet)**

```python
import json
import os
import subprocess
import sys

import yaml

try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init()
    HAS_COLOR = True
except ImportError:
    HAS_COLOR = False

    class Fore:
        CYAN = YELLOW = GREEN = WHITE = RED = ""

    class Style:
        RESET_ALL = BRIGHT = ""


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATEGORIES_PATH  = os.path.join(PROJECT_ROOT, "config", "categories.yaml")
RUN_HISTORY_PATH = os.path.join(PROJECT_ROOT, "data", "run_history.json")
SCHEDULER        = os.path.join(PROJECT_ROOT, "agents", "scheduler.py")
SETUP_COOKIES    = os.path.join(PROJECT_ROOT, "tools", "setup_costco_session.py")
SETUP_SHEET      = os.path.join(PROJECT_ROOT, "agents", "setup_sheet.py")


MENU_GROUPS = [
    {
        "label": "RESEARCH",
        "items": [
            {"label": "Discover new products — all categories",   "mode": "discovery", "category_prompt": False, "args": []},
            {"label": "Discover new products — pick category",    "mode": "discovery", "category_prompt": True,  "args": []},
            {"label": "Score PENDING rows — all categories",      "mode": "research",  "category_prompt": False, "args": []},
            {"label": "Score PENDING rows — pick category",       "mode": "research",  "category_prompt": True,  "args": []},
        ],
    },
    {
        "label": "PRICE & STOCK",
        "items": [
            {"label": "Recheck failed / missing prices",          "mode": "recheck",   "category_prompt": False, "args": []},
            {"label": "Force full refresh (all rows)",            "mode": "recheck",   "category_prompt": False, "args": ["--force"]},
            {"label": "Active listings monitor",                  "mode": "active",    "category_prompt": False, "args": []},
        ],
    },
    {
        "label": "MAINTENANCE",
        "items": [
            {"label": "Daily sweep (APPROVED→READY, PAUSED_OOS)", "mode": "daily",    "category_prompt": False, "args": []},
            {"label": "Rotation digest (weekly)",                  "mode": "rotation", "category_prompt": False, "args": []},
            {"label": "Refresh Costco session cookies",            "mode": None,       "category_prompt": False, "args": [], "script": SETUP_COOKIES},
            {"label": "Sheet formatter / setup",                   "mode": None,       "category_prompt": False, "args": [], "script": SETUP_SHEET},
        ],
    },
]


def load_categories(path=None):
    """Return list of category name strings from categories.yaml."""
    path = path or CATEGORIES_PATH
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        return list(data.get("categories", {}).keys())
    except Exception:
        return []


def load_last_run(mode, path=None):
    """Return 'YYYY-MM-DD HH:MM' of most recent run for mode, or ''."""
    path = path or RUN_HISTORY_PATH
    try:
        with open(path) as f:
            data = json.load(f)
        runs = [r for r in data.get("runs", []) if r.get("mode") == mode]
        if not runs:
            return ""
        last = runs[-1]
        return f"{last['date']} {last['time']}"
    except Exception:
        return ""


def get_all_items():
    """Flat ordered list of all menu items across all groups."""
    items = []
    for group in MENU_GROUPS:
        items.extend(group["items"])
    return items
```

- [ ] **Step 2: Run the tests — all should pass**

```
pytest tests/test_menu.py -v
```

Expected output:
```
tests/test_menu.py::test_load_categories_returns_category_names PASSED
tests/test_menu.py::test_load_categories_missing_file_returns_empty PASSED
tests/test_menu.py::test_load_categories_no_categories_key PASSED
tests/test_menu.py::test_load_last_run_returns_most_recent_for_mode PASSED
tests/test_menu.py::test_load_last_run_missing_mode_returns_empty PASSED
tests/test_menu.py::test_load_last_run_missing_file_returns_empty PASSED
```

- [ ] **Step 3: Commit**

```
git add agents/menu.py tests/test_menu.py
git commit -m "feat: add menu helper functions with tests (load_categories, load_last_run)"
```

---

## Task 3: Tests for `get_all_items()` structure

**Files:**
- Modify: `tests/test_menu.py`

- [ ] **Step 1: Append these tests to `tests/test_menu.py`**

```python
# ── get_all_items ─────────────────────────────────────────────────────────────

def test_get_all_items_returns_11_items():
    from agents.menu import get_all_items
    items = get_all_items()
    assert len(items) == 11


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


def test_get_all_items_exactly_two_category_prompt_items():
    from agents.menu import get_all_items
    prompts = [i for i in get_all_items() if i["category_prompt"]]
    assert len(prompts) == 2
    assert prompts[0]["mode"] == "discovery"
    assert prompts[1]["mode"] == "research"
```

- [ ] **Step 2: Run tests — all should pass**

```
pytest tests/test_menu.py -v
```

Expected: all 10 tests PASS.

- [ ] **Step 3: Commit**

```
git add tests/test_menu.py
git commit -m "test: add get_all_items structure tests"
```

---

## Task 4: Add interactive UI functions to `agents/menu.py`

**Files:**
- Modify: `agents/menu.py`

- [ ] **Step 1: Append `render_menu()`, `prompt_category()`, `run_item()`, and `main()` to `agents/menu.py`**

Add these functions after `get_all_items()`:

```python
def render_menu(last_runs):
    """Print the grouped menu. last_runs maps mode -> timestamp string."""
    print()
    print(f"{Style.BRIGHT}{Fore.WHITE}WAT Reselling Agent{Style.RESET_ALL}")
    print("=" * 40)
    num = 1
    for i, group in enumerate(MENU_GROUPS, 1):
        print()
        print(f"{Fore.YELLOW}[{i}] {group['label']}{Style.RESET_ALL}")
        for item in group["items"]:
            ts = last_runs.get(item.get("mode") or "", "")
            ts_str = f"  {Fore.CYAN}[last: {ts}]{Style.RESET_ALL}" if ts else ""
            print(f"  {num:2}. {item['label']}{ts_str}")
            num += 1
    print()
    print(f"   0. Exit")
    print()


def prompt_category(categories):
    """Show numbered category list. Returns category name or None (go back)."""
    if not categories:
        val = input("Enter category name (or blank to go back): ").strip()
        return val or None
    print()
    print(f"{Fore.YELLOW}Categories:{Style.RESET_ALL}")
    for i, cat in enumerate(categories, 1):
        print(f"  {i}. {cat}")
    print()
    while True:
        raw = input("Pick a category (or 0 to go back): ").strip()
        if raw == "0":
            return None
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(categories):
                return categories[idx]
        except ValueError:
            pass
        print("Invalid choice — try again.")


def run_item(item, category=None):
    """Invoke the subprocess for the selected menu item."""
    if item.get("script"):
        cmd = [sys.executable, item["script"]]
    else:
        cmd = [sys.executable, SCHEDULER, "--mode", item["mode"]] + item.get("args", [])
        if category:
            cmd += ["--category", category]
    print()
    print(f"{Fore.CYAN}Running: {' '.join(cmd)}{Style.RESET_ALL}")
    print()
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"{Fore.RED}Command exited with code {result.returncode}{Style.RESET_ALL}")
    input("\nPress Enter to return to menu...")


def main():
    categories = load_categories()
    all_items  = get_all_items()
    while True:
        modes     = {item["mode"] for item in all_items if item.get("mode")}
        last_runs = {mode: load_last_run(mode) for mode in modes}
        render_menu(last_runs)
        raw = input("Pick a number: ").strip()
        if raw == "0":
            print("Exiting.")
            break
        try:
            idx = int(raw) - 1
            if not (0 <= idx < len(all_items)):
                raise ValueError
        except ValueError:
            print(f"{Fore.RED}Invalid choice — try again.{Style.RESET_ALL}")
            continue
        item     = all_items[idx]
        category = None
        if item["category_prompt"]:
            category = prompt_category(categories)
            if category is None:
                continue
        run_item(item, category=category)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Re-run tests to confirm nothing broke**

```
pytest tests/test_menu.py -v
```

Expected: all 10 tests still PASS.

- [ ] **Step 3: Commit**

```
git add agents/menu.py
git commit -m "feat: add menu interactive loop (render_menu, prompt_category, run_item, main)"
```

---

## Task 5: Wire `menu` command into `run.ps1`

**Files:**
- Modify: `run.ps1`

- [ ] **Step 1: Open `run.ps1` and add the `menu` case inside the `switch` block, before the `default` case**

Find this line in `run.ps1`:
```powershell
    # ── Help ──────────────────────────────────────────────────────────────────
    default {
```

Insert the following block immediately before it:

```powershell
    # ── Interactive menu ──────────────────────────────────────────────────────
    "menu" {
        & $py agents\menu.py
    }

```

- [ ] **Step 2: Smoke-test the help output still works**

```
.\run.ps1 help
```

Expected: the help text prints normally (no change to default case).

- [ ] **Step 3: Smoke-test the menu launches**

```
.\run.ps1 menu
```

Expected: the grouped menu appears with timestamps and `Pick a number:` prompt. Type `0` to exit.

- [ ] **Step 4: Commit**

```
git add run.ps1
git commit -m "feat: add 'menu' command to run.ps1 — launches interactive agent menu"
```

---

## Task 6: Install `colorama` if not present

**Files:**
- No file changes — dependency check only

- [ ] **Step 1: Check if colorama is in requirements**

```
findstr colorama requirements.txt
```

If no output, run:

```
pip install colorama
```

Then add it to `requirements.txt`:

```
colorama>=0.4.6
```

```
git add requirements.txt
git commit -m "chore: add colorama to requirements"
```

If `colorama` was already present, skip this task entirely.

---

## Task 7: End-to-end manual verification

- [ ] **Step 1: Run the full test suite**

```
pytest tests/test_menu.py -v
```

Expected: all 10 tests PASS.

- [ ] **Step 2: Launch the menu and verify each group renders**

```
.\run.ps1 menu
```

Confirm:
- Three groups: RESEARCH, PRICE & STOCK, MAINTENANCE
- 11 numbered options total
- `[last: ...]` timestamps appear next to any modes that have run history
- `0` exits cleanly

- [ ] **Step 3: Test category sub-prompt flow**

From the menu, pick option `2` (Discover — pick category).
Confirm:
- Category list appears with all categories from `config/categories.yaml`
- Picking `0` returns to the main menu without running anything
- Picking a valid number shows the `Running: ...` line and invokes `scheduler.py --mode discovery --category <name>`

- [ ] **Step 4: Test invalid input handling**

At `Pick a number:`, enter `99`, then `abc`. Confirm both print `Invalid choice — try again.` and re-show the prompt without crashing.
