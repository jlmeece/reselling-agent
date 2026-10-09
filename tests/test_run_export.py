"""run_export (scheduler --mode export): photos -> upload -> CSV, all three tools mocked."""
import json
from pathlib import Path

import pytest

from agents import scheduler as sch
from tools import ebay_export, photo_compositor, upload_photos

COL = sch.load_col_map()
START = 4


def _row(status="READY", sku="X1"):
    row = [""] * 50
    row[sch.col_to_idx(COL["status"])] = status
    row[sch.col_to_idx(COL["sku"])] = sku
    return row


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Mocks every step; `calls` records the order they ran in."""
    calls, sent = [], []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setattr(sch, "EXPORT_STATE_PATH", str(tmp_path / "export_state.json"))
    monkeypatch.setattr(photo_compositor, "load_template", lambda: calls.append("template") or {"box": None})
    monkeypatch.setattr(photo_compositor, "_load_columns", lambda: {})
    monkeypatch.setattr(photo_compositor, "build_jobs", lambda rows, start, cols: ["job"])

    def photos(jobs, tpl, dry_run=False):
        calls.append(("photos", dry_run))
        return {"processed": 1, "done": 2, "no_image": 0, "no_sku": 3, "failed": []}

    def upload(dry_run=False):
        calls.append(("upload", dry_run))
        return {"uploaded": 0 if dry_run else 1, "skipped": 2, "failed": [], "planned": 1}

    def gallery(jobs, dry_run=False):
        calls.append(("gallery", dry_run))
        return {"uploaded": 1, "skipped": 0, "failed": []}

    def generate(eligible, config):
        calls.append("csv")
        return "Action,Title\n" + "".join(f"Add,row{r}\n" for r, _ in eligible)

    def write(csv_text):
        calls.append("write")
        p = tmp_path / "ebay_upload_20261008_103000.csv"
        p.write_text(csv_text)
        return p

    monkeypatch.setattr(photo_compositor, "run", photos)
    monkeypatch.setattr(upload_photos, "run", upload)
    monkeypatch.setattr(upload_photos, "upload_gallery", gallery)
    monkeypatch.setattr(upload_photos, "load_hosted", lambda: {"X1": {"url": "https://i.ebayimg.com/a.jpg"}})
    monkeypatch.setattr(ebay_export, "generate_ebay_csv", generate)
    monkeypatch.setattr(ebay_export, "write_csv", write)
    monkeypatch.setattr(ebay_export, "_load_config", lambda: {})
    monkeypatch.setattr(sch, "_send_telegram_document",
                        lambda t, c, path, cap: sent.append((Path(path).name, cap)) or True)
    monkeypatch.setattr(sch, "_send_telegram", lambda t, c, m: sent.append((None, m)) or True)
    return calls, sent


def _run(monkeypatch, rows, dry_run=False):
    monkeypatch.setattr(sch, "read_sheet", lambda *a, **k: rows)
    return sch.run_export({}, COL, None, "Product Tracker", START, 500, dry_run=dry_run)


def test_dry_run_chains_four_steps_in_order_and_summarizes(env, monkeypatch, tmp_path):
    calls, sent = env
    res = _run(monkeypatch, [_row(), _row(status="APPROVED"), _row(sku="X2")], dry_run=True)
    assert calls == [("photos", True), ("upload", True), ("gallery", True)]  # CSV only counted: no template/csv/write
    assert res["status"] == "ok" and "errors" not in res
    assert res["notes"] == ("[dry-run] photos would make 1, have 2, no SKU 3, no image 0; "
                            "upload would host 1, unchanged 2; gallery would host 1, unchanged 0; "
                            "would export 2 READY row(s)")
    assert sent == [] and not (tmp_path / "export_state.json").exists()


def test_real_run_order_writes_csv_and_sends_one_document(env, monkeypatch, tmp_path):
    calls, sent = env
    res = _run(monkeypatch, [_row(), _row(sku="X2")])
    assert calls == ["template", ("photos", False), ("upload", False), ("gallery", False), "csv", "write"]
    assert res["status"] == "ok"
    assert "exported 2 listing(s) -> ebay_upload_20261008_103000.csv (1 with branded photo)" in res["notes"]
    assert len(sent) == 1
    name, caption = sent[0]
    assert name == "ebay_upload_20261008_103000.csv"
    assert caption.startswith("📦 Export ready — 2 listing(s). CSV: ebay_upload_20261008_103000.csv. "
                              "Upload to Seller Hub.")
    assert "Branded main photo: 1/2" in caption
    assert json.loads((tmp_path / "export_state.json").read_text())["rows"] == 2


def test_unchanged_export_is_not_resent(env, monkeypatch):
    calls, sent = env
    _run(monkeypatch, [_row()])
    calls.clear()
    res = _run(monkeypatch, [_row()])
    assert "write" not in calls and len(sent) == 1
    assert "unchanged since ebay_upload_20261008_103000.csv" in res["notes"]


def test_zero_ready_rows_is_silent(env, monkeypatch):
    calls, sent = env
    res = _run(monkeypatch, [_row(status="APPROVED")])
    assert "csv" not in calls and sent == []
    assert res["status"] == "ok" and "0 READY" in res["notes"]


def test_photo_and_upload_failures_do_not_block_the_csv(env, monkeypatch):
    calls, sent = env

    def boom(*a, **k):
        raise RuntimeError("PIL exploded")

    monkeypatch.setattr(photo_compositor, "run", boom)
    monkeypatch.setattr(upload_photos, "run", lambda dry_run=False: {
        "uploaded": 0, "skipped": 0, "planned": 1, "failed": [("X1", "931: token")]})
    res = _run(monkeypatch, [_row()])
    assert "write" in calls and len(sent) == 1
    assert "2 photo/upload problem(s)" in sent[0][1]
    assert res["status"] == "error" and "photo step crashed" in res["errors"]


def test_missing_template_skips_photos_only(env, monkeypatch):
    calls, sent = env

    def missing():
        raise FileNotFoundError("Template not found")

    monkeypatch.setattr(photo_compositor, "load_template", missing)
    res = _run(monkeypatch, [_row()])
    assert "photos skipped — template missing" in res["notes"]
    assert res["status"] == "ok" and len(sent) == 1


def test_failed_telegram_keeps_state_unsaved_so_next_run_retries(env, monkeypatch, tmp_path):
    calls, sent = env
    monkeypatch.setattr(sch, "_send_telegram_document", lambda *a: False)
    monkeypatch.setattr(sch, "_send_telegram", lambda *a: False)
    res = _run(monkeypatch, [_row()])
    assert res["status"] == "error" and "not delivered" in res["errors"]
    assert not (tmp_path / "export_state.json").exists()
