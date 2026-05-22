# Discovery Add-Limit — Design Spec
**Date:** 2026-05-22  
**Status:** Approved

## Overview

Add a "how many items to add" prompt to the startup menu's discovery options so the user can cap how many new PENDING rows get written to the sheet per run. Applies to both "Discover all categories" and "Discover pick category."

---

## Architecture

**Modified files:**
- `agents/menu.py` — new `prompt_limit()` function; `limit_prompt` field on discovery items; wiring in `main()` and `run_item()`
- `agents/scheduler.py` — `run_discovery()` accepts `add_limit` param and forwards it; CLI arg added
- `agents/researcher.py` — new `--add-limit` arg; slices `new_products[:add_limit]` before sheet write

No new files. No changes to other modes (research, recheck, etc.).

---

## Menu UX Flow

### Pick category (option 2)
```
Pick a number: 2

Categories:
  1. Precious Metals
  2. Jewelry
  ...

Pick a category (or 0 to go back): 2

How many new items to add? (Enter for all): 10

Running: python researcher.py --discover-only --category Jewelry --add-limit 10
```

### All categories (option 1)
```
Pick a number: 1

How many new items to add? (Enter for all): 10

Running: python researcher.py --discover-only --add-limit 10
```

### Validation
- Blank input → no limit (add all new products)
- Non-integer or `<= 0` → re-prompt: "Enter a positive number or press Enter to add all."
- `0` in the category sub-prompt still means "go back" (unchanged)

---

## Code Changes

### `agents/researcher.py`

Add CLI arg:
```python
parser.add_argument("--add-limit", type=int, default=None,
                    help="Max new products to add to sheet during discovery")
```

Pass to `run_researcher`:
```python
run_researcher(limit=args.limit, add_limit=args.add_limit, ...)
```

In `run_researcher`, after computing `new_products` and before `_add_new_products_batch`:
```python
if add_limit and len(new_products) > add_limit:
    logger.info(f"  Capping at {add_limit} new products (found {len(new_products)}).")
    new_products = new_products[:add_limit]
```

`run_researcher` signature gains `add_limit=None`.

### `agents/scheduler.py`

`run_discovery` signature:
```python
def run_discovery(config, COL, service, sheet_name, start_row, end_row, category=None, add_limit=None):
```

Subprocess build:
```python
if add_limit:
    cmd += ["--add-limit", str(add_limit)]
```

CLI arg in `main()`:
```python
parser.add_argument("--add-limit", type=int, default=None,
                    help="Max new products to add to sheet during discovery")
```

Pass to `run_discovery`:
```python
run_discovery(..., category=args.category, add_limit=args.add_limit)
```

### `agents/menu.py`

New function:
```python
def prompt_limit():
    """Returns positive int or None (no limit)."""
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

Both discovery items in `MENU_GROUPS` get `"limit_prompt": True`:
```python
{"label": "Discover new products — all categories", ..., "limit_prompt": True},
{"label": "Discover new products — pick category",  ..., "limit_prompt": True},
```

`main()` loop: after category sub-prompt, before `run_item`:
```python
add_limit = None
if item.get("limit_prompt"):
    add_limit = prompt_limit()
run_item(item, category=category, add_limit=add_limit)
```

`run_item` signature and command build:
```python
def run_item(item, category=None, add_limit=None):
    ...
    if add_limit:
        cmd += ["--add-limit", str(add_limit)]
```

---

## Error Handling

- Invalid menu input: re-prompts (existing behavior unchanged)
- `add_limit` of `None` (user pressed Enter): no flag passed, researcher adds all new products
- `--add-limit` on non-discovery modes: ignored (arg is parsed but not forwarded)
