"""Bot Audit-queue Delete refuses a row whose eBay listing is live (2026-10-19 orphan incident)."""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agents.telegram_bot as tb

from agents.scheduler import load_col_map  # noqa: E402

COL = load_col_map()


def _row(ebay_url):
    cells = {"status": "AUDIT_REVIEW", "title": "Grand Leisure City Heights",
             "category": "Outdoor Furniture", "ebay_listing_url": ebay_url,
             "costco_url": "https://www.costco.com/x.product.1.html", "sku": "SKU1"}
    row = [""] * 60
    for k, v in cells.items():
        row[tb.col_to_idx(COL[k])] = v
    return row


@pytest.fixture
def bot(monkeypatch):
    state = {"rows": [], "graveyard": [], "deleted": []}
    monkeypatch.setattr(tb, "_read_product_rows", lambda: (COL, "svc", "Product Tracker", 4, state["rows"]))
    monkeypatch.setattr(tb, "write_to_graveyard", lambda svc, rows: state["graveyard"].extend(rows))
    monkeypatch.setattr(tb, "_delete_sheet_row",
                        lambda svc, sheet, row_num, ctx: state["deleted"].append(row_num))
    monkeypatch.setattr(tb, "extract_audit_queue", lambda *a, **k: [])
    monkeypatch.setattr(tb, "_finish_audit_queue", AsyncMock())

    def run(ebay_url):
        state["rows"] = [_row(ebay_url)]
        query = SimpleNamespace(edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query, message=None)
        ctx = SimpleNamespace(user_data={}, bot_data={"chat_id": 1})
        asyncio.run(tb.cb_audit_confirm_delete(update, ctx, "4"))
        call = query.edit_message_text.call_args
        state["screen"] = call[0][0] if call else ""
        return state
    return run


def test_delete_blocked_when_listing_is_live(bot):
    st = bot("https://www.ebay.com/itm/860049380044")
    assert st["deleted"] == [] and st["graveyard"] == []
    assert "this listing is live on eBay" in st["screen"]
    assert "end or hide it first, then delete" in st["screen"] and "860049380044" in st["screen"]


@pytest.mark.parametrize("url", ["", "https://www.ebay.com/sch/i.html?_nkw=x"])
def test_delete_proceeds_without_live_listing_and_keeps_costco_url(bot, url):
    st = bot(url)
    assert st["deleted"] == [4]
    (g,) = st["graveyard"]
    assert g["costco_url"] == "https://www.costco.com/x.product.1.html" and g["sku"] == "SKU1"
