"""Tests for the Sheet grid-size guard (writes past the grid fail with 'exceeds grid limits')."""
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import sheet_writer
from tools.sheet_writer import ensure_grid_columns, required_grid_columns


def _service(title="Product Tracker", columns=49, sheet_id=123):
    svc = MagicMock()
    svc.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"sheetId": sheet_id, "title": title,
                                   "gridProperties": {"columnCount": columns}}}]
    }
    svc.spreadsheets().batchUpdate().execute.return_value = {}
    svc.reset_mock()  # forget the setup calls above; keep return values
    return svc


def test_required_grid_columns_uses_rightmost_column():
    assert required_grid_columns({"a": "A", "b": "Z", "c": "AA"}) == 27
    assert required_grid_columns({"x": "BA"}) == 53


def test_required_grid_columns_matches_real_col_map():
    # AX-BA are the MPT columns; the grid must reach BA (53 cols)
    assert required_grid_columns() >= 53


def test_ensure_grid_expands_when_too_small(monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet-abc")
    svc = _service(columns=49)
    assert ensure_grid_columns(svc, "Product Tracker", 53) is True
    body = svc.spreadsheets().batchUpdate.call_args.kwargs["body"]
    req = body["requests"][0]["updateSheetProperties"]
    assert req["properties"] == {"sheetId": 123, "gridProperties": {"columnCount": 53}}
    assert req["fields"] == "gridProperties.columnCount"
    assert svc.spreadsheets().batchUpdate.call_args.kwargs["spreadsheetId"] == "sheet-abc"


def test_ensure_grid_noop_when_large_enough(monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet-abc")
    svc = _service(columns=60)
    assert ensure_grid_columns(svc, "Product Tracker", 53) is False
    svc.spreadsheets().batchUpdate.assert_not_called()


def test_ensure_grid_never_shrinks(monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet-abc")
    svc = _service(columns=53)
    assert ensure_grid_columns(svc, "Product Tracker", 53) is False
    svc.spreadsheets().batchUpdate.assert_not_called()


def test_ensure_grid_unknown_tab_raises(monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet-abc")
    with pytest.raises(ValueError):
        ensure_grid_columns(_service(title="Other"), "Product Tracker", 53)
