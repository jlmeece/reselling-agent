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
