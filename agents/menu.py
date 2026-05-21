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
