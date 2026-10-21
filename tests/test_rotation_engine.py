"""run_rotation_check: the rotation note goes to col T (tier_summary). It used to read
col_map["notes"], a key col_map.yaml no longer has — KeyError every week since 10/04."""
import tools.sheet_writer as sheet_writer
from agents.scheduler import load_col_map, col_to_idx
from tools.rotation_engine import run_rotation_check

COL = load_col_map()


def _row(**cells):
    row = []
    for key, val in cells.items():
        i = col_to_idx(COL[key])
        while len(row) <= i:
            row.append("")
        row[i] = val
    return row


def test_rotation_note_written_to_tier_summary(monkeypatch):
    writes = []
    monkeypatch.setattr(sheet_writer, "write_row_partial",
                        lambda svc, sheet, row, pairs: writes.append((row, dict(pairs))))
    rows = [_row(status="ACTIVE", category="Pharmacy", title=f"Weak {i}", demand_score="1",
                 net_margin="0.01", units_sold="0") for i in range(3)]
    rows.append(_row(status="WATCH", category="Pharmacy", title="Replacement", demand_score="9",
                     net_margin="0.30", units_sold="0"))
    config = {"business": {"min_margin_threshold": 0.10},
              "categories": {"Pharmacy": {"max_active": 3, "capacity_headroom": 0,
                                          "rotation_threshold": 4.0}}}
    result = run_rotation_check("svc", "Product Tracker", rows, COL, config, start_row=4)
    assert len(result["Pharmacy"]) == 3
    notes = [w for _, w in writes if COL["tier_summary"] in w]
    assert len(notes) == 3
    assert all(n[COL["tier_summary"]].startswith("ROTATION CANDIDATE") for n in notes)
    assert "Replacement" in notes[0][COL["tier_summary"]]
