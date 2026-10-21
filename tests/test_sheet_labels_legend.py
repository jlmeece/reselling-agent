"""Header label + Legend text must match col_map and the live formulas (audit 10/21:
AE was labelled FULFILLMENT but holds ad_cost; the Legend showed net = H-G-AC-AD-AE)."""
from agents.scheduler import col_to_idx, load_col_map
from agents.setup_sheet import legend_rows
from tools.formula_seeder import net_profit_formula
from tools.sheet_formatter import HEADER_LABELS, _build_legend_rows

COL = load_col_map()


def test_ae_header_is_ad_cost():
    assert COL["ad_cost"] == "AE"
    assert HEADER_LABELS[col_to_idx("AE")] == "AD COST"
    assert "FULFILLMENT" not in HEADER_LABELS


def _formatter_row(col):
    rows, *_ = _build_legend_rows()
    return next(r for r in rows if r and r[0] == col)


def test_formatter_legend_shows_current_formulas():
    assert "H-G-AC-AD-AE-AF" in _formatter_row("I")[2]
    assert "1.08" in _formatter_row("AC")[2] and "0.30" in _formatter_row("AC")[2]
    assert _formatter_row("AE")[1] == "AD COST"
    rows, *_ = _build_legend_rows()
    # USER_ENTERED write: a cell starting with "=" would become a live formula on the Legend tab
    assert not any(str(c).startswith("=") for r in rows for c in r[1:] if r and r[0] == "I")


def test_setup_sheet_legend_matches_seeded_net_formula():
    net = next(r for r in legend_rows() if r[0].startswith("I "))
    assert net[1] == '=IF(H<>"",H-G-AC-AD-AE-AF,"")'
    assert "H4-G4-AC4-AD4-AE4-AF4" in net_profit_formula(4)     # same terms as the seeder
    ac = next(r for r in legend_rows() if r[0].startswith("AC ("))
    assert ac[1] == "=H*AB*1.08+0.30"
    assert not any(r[0].startswith("AE  fulfillment") for r in legend_rows())
