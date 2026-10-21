"""Col I (net profit) must be blank — not a computed negative — when col H is blank, or the
auditor reads an un-priced row as "Negative net profit" and deletes it (r85 KitchenAid, 10/21)."""
from tools.formula_seeder import net_profit_formula
from tools.reseed_formulas import build_requests


def _formulas_by_col(requests, row):
    """{col_index: formula} for the updateCells requests targeting sheet row `row`."""
    out = {}
    for req in requests:
        uc = req["updateCells"]
        if uc["range"]["startRowIndex"] != row - 1:
            continue
        val = uc["rows"][0]["values"][0]["userEnteredValue"]
        out[uc["range"]["startColumnIndex"]] = val.get("formulaValue", val.get("numberValue"))
    return out


def _eval_net(formula, H, G, AC, AD, AE, AF):
    """Evaluate the guarded net formula the way Sheets would (blank H -> "")."""
    assert formula.startswith('=IF(H85="","",')
    if H == "":
        return ""
    return H - G - AC - AD - AE - AF


def test_net_formula_is_guarded():
    assert net_profit_formula(85) == '=IF(H85="","",H85-G85-AC85-AD85-AE85-AF85)'


def test_blank_h_row_evaluates_blank_not_negative():
    f = net_profit_formula(85)
    # r85 KitchenAid: H blank, G 299.99 -> old formula gave -299.99
    assert _eval_net(f, "", 299.99, 0.30, 0, 0, 24.75) == ""


def test_priced_row_keeps_the_same_net():
    f = net_profit_formula(85)
    H, G, AB = 100.0, 60.0, 0.1325
    AC, AF = H * AB * 1.08 + 0.30, G * 0.0825
    assert _eval_net(f, H, G, AC, 5.0, 0.0, AF) == H - G - AC - 5.0 - 0.0 - AF


def test_reseed_writes_guarded_net_for_blank_h_row():
    rows = [["PENDING", "", "KitchenAid"] + [""] * 25]          # r4, H blank
    reqs = build_requests(rows, tab_id=7, start_row=4)
    assert _formulas_by_col(reqs, 4)[8] == '=IF(H4="","",H4-G4-AC4-AD4-AE4-AF4)'


def test_reseed_writes_guarded_net_for_priced_row_and_skips_blank_status():
    priced = ["ACTIVE", "", "Kettle", "", "", "", "129.99", "149.99"] + [""] * 20
    rows = [priced, [""] * 28]                                    # r4 priced, r5 empty
    reqs = build_requests(rows, tab_id=7, start_row=4)
    f4 = _formulas_by_col(reqs, 4)
    assert f4[8] == '=IF(H4="","",H4-G4-AC4-AD4-AE4-AF4)'
    assert f4[28] == "=H4*AB4*1.08+0.30"
    assert f4[31] == "=G4*0.0825"
    assert _formulas_by_col(reqs, 5) == {}
