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
        "label": "PIPELINE",
        "items": [
            {"label": "1. Discover new Costco products (all categories)",    "mode": "discovery", "category_prompt": False, "args": [], "limit_prompt": True},
            {"label": "2. Discover new Costco products (pick category)",     "mode": "discovery", "category_prompt": True,  "args": [], "limit_prompt": True},
            {"label": "3. Research & score queued products (all)",           "mode": "research",  "category_prompt": False, "args": []},
            {"label": "4. Research & score queued products (pick category)", "mode": "research",  "category_prompt": True,  "args": []},
            {"label": "5. Export READY products → eBay CSV",                "action": "ebay_export", "category_prompt": False, "args": []},
        ],
    },
    {
        "label": "MONITORING",
        "items": [
            {"label": "6. Check active eBay listings (OOS / price changes)", "mode": "active", "category_prompt": False, "args": []},
            {"label": "7. Run daily sweep (promote APPROVED, restock checks)", "mode": "daily", "category_prompt": False, "args": []},
        ],
    },
    {
        "label": "MAINTENANCE",
        "items": [
            {"label": "8. Audit: clean junk rows (CFO mode)",                "mode": "audit",          "category_prompt": False, "args": []},
            {"label": "9. Weekly performance digest",                         "mode": "rotation",       "category_prompt": False, "args": []},
            {"label": "10. Fix products with missing eBay prices",            "mode": "recheck",        "category_prompt": False, "args": []},
            {"label": "11. Refresh all prices and stock status",              "mode": "recheck",        "category_prompt": False, "args": ["--force"]},
            {"label": "12. Reset PAUSED products → PENDING for re-research", "action": "reset_paused", "category_prompt": False, "args": []},
            {"label": "13. Refresh Costco session cookies",                   "mode": None,             "category_prompt": False, "args": [], "script": SETUP_COOKIES},
            {"label": "14. Sheet formatter / setup",                          "mode": None,             "category_prompt": False, "args": [], "script": SETUP_SHEET},
        ],
    },
]


def reset_paused_to_pending():
    sys.path.insert(0, PROJECT_ROOT)
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"), encoding="utf-8", override=True)
    from tools.sheet_writer import get_sheets_service, read_sheet, write_row_partial

    with open(os.path.join(PROJECT_ROOT, "config", "categories.yaml")) as f:
        config = yaml.safe_load(f)
    with open(os.path.join(PROJECT_ROOT, "config", "col_map.yaml")) as f:
        COL = yaml.safe_load(f)["columns"]

    business   = config["business"]
    sheet_name = business["sheet_name"]
    start_row  = business["data_start_row"]
    end_row    = business["data_end_row"]

    service  = get_sheets_service()
    all_data = read_sheet(service, f"'{sheet_name}'!A{start_row}:AV{end_row}")

    paused_rows = [
        idx + start_row
        for idx, row in enumerate(all_data)
        if row and row[0] == "PAUSED_DEMAND"
    ]

    if not paused_rows:
        print("No PAUSED_DEMAND rows found.")
        return

    raw = input(f"Reset {len(paused_rows)} PAUSED_DEMAND products to PENDING? (y/n): ").strip().lower()
    if raw != "y":
        print("Cancelled.")
        return

    for sheet_row in paused_rows:
        write_row_partial(service, sheet_name, sheet_row, [
            (COL["status"],       "PENDING"),
            (COL["re_eval_date"], ""),
        ])
    print(f"{len(paused_rows)} rows reset to PENDING.")


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
    for group in MENU_GROUPS:
        print()
        print(f"{Fore.YELLOW}── {group['label']} ──{Style.RESET_ALL}")
        for item in group["items"]:
            ts = last_runs.get(item.get("mode") or "", "")
            ts_str = f"  {Fore.CYAN}[last: {ts}]{Style.RESET_ALL}" if ts else ""
            print(f"  {item['label']}{ts_str}")
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


def run_item(item, category=None, add_limit=None):
    """Invoke the subprocess for the selected menu item."""
    if item.get("action") == "reset_paused":
        reset_paused_to_pending()
        input("\nPress Enter to return to menu...")
        return
    elif item.get("action") == "ebay_export":
        subprocess.run([sys.executable, "tools/ebay_export.py"])
        input("\nPress Enter to return to menu...")
        return
    if item.get("script"):
        cmd = [sys.executable, item["script"]]
    else:
        cmd = [sys.executable, SCHEDULER, "--mode", item["mode"]] + item.get("args", [])
        if category:
            cmd += ["--category", category]
        if add_limit is not None:
            cmd += ["--add-limit", str(add_limit)]
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
        add_limit = None
        if item.get("limit_prompt"):
            add_limit = prompt_limit()
        run_item(item, category=category, add_limit=add_limit)


if __name__ == "__main__":
    main()
