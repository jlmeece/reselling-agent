# Discovery Add-Limit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "how many new items to add?" prompt to the startup menu's discovery options, capping the number of new PENDING rows written to the sheet per run.

**Architecture:** New `--add-limit N` flag flows from `menu.py` (prompt) → `scheduler.py` (forwarded as CLI arg) → `researcher.py` (slices `new_products[:N]` before the batch sheet write). Existing `--limit` (which caps scoring) is untouched.

**Tech Stack:** Python 3, argparse, pytest, existing `agents/` module pattern.

---

### Task 1: Add `--add-limit` to researcher.py

**Files:**
- Modify: `agents/researcher.py`
- Test: `tests/test_menu.py` (add to existing test file — researcher arg tests are small enough to live here for now)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_menu.py`:

```python
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
```

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/test_menu.py::test_researcher_argparse_accepts_add_limit tests/test_menu.py::test_researcher_run_researcher_accepts_add_limit_kwarg -v
```

Expected: FAIL — `--add-limit` not in help output, `add_limit` not in signature.

- [ ] **Step 3: Add `--add-limit` arg to argparse in researcher.py**

In `agents/researcher.py`, find the argparse block (line ~1140). Add after the `--limit` argument:

```python
    parser.add_argument("--add-limit", type=int, default=None,
                        help="Max new products to add to sheet during discovery")
```

Then update the `run_researcher(...)` call on the next line:

```python
    run_researcher(limit=args.limit, add_limit=args.add_limit,
                   category_filter=args.category,
                   discover_only=args.discover_only,
                   skip_discovery=args.skip_discovery)
```

- [ ] **Step 4: Add `add_limit` param to `run_researcher` signature**

In `agents/researcher.py`, find `def run_researcher(...)` (line ~410). Change:

```python
def run_researcher(limit=None, category_filter=None, discover_only=False, skip_discovery=False):
```

to:

```python
def run_researcher(limit=None, add_limit=None, category_filter=None, discover_only=False, skip_discovery=False):
```

- [ ] **Step 5: Add the slice before `_add_new_products_batch`**

In `run_researcher`, find the block starting with `if new_products:` (line ~446). Add the cap just before the batch call:

```python
        new_products = [p for p in discovered if p["url"] not in existing_urls]
        logger.info(f"  {len(discovered)} found, {len(new_products)} are new.")

        if add_limit and len(new_products) > add_limit:
            logger.info(f"  Capping at {add_limit} new products (found {len(new_products)}).")
            new_products = new_products[:add_limit]

        # Add new products to sheet as PENDING (single batch API call)
        if new_products:
            _add_new_products_batch(service, sheet_name, new_products, COL)
```

- [ ] **Step 6: Run tests to verify they pass**

```
pytest tests/test_menu.py::test_researcher_argparse_accepts_add_limit tests/test_menu.py::test_researcher_run_researcher_accepts_add_limit_kwarg -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```
git add agents/researcher.py tests/test_menu.py
git commit -m "feat: add --add-limit to researcher.py to cap new products added per discovery run"
```

---

### Task 2: Wire `add_limit` through scheduler.py

**Files:**
- Modify: `agents/scheduler.py`
- Test: `tests/test_menu.py` (continue adding)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_menu.py`:

```python
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
```

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/test_menu.py::test_run_discovery_passes_add_limit_to_subprocess tests/test_menu.py::test_run_discovery_omits_add_limit_when_none tests/test_menu.py::test_scheduler_argparse_accepts_add_limit -v
```

Expected: FAIL.

- [ ] **Step 3: Update `run_discovery` signature and subprocess build**

In `agents/scheduler.py`, find `def run_discovery(...)` (line ~504). Change:

```python
def run_discovery(config, COL, service, sheet_name, start_row, end_row, category=None):
```

to:

```python
def run_discovery(config, COL, service, sheet_name, start_row, end_row, category=None, add_limit=None):
```

Update the subprocess command build inside the function:

```python
    cmd = [sys.executable, os.path.join(os.path.dirname(__file__), "researcher.py"), "--discover-only"]
    if category:
        cmd += ["--category", category]
    if add_limit:
        cmd += ["--add-limit", str(add_limit)]
    result = subprocess.run(cmd, capture_output=False)
```

- [ ] **Step 4: Add `--add-limit` CLI arg and pass it to `run_discovery`**

In `agents/scheduler.py`, find the `main()` argparse block (line ~858). Add after the `--force` argument:

```python
    parser.add_argument("--add-limit", type=int, default=None,
                        help="Max new products to add to sheet during discovery")
```

Then find where `run_discovery` is called (line ~903) and update:

```python
        elif args.mode == "discovery":
            run_discovery(config, COL, service, sheet_name, start_row, end_row,
                          category=args.category, add_limit=args.add_limit)
```

- [ ] **Step 5: Run tests to verify they pass**

```
pytest tests/test_menu.py::test_run_discovery_passes_add_limit_to_subprocess tests/test_menu.py::test_run_discovery_omits_add_limit_when_none tests/test_menu.py::test_scheduler_argparse_accepts_add_limit -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```
git add agents/scheduler.py tests/test_menu.py
git commit -m "feat: wire add_limit through scheduler run_discovery and CLI"
```

---

### Task 3: Add `prompt_limit()` to menu.py and wire it up

**Files:**
- Modify: `agents/menu.py`
- Test: `tests/test_menu.py` (continue adding)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_menu.py`:

```python
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
```

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/test_menu.py::test_prompt_limit_returns_int_on_valid_input tests/test_menu.py::test_prompt_limit_returns_none_on_blank tests/test_menu.py::test_prompt_limit_retries_on_invalid_then_accepts tests/test_menu.py::test_discovery_items_have_limit_prompt tests/test_menu.py::test_non_discovery_items_have_no_limit_prompt tests/test_menu.py::test_run_item_appends_add_limit_to_command tests/test_menu.py::test_run_item_omits_add_limit_when_none -v
```

Expected: FAIL — `prompt_limit` not defined, `limit_prompt` not on items.

- [ ] **Step 3: Add `prompt_limit()` function to menu.py**

In `agents/menu.py`, after the `prompt_category` function (line ~133), add:

```python
def prompt_limit():
    """Returns a positive int (items to add) or None (no limit)."""
    while True:
        raw = input("How many new items to add? (Enter for all): ").strip()
        if not raw:
            return None
        try:
            n = int(raw)
            if n > 0:
                return n
        except ValueError:
            pass
        print("Enter a positive number or press Enter to add all.")
```

- [ ] **Step 4: Add `limit_prompt: True` to both discovery items in `MENU_GROUPS`**

In `agents/menu.py`, find the two discovery items (lines ~34–35). Update both:

```python
{"label": "Discover new products — all categories", "mode": "discovery", "category_prompt": False, "args": [], "limit_prompt": True},
{"label": "Discover new products — pick category",  "mode": "discovery", "category_prompt": True,  "args": [], "limit_prompt": True},
```

- [ ] **Step 5: Update `run_item` to accept and forward `add_limit`**

In `agents/menu.py`, find `def run_item(item, category=None):` (line ~136). Change to:

```python
def run_item(item, category=None, add_limit=None):
    """Invoke the subprocess for the selected menu item."""
    if item.get("script"):
        cmd = [sys.executable, item["script"]]
    else:
        cmd = [sys.executable, SCHEDULER, "--mode", item["mode"]] + item.get("args", [])
        if category:
            cmd += ["--category", category]
        if add_limit:
            cmd += ["--add-limit", str(add_limit)]
    print()
    print(f"{Fore.CYAN}Running: {' '.join(cmd)}{Style.RESET_ALL}")
    print()
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"{Fore.RED}Command exited with code {result.returncode}{Style.RESET_ALL}")
    input("\nPress Enter to return to menu...")
```

- [ ] **Step 6: Wire `prompt_limit()` into `main()`**

In `agents/menu.py`, find the `main()` function's item-handling block (line ~171). Update:

```python
        item     = all_items[idx]
        category = None
        if item["category_prompt"]:
            category = prompt_category(categories)
            if category is None:
                continue
        add_limit = None
        if item.get("limit_prompt"):
            add_limit = prompt_limit()
        run_item(item, category=category, add_limit=add_limit)
```

- [ ] **Step 7: Run all new tests**

```
pytest tests/test_menu.py::test_prompt_limit_returns_int_on_valid_input tests/test_menu.py::test_prompt_limit_returns_none_on_blank tests/test_menu.py::test_prompt_limit_retries_on_invalid_then_accepts tests/test_menu.py::test_discovery_items_have_limit_prompt tests/test_menu.py::test_non_discovery_items_have_no_limit_prompt tests/test_menu.py::test_run_item_appends_add_limit_to_command tests/test_menu.py::test_run_item_omits_add_limit_when_none -v
```

Expected: PASS.

- [ ] **Step 8: Run full test suite to check for regressions**

```
pytest tests/test_menu.py -v
```

Expected: All tests PASS.

- [ ] **Step 9: Commit**

```
git add agents/menu.py tests/test_menu.py
git commit -m "feat: add prompt_limit to menu discovery options and wire --add-limit through run_item"
```
