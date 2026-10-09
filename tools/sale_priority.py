"""
Sale-window priority — shared sort key for the research queue (agents/researcher.py) and
the Telegram review queue (agents/telegram_bot.py extract_review_queue).

On-sale items have a deadline: if they aren't researched, approved and listed before the
Costco sale ends, the margin they were picked for is gone. Both queues put them first.
"""

from datetime import date

from tools.sale_history import parse_sale_end


def sale_end_sort_key(badge, today=None):
    """
    Sort key for a col X sale badge ("🔥 -$8 ends 10/18/26"); use with a stable sort:
      (0, 'YYYY-MM-DD')  badge with an end date today or later — soonest first
      (1, '')            badge with no parseable end date
      (2, '')            blank, an already-expired badge, or anything unreadable
    Never raises.
    """
    try:
        badge = str(badge or "").strip()
        if not badge:
            return (2, "")
        today = today or date.today()
        end = parse_sale_end(badge, today=today)
        if not end:
            return (1, "")
        if end < today.isoformat():
            return (2, "")    # sale already over (col X can hold stale badges)
        return (0, end)
    except Exception:
        return (2, "")
