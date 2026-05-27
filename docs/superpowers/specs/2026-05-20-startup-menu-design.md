# Startup Menu — Design Spec
**Date:** 2026-05-20  
**Status:** Approved

## Overview

Add an interactive startup menu so the agent can be launched without memorizing CLI commands. Triggered via `.\run.ps1 menu`. Groups all existing agent modes into a numbered, navigable interface with live last-run timestamps and category sub-prompts.

---

## Architecture

**New file:** `agents/menu.py`  
**Modified file:** `run.ps1` (add `menu` case)

`menu.py` is a pure launcher — it reads config, renders the menu, and delegates to existing scripts via `subprocess`. No changes to `scheduler.py` or any tool.

### Data sources
- `config/categories.yaml` — loads the live category list for sub-prompts
- `data/run_history.json` — provides `[last: YYYY-MM-DD HH:MM]` timestamps per mode

### Dependencies
- `colorama` for colored output — graceful plain-text fallback if not installed
- `yaml`, `json`, `subprocess` — all stdlib or already in requirements

### Flow
1. `.\run.ps1 menu` → `python agents/menu.py`
2. Menu loop: display groups → user picks number → run command (or show category sub-prompt first)
3. After command exits, return to main menu
4. `0` exits

---

## Menu Structure

```
WAT Reselling Agent
===================

[1] RESEARCH
    1. Discover new products — all categories    [last: 2026-05-19 07:01]
    2. Discover new products — pick category
    3. Score PENDING rows — all categories       [last: 2026-05-19 10:02]
    4. Score PENDING rows — pick category

[2] PRICE & STOCK
    5. Recheck failed / missing prices           [last: 2026-05-20 08:14]
    6. Force full refresh (all rows)
    7. Active listings monitor                   [last: 2026-05-20 13:00]

[3] MAINTENANCE
    8. Daily sweep (APPROVED→READY, PAUSED_OOS)  [last: 2026-05-20 09:01]
    9. Rotation digest (weekly)
   10. Refresh Costco session cookies
   11. Sheet formatter / setup

[0] Exit
```

### Category sub-prompt (options 2 and 4)

```
Categories:
  1. Precious Metals
  2. Jewelry
  3. Outdoor Furniture
  4. Watches
  5. Pharmacy
  6. Small Appliances
  7. Toys

Pick a category (or 0 to go back):
```

Category list is loaded live from `categories.yaml`. Falls back to free-text input if list is unavailable.

---

## Command Mapping

| Menu # | Label | Invokes |
|--------|-------|---------|
| 1 | Discover — all | `scheduler.py --mode discovery` |
| 2 | Discover — pick category | `scheduler.py --mode discovery --category <X>` |
| 3 | Score PENDING — all | `scheduler.py --mode research` |
| 4 | Score PENDING — pick category | `scheduler.py --mode research --category <X>` |
| 5 | Recheck failed/missing | `scheduler.py --mode recheck` |
| 6 | Force full refresh | `scheduler.py --mode recheck --force` |
| 7 | Active monitor | `scheduler.py --mode active` |
| 8 | Daily sweep | `scheduler.py --mode daily` |
| 9 | Rotation digest | `scheduler.py --mode rotation` |
| 10 | Refresh cookies | `tools/setup_costco_session.py` |
| 11 | Sheet formatter | `agents/setup_sheet.py` |

---

## Error Handling

- **Invalid input:** re-prompts, no crash
- **Subprocess non-zero exit:** prints exit code, returns to menu
- **`run_history.json` missing/corrupt:** timestamps omitted silently
- **`colorama` not installed:** plain text output, fully functional
- **Category list unavailable:** falls back to free-text category input
