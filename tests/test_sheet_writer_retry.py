"""Fast, fully-mocked tests for tools/sheet_writer.execute_with_retry and its call sites."""
import os
import socket
import sys
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import sheet_writer


def _http_error(status):
    resp = MagicMock()
    resp.status = status
    resp.reason = "test"
    return HttpError(resp, b"{}")


def _request(*outcomes):
    """A fake googleapiclient request whose .execute() yields each outcome in turn."""
    req = MagicMock()
    req.execute.side_effect = list(outcomes)
    return req


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(sheet_writer.time, "sleep", lambda s: calls.append(s))
    return calls


def test_success_first_try_no_sleep(sleeps):
    assert sheet_writer.execute_with_retry(_request({"ok": 1})) == {"ok": 1}
    assert sleeps == []


def test_429_then_success_retries_once(sleeps):
    req = _request(_http_error(429), {"ok": 1})
    assert sheet_writer.execute_with_retry(req) == {"ok": 1}
    assert sleeps == [1]
    assert req.execute.call_count == 2


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retryable_statuses_exhaust_with_exponential_backoff(sleeps, status):
    req = _request(*[_http_error(status)] * 6)
    with pytest.raises(HttpError):
        sheet_writer.execute_with_retry(req)
    assert sleeps == [1, 2, 4, 8, 16]
    assert req.execute.call_count == 6  # 1 attempt + 5 retries


def test_socket_timeout_is_retried(sleeps):
    req = _request(socket.timeout("slow"), {"ok": 1})
    assert sheet_writer.execute_with_retry(req) == {"ok": 1}
    assert sleeps == [1]


@pytest.mark.parametrize("status", [400, 403, 404])
def test_non_retryable_status_raises_immediately(sleeps, status):
    req = _request(_http_error(status))
    with pytest.raises(HttpError):
        sheet_writer.execute_with_retry(req)
    assert sleeps == []
    assert req.execute.call_count == 1


def test_every_retry_is_logged(sleeps, monkeypatch):
    logged = []
    monkeypatch.setattr(sheet_writer.logger, "warning", lambda msg: logged.append(msg))
    sheet_writer.execute_with_retry(_request(_http_error(429), _http_error(503), {"ok": 1}), "unit")
    assert len(logged) == 2
    assert "retry 1/5" in logged[0] and "429" in logged[0] and "unit" in logged[0]
    assert "retry 2/5" in logged[1] and "503" in logged[1]


def _service_with_write_failures(n_failures):
    """Service whose values().update()/batchUpdate() .execute() 429s n times then succeeds."""
    service = MagicMock()
    values = service.spreadsheets.return_value.values.return_value
    for name in ("update", "batchUpdate"):
        getattr(values, name).return_value.execute.side_effect = (
            [_http_error(429)] * n_failures + [{}]
        )
    values.get.return_value.execute.return_value = {"values": [["h"]] * 5}
    return service, values


def test_write_cell_retries(sleeps, monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sid")
    service, values = _service_with_write_failures(2)
    sheet_writer.write_cell(service, "Tab", "A", 5, "x")
    assert values.update.return_value.execute.call_count == 3
    assert sleeps == [1, 2]


def test_write_row_partial_retries(sleeps, monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sid")
    service, values = _service_with_write_failures(1)
    sheet_writer.write_row_partial(service, "Tab", 5, [("A", 1), ("C", 2)])
    assert values.batchUpdate.return_value.execute.call_count == 2


def test_append_row_retries_only_the_failing_write_not_the_row_lookup(sleeps, monkeypatch):
    """A retried write must not re-read A:A, or a write that actually landed would double-append."""
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sid")
    service, values = _service_with_write_failures(1)
    row = sheet_writer.append_row(service, "Tab", {"A": "t"}, COL={}, data_start_row=4)
    assert row == 6
    assert values.get.return_value.execute.call_count == 1
    assert values.batchUpdate.return_value.execute.call_count == 2


def test_append_rows_batch_retries(sleeps, monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sid")
    service, values = _service_with_write_failures(1)
    rows = sheet_writer.append_rows_batch(service, "Tab", [{"A": 1}, {"A": 2}])
    assert rows == [6, 7]
    assert values.batchUpdate.return_value.execute.call_count == 2


def test_read_sheet_retries_and_returns_values(sleeps, monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sid")
    service = MagicMock()
    get = service.spreadsheets.return_value.values.return_value.get
    get.return_value.execute.side_effect = [_http_error(429), {"values": [["a", "b"]]}]
    assert sheet_writer.read_sheet(service, "Tab!A1:B1") == [["a", "b"]]
    assert get.return_value.execute.call_count == 2
    assert sleeps == [1]


def test_read_sheet_missing_values_key_returns_empty(sleeps, monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sid")
    service = MagicMock()
    service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {}
    assert sheet_writer.read_sheet(service, "Tab!A1:B1") == []
