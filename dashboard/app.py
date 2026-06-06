"""
JA_Liquidations — Seller Intelligence Dashboard
================================================
Industrial seller-tool aesthetic: dark terminal meets modern SaaS.
Near-black background, amber accents, monospace data, high-density layout.

Run: python -m streamlit run dashboard/app.py
     (from the reselling-agent project root)
"""

import os
import sys
import json
import re
from datetime import datetime, date as _date
from collections import defaultdict

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(encoding="utf-8", override=True)

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="JA_Liquidations — WAT Dashboard",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Industrial dark theme CSS ─────────────────────────────────────────────────

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@400;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');

/* Root colors */
:root {
    --bg:        #0A0E1A;
    --surface:   #111827;
    --border:    #1E2D45;
    --amber:     #F5A623;
    --amber-dim: #B37A15;
    --green:     #39D353;
    --red:       #FF4444;
    --blue:      #4A9EFF;
    --purple:    #C084FC;
    --teal:      #2DD4BF;
    --muted:     #6B7280;
    --text:      #E5E7EB;
    --bright:    #F9FAFB;
}

/* Base */
html, body, .stApp { background: var(--bg) !important; color: var(--text); font-family: 'JetBrains Mono', monospace; }

/* Hide streamlit chrome */
#MainMenu, footer, header { visibility: hidden; }
.block-container { padding: 1.5rem 2rem 2rem; max-width: 1600px; }

/* Typography */
h1 { font-family: 'Barlow Condensed', sans-serif; font-size: 2.4rem; font-weight: 800;
     letter-spacing: 0.04em; color: var(--bright); margin-bottom: 0; }
h2 { font-family: 'Barlow Condensed', sans-serif; font-size: 1.3rem; font-weight: 700;
     letter-spacing: 0.08em; color: var(--amber); text-transform: uppercase;
     border-bottom: 1px solid var(--border); padding-bottom: 4px; margin-top: 1.4rem; }
h3 { font-family: 'Barlow Condensed', sans-serif; font-size: 1rem; font-weight: 600;
     color: var(--muted); text-transform: uppercase; letter-spacing: 0.1em; }

/* Metric cards */
div[data-testid="metric-container"] {
    background: var(--surface) !important;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 14px 16px !important;
}
div[data-testid="metric-container"] label {
    font-family: 'Barlow Condensed', sans-serif !important;
    font-size: 0.72rem !important;
    font-weight: 700 !important;
    letter-spacing: 0.12em !important;
    color: var(--muted) !important;
    text-transform: uppercase !important;
}
div[data-testid="metric-container"] [data-testid="metric-value"] {
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 1.8rem !important;
    font-weight: 600 !important;
    color: var(--bright) !important;
}

/* Dataframe / tables */
.stDataFrame { border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
.stDataFrame table { font-family: 'JetBrains Mono', monospace !important; font-size: 0.78rem; }
.stDataFrame thead th { background: var(--surface) !important; color: var(--amber) !important;
    font-family: 'Barlow Condensed', sans-serif !important; font-size: 0.8rem !important;
    font-weight: 700 !important; letter-spacing: 0.08em !important; text-transform: uppercase !important; }
.stDataFrame tbody tr:hover { background: #1A2333 !important; }

/* Buttons */
.stButton > button { background: var(--surface) !important; color: var(--amber) !important;
    border: 1px solid var(--amber-dim) !important; border-radius: 4px !important;
    font-family: 'Barlow Condensed', sans-serif !important; font-weight: 700 !important;
    font-size: 0.85rem !important; letter-spacing: 0.08em !important;
    text-transform: uppercase !important; padding: 6px 18px !important; }
.stButton > button:hover { background: var(--amber-dim) !important; color: #000 !important; }

/* Sidebar */
section[data-testid="stSidebar"] { background: var(--surface) !important; border-right: 1px solid var(--border); }

/* Status pills */
.pill {
    display: inline-block; padding: 2px 10px; border-radius: 12px;
    font-family: 'Barlow Condensed', sans-serif; font-size: 0.78rem;
    font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;
    margin: 2px 3px;
}
.pill-pending  { background: #3D3500; color: #FFD700; border: 1px solid #665A00; }
.pill-approved { background: #0D2D0D; color: #39D353; border: 1px solid #1A5C1A; }
.pill-ready    { background: #0A2222; color: #2DD4BF; border: 1px solid #155555; }
.pill-active   { background: #0D2400; color: #78FF55; border: 1px solid #1E5000; }
.pill-watch    { background: #0A1E3D; color: #4A9EFF; border: 1px solid #1A3A6A; }
.pill-paused   { background: #2D1700; color: #FF9500; border: 1px solid #5C3000; }
.pill-audit    { background: #2D1200; color: #FF6D00; border: 1px solid #5C2800; }

/* Alert cards */
.alert-card {
    background: var(--surface); border: 1px solid var(--border);
    border-left: 3px solid var(--amber); border-radius: 6px;
    padding: 12px 16px; margin: 6px 0; font-size: 0.82rem;
}
.alert-card.sale { border-left-color: var(--amber); }
.alert-card.ship { border-left-color: var(--teal); }
.alert-card.urgent { border-left-color: var(--red); }

/* Run log monospace */
.run-log { background: #080C14; border: 1px solid var(--border); border-radius: 4px;
    padding: 12px; font-family: 'JetBrains Mono', monospace; font-size: 0.75rem;
    color: #7CB9E8; line-height: 1.6; max-height: 260px; overflow-y: auto; }

/* Section divider */
.sec-divider { border: none; border-top: 1px solid var(--border); margin: 1rem 0 0.5rem; }

/* Score badge */
.score-t1 { color: var(--green); font-weight: 600; }
.score-t2 { color: #FFD700; font-weight: 600; }
.score-t3 { color: var(--red); }
</style>
""", unsafe_allow_html=True)

# ── Data loading ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=120)
def load_sheet_data():
    """Load all rows from Product Tracker via Sheets API. Cached 2 min."""
    try:
        from tools.sheet_writer import get_sheets_service, read_sheet
        import yaml
        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "categories.yaml")
        with open(config_path) as f:
            config = yaml.safe_load(f)
        business   = config["business"]
        sheet_name = business["sheet_name"]
        start_row  = business["data_start_row"]
        end_row    = business["data_end_row"]
        service    = get_sheets_service()
        rows       = read_sheet(service, f"'{sheet_name}'!A{start_row}:AW{end_row}")
        return rows, sheet_name
    except Exception as e:
        return [], f"error: {e}"


@st.cache_data(ttl=300)
def load_graveyard_data():
    """Load Graveyard tab. Returns list of dicts or empty list if tab missing."""
    try:
        from tools.sheet_writer import get_sheets_service
        svc = get_sheets_service()
        sheet_id = os.getenv("GOOGLE_SHEET_ID")
        result = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range="Graveyard!A1:M200"
        ).execute()
        rows = result.get("values", [])
        if len(rows) < 2:
            return []
        headers = rows[0]
        return [dict(zip(headers, r + [""] * (len(headers) - len(r)))) for r in rows[1:]]
    except Exception:
        return []


@st.cache_data(ttl=300)
def load_audit_log():
    """Load Audit Log tab. Returns list of dicts or empty list if tab missing."""
    try:
        from tools.sheet_writer import get_sheets_service
        svc = get_sheets_service()
        sheet_id = os.getenv("GOOGLE_SHEET_ID")
        result = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range="Audit Log!A1:H50"
        ).execute()
        rows = result.get("values", [])
        if len(rows) < 2:
            return []
        headers = rows[0]
        return [dict(zip(headers, r + [""] * (len(headers) - len(r)))) for r in rows[1:]]
    except Exception:
        return []


def safe(lst, i, default=""):
    return str(lst[i]).strip() if lst and i < len(lst) else default


def col_idx(letter):
    result = 0
    for c in letter.upper():
        result = result * 26 + (ord(c) - ord("A") + 1)
    return result - 1


def _safe_float(v, default=0.0):
    try:
        return float(str(v).replace("$", "").replace(",", "").strip() or default)
    except Exception:
        return default


def _parse_sale_expiry(sale_str):
    """Return datetime.date parsed from 'ends MM/DD' or 'ends MM/DD/YY', or None."""
    if not sale_str:
        return None
    m = re.search(r'ends\s+(\d{1,2}/\d{1,2}(?:/\d{2,4})?)', sale_str, re.IGNORECASE)
    if not m:
        return None
    try:
        parts = m.group(1).split('/')
        month, day = int(parts[0]), int(parts[1])
        year = int(parts[2]) if len(parts) > 2 else _date.today().year
        if year < 100:
            year += 2000
        return _date(year, month, day)
    except Exception:
        return None


def _pricing_str(p):
    """Compact pricing summary for tables: cost → eBay = net (margin%)"""
    cost  = p.get("cost")
    price = p.get("price")
    margin = p.get("margin")
    sale_val = p.get("sale_val", "")
    regular_price = p.get("regular_price", "")

    if cost is None or price is None:
        return "—"

    net     = price * margin if margin is not None else None
    net_pct = margin * 100   if margin is not None else None
    net_part = f" = ${net:,.2f} net ({net_pct:.0f}%)" if net is not None else ""

    if sale_val:
        reg_part = ""
        if regular_price:
            try:
                reg = float(str(regular_price).replace("$", "").replace(",", ""))
                reg_part = f" (${reg:,.2f} reg)"
            except (ValueError, TypeError):
                pass
        return f"🔥 ${cost:,.2f} sale{reg_part} → ${price:,.2f}{net_part}"
    return f"${cost:,.2f} → ${price:,.2f}{net_part}"


def _ready_note(p, today=None):
    """Return expiry warning if sale ends within 7 days."""
    if today is None:
        today = _date.today()
    if p.get("sale_val"):
        expiry = _parse_sale_expiry(p["sale_val"])
        if expiry and (expiry - today).days <= 7:
            return "⚠️ Sale expires soon"
    return ""


# Column indices based on col_map.yaml
A_STATUS   = 0
B_SCORE    = 1
C_TITLE    = 2
D_CAT      = 3
E_PLAT     = 4
F_STOCK    = 5
G_COST     = 6
H_PRICE    = 7
I_PROFIT   = 8
J_MARGIN   = 9
K_SOLD90   = 10
L_AVG      = 11
M_ACTIVE   = 12
N_COMP     = 13
O_CHECKED  = 14
R_URL      = 17
T_SUMMARY  = 19
V_SUGG     = 21
W_LIMIT    = 22
X_SALE     = 23
Y_SHIP     = 24
AV_NOTES   = 47
AW_REGULAR = 48


def parse_rows(rows):
    pipeline = defaultdict(int)
    products = []
    sale_items = []
    ship_items = []
    cat_stats  = defaultdict(lambda: {"count": 0, "score_sum": 0.0, "score_n": 0,
                                       "t1": 0, "margin_sum": 0.0, "margin_n": 0})
    top_pending = []

    for row in rows:
        if not row or not row[0]:
            continue
        status   = safe(row, A_STATUS)
        score_s  = safe(row, B_SCORE)
        title    = safe(row, C_TITLE)
        category = safe(row, D_CAT)
        stock    = safe(row, F_STOCK)
        cost_s   = safe(row, G_COST)
        price_s  = safe(row, H_PRICE)
        profit_s = safe(row, I_PROFIT)
        margin_s = safe(row, J_MARGIN)
        sold90   = safe(row, K_SOLD90)
        avg_s    = safe(row, L_AVG)
        comp_s   = safe(row, M_ACTIVE)
        checked  = safe(row, O_CHECKED)
        url      = safe(row, R_URL)
        summary  = safe(row, T_SUMMARY)
        sugg_s   = safe(row, V_SUGG)
        limit_s  = safe(row, W_LIMIT)
        sale_val         = safe(row, X_SALE)
        ship_val         = safe(row, Y_SHIP)
        regular_price_s  = safe(row, AW_REGULAR)

        if not status:
            continue

        pipeline[status] += 1

        try:
            score = float(score_s)
        except (ValueError, TypeError):
            score = None

        try:
            cost  = float(cost_s.replace("$", "").replace(",", ""))
            price = float(price_s.replace("$", "").replace(",", ""))
            margin = (price - cost - price * 0.1325) / price if price > 0 else None
        except (ValueError, TypeError):
            cost = price = margin = None

        if score is not None:
            cat = cat_stats[category or "Unknown"]
            cat["count"] += 1
            cat["score_sum"] += score
            cat["score_n"] += 1
            if score >= 7.0:
                cat["t1"] += 1
            if margin is not None:
                cat["margin_sum"] += margin
                cat["margin_n"] += 1

        products.append({
            "status": status, "score": score, "title": title, "category": category,
            "stock": stock, "cost": cost, "price": price, "margin": margin,
            "sold_90d": sold90, "avg_price": avg_s, "comp_count": comp_s,
            "last_checked": checked, "costco_url": url, "summary": summary,
            "sugg_price": sugg_s, "purch_limit": limit_s,
            "sale_val": sale_val, "ship_val": ship_val, "regular_price": regular_price_s,
        })

        if sale_val:
            sale_items.append({"title": title, "sale": sale_val, "stock": stock,
                                "score": score, "url": url, "cost": cost,
                                "price": price, "margin": margin,
                                "regular_price": regular_price_s})
        if ship_val:
            ship_items.append({"title": title, "ship": ship_val, "score": score})

        if status == "PENDING" and score is not None:
            top_pending.append({"score": score, "title": title, "url": url,
                                 "summary": summary})

    top_pending.sort(key=lambda x: -x["score"])
    return pipeline, products, sale_items, ship_items, cat_stats, top_pending


def load_run_history():
    """Read last few entries from run_history.json."""
    try:
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "run_history.json")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data[-20:]
        return []
    except Exception:
        return []


# ── Header ────────────────────────────────────────────────────────────────────

st.markdown("""
<div style="display:flex; align-items:baseline; gap:16px; margin-bottom:0.5rem;">
  <h1 style="margin:0;">JA_LIQUIDATIONS</h1>
  <span style="font-family:'Barlow Condensed',sans-serif; font-size:1.1rem;
               color:#6B7280; letter-spacing:0.12em; text-transform:uppercase;">
    WAT Seller Intelligence
  </span>
</div>
""", unsafe_allow_html=True)

# Refresh button
col_hdr_a, col_hdr_b = st.columns([6, 1])
with col_hdr_b:
    if st.button("⟳  REFRESH"):
        st.cache_data.clear()

# ── Load data ─────────────────────────────────────────────────────────────────

with st.spinner("Loading sheet data..."):
    rows, sheet_name = load_sheet_data()

graveyard   = load_graveyard_data()
audit_log   = load_audit_log()
last_audit  = audit_log[-1] if audit_log else {}

if not rows:
    st.error(f"Could not load sheet data: {sheet_name}")
    st.stop()

pipeline, products, sale_items, ship_items, cat_stats, top_pending = parse_rows(rows)

total = sum(pipeline.values())
n_active  = pipeline.get("ACTIVE", 0)
n_ready   = pipeline.get("READY", 0)
n_pending = pipeline.get("PENDING", 0)
n_watch   = pipeline.get("WATCH", 0)
n_approved = pipeline.get("APPROVED", 0)
n_paused  = sum(v for k, v in pipeline.items() if k.startswith("PAUSED"))

# ── Pipeline pill bar ─────────────────────────────────────────────────────────

st.markdown("<hr class='sec-divider'>", unsafe_allow_html=True)

pills = ""
pill_map = [
    ("PENDING",  n_pending,  "pending"),
    ("APPROVED", n_approved, "approved"),
    ("READY",    n_ready,    "ready"),
    ("ACTIVE",   n_active,   "active"),
    ("WATCH",    n_watch,    "watch"),
    ("PAUSED",   n_paused,   "paused"),
]
for label, count, cls in pill_map:
    pills += f'<span class="pill pill-{cls}">{label} &nbsp;<b>{count}</b></span>'

st.markdown(
    f'<div style="margin:8px 0 16px;">{pills}'
    f'<span style="float:right; font-family:\'Barlow Condensed\',sans-serif; '
    f'font-size:0.85rem; color:#6B7280; letter-spacing:0.08em;">'
    f'TOTAL TRACKED: {total}</span></div>',
    unsafe_allow_html=True
)

# ── KPI row ───────────────────────────────────────────────────────────────────

kpi1, kpi2, kpi3, kpi4, kpi5, kpi6 = st.columns(6)

tier1_count = sum(1 for p in products if p["score"] and p["score"] >= 7.0)
avg_score   = (
    sum(p["score"] for p in products if p["score"] is not None) /
    max(1, sum(1 for p in products if p["score"] is not None))
)
avg_margin_all = [p["margin"] for p in products if p["margin"] is not None]
avg_margin = sum(avg_margin_all) / len(avg_margin_all) if avg_margin_all else 0

kpi1.metric("Tier 1 Products",    tier1_count)
kpi2.metric("Avg Score",          f"{avg_score:.1f}")
kpi3.metric("On Sale 🔥",         len(sale_items))
kpi4.metric("Free Ship 📦",       len(ship_items))
kpi5.metric("Avg Margin",         f"{avg_margin * 100:.1f}%")
kpi6.metric("Live on eBay",       n_active)

# ── Sheet Health Bar ──────────────────────────────────────────────────────────

cat_health = {}
for cat_name in cat_stats:
    cat_prods = [p for p in products if p["category"] == cat_name]
    if not cat_prods:
        continue
    viable_n = sum(
        1 for p in cat_prods
        if p["margin"] is not None and p["price"] is not None
        and (p["price"] * p["margin"]) >= 1.00
        and _safe_float(p["sold_90d"]) > 0
    )
    viable_pct = viable_n / len(cat_prods)
    net_vals = [p["price"] * p["margin"] for p in cat_prods
                if p["margin"] is not None and p["price"] is not None]
    avg_net  = sum(net_vals) / len(net_vals) if net_vals else 0
    avg_vel  = sum(_safe_float(p["sold_90d"]) for p in cat_prods) / len(cat_prods)
    net_score = min(avg_net / 5.0, 1.0)
    vel_score = min(avg_vel / 20.0, 1.0)
    cat_health[cat_name] = max(0, int(viable_pct * 50 + net_score * 25 + vel_score * 25))

overall_health = int(sum(cat_health.values()) / len(cat_health)) if cat_health else 0


def _health_color(score):
    if score >= 60:
        return "#39D353"
    if score >= 30:
        return "#F5A623"
    return "#FF4444"


def _health_bar(score, width=120):
    filled = int(score / 100 * width)
    color  = _health_color(score)
    return (
        f'<div style="display:inline-block;width:{width}px;height:8px;'
        f'background:#1E2D45;border-radius:4px;vertical-align:middle;">'
        f'<div style="width:{filled}px;height:8px;background:{color};'
        f'border-radius:4px;"></div></div>'
    )


last_audit_date  = last_audit.get("DATE", "Never")
auto_removed     = last_audit.get("AUTO_REMOVED", "—")
flagged_review   = last_audit.get("FLAGGED_REVIEW", "—")
lifetime_removed = len(graveyard)

cat_bars_html = "".join(
    f'<div style="text-align:center;">'
    f'<div style="font-size:0.62rem;color:#6B7280;text-transform:uppercase;'
    f'letter-spacing:0.1em;margin-bottom:2px;">{cat}</div>'
    f'<div style="display:flex;align-items:center;gap:6px;">'
    f'{_health_bar(score, 60)}'
    f'<span style="font-size:0.82rem;color:{_health_color(score)};">{score}</span>'
    f'{"<span style=\'color:#FF4444;font-size:0.7rem;\'> ⚠</span>" if score < 30 else ""}'
    f'</div>'
    f'</div>'
    for cat, score in sorted(cat_health.items())
)

st.markdown(
    f'<div style="background:#111827;border:1px solid #1E2D45;border-radius:8px;'
    f'padding:16px 20px;margin-bottom:16px;">'
    f'<div style="display:flex;align-items:center;gap:24px;flex-wrap:wrap;">'
    f'<div>'
    f'<div style="font-size:0.65rem;color:#6B7280;text-transform:uppercase;'
    f'letter-spacing:0.12em;margin-bottom:4px;">Sheet Health</div>'
    f'<div style="display:flex;align-items:center;gap:10px;">'
    f'{_health_bar(overall_health, 160)}'
    f'<span style="font-family:\'JetBrains Mono\',monospace;font-size:1.1rem;'
    f'color:{_health_color(overall_health)};font-weight:600;">{overall_health}/100</span>'
    f'</div>'
    f'</div>'
    f'{cat_bars_html}'
    f'<div style="margin-left:auto;text-align:right;">'
    f'<div style="font-size:0.65rem;color:#6B7280;text-transform:uppercase;'
    f'letter-spacing:0.1em;">Last Audit</div>'
    f'<div style="font-size:0.8rem;color:#E5E7EB;">{last_audit_date}</div>'
    f'<div style="font-size:0.72rem;color:#6B7280;">'
    f'{auto_removed} removed &nbsp;|&nbsp; {flagged_review} flagged &nbsp;|&nbsp; '
    f'{lifetime_removed} in graveyard</div>'
    f'</div>'
    f'</div>'
    f'</div>',
    unsafe_allow_html=True
)

st.markdown("<hr class='sec-divider'>", unsafe_allow_html=True)

# ── Main layout: left = opportunity table, right = alerts + focus ─────────────

left_col, right_col = st.columns([3, 2], gap="large")

with left_col:
    # ── Opportunity table ──────────────────────────────────────────────────────
    st.markdown("## Opportunity Queue")
    st.markdown("<h3>PENDING — sorted by score</h3>", unsafe_allow_html=True)

    import pandas as pd

    scored_rows = [p for p in products if p["status"] == "SCORED"]
    if scored_rows:
        st.markdown("## 🟢 Needs Your Decision")
        st.markdown("<h3>SCORED — Tier 1 products awaiting your APPROVED or PAUSED call</h3>", unsafe_allow_html=True)
        df_scored = pd.DataFrame([{
            "Score": f"{p['score']:.1f}" if p["score"] else "—",
            "Title": p["title"][:55],
            "Category": p["category"],
            "Pricing": _pricing_str(p),
            "Ship": "FREE 📦" if p["ship_val"] else "",
            "Sold 90d": p["sold_90d"] or "—",
        } for p in scored_rows])
        st.dataframe(df_scored, use_container_width=True, height=min(200, 60 + len(scored_rows) * 35), hide_index=True)
        sheet_url = f"https://docs.google.com/spreadsheets/d/{os.getenv('GOOGLE_SHEET_ID', '')}/edit"
        st.markdown(
            f'<a href="{sheet_url}" target="_blank" style="font-size:0.8rem;color:#4A9EFF;">'
            f'→ Open Sheet — change status to APPROVED or PAUSED_DEMAND</a>',
            unsafe_allow_html=True
        )

    ready_rows = [p for p in products if p["status"] == "READY"]
    if ready_rows:
        today_d = _date.today()
        st.markdown("## 📤 Ready to List")
        st.markdown("<h3>READY — export CSV and list on eBay</h3>", unsafe_allow_html=True)
        df_ready = pd.DataFrame([{
            "Title": p["title"][:55],
            "Category": p["category"],
            "Pricing": _pricing_str(p),
            "Ship": "FREE 📦" if p["ship_val"] else "",
            "Sold 90d": p["sold_90d"] or "—",
            "Note": _ready_note(p, today_d),
        } for p in ready_rows])
        st.dataframe(df_ready, use_container_width=True, height=min(200, 60 + len(ready_rows) * 35), hide_index=True)

    pending_rows = [p for p in products if p["status"] == "PENDING" and p["score"] is not None]
    pending_rows.sort(key=lambda x: -x["score"])

    if pending_rows:
        df_pending = pd.DataFrame([{
            "Score": f"{p['score']:.1f}",
            "Tier":  ("T1 🟢" if p["score"] >= 7 else ("T2 🟡" if p["score"] >= 4 else "T3 🔴")),
            "Title": p["title"][:55],
            "Category": p["category"],
            "Cost":  f"${p['cost']:,.0f}" if p["cost"] else "—",
            "eBay":  f"${p['price']:,.0f}" if p["price"] else "—",
            "Margin": f"{p['margin']*100:.1f}%" if p["margin"] else "—",
            "Sold 90d": p["sold_90d"] or "—",
            "SALE": "🔥" if p["sale_val"] else "",
            "SHIP": "📦" if p["ship_val"] else "",
        } for p in pending_rows])

        st.dataframe(df_pending, use_container_width=True, height=380, hide_index=True)
    else:
        st.markdown('<div style="color:#6B7280; font-size:0.85rem;">No PENDING products in queue.</div>',
                    unsafe_allow_html=True)

    # ── All products table (collapsed) ────────────────────────────────────────
    with st.expander("Full product table (all statuses)"):
        all_rows = [p for p in products if p["score"] is not None]
        all_rows.sort(key=lambda x: -(x["score"] or 0))
        df_all = pd.DataFrame([{
            "Status": p["status"],
            "Score": f"{p['score']:.1f}" if p["score"] else "—",
            "Title": p["title"][:50],
            "Category": p["category"],
            "Pricing": _pricing_str(p),
            "Ship": "FREE 📦" if p["ship_val"] else "",
            "Stock": p["stock"][:20],
            "SALE": "🔥" if p["sale_val"] else "",
        } for p in all_rows])
        st.dataframe(df_all, use_container_width=True, height=400, hide_index=True)

with right_col:
    # ── Sale alerts ──────────────────────────────────────────────────────────
    st.markdown("## Sale Opportunities")
    if sale_items:
        today = _date.today()
        shown = 0
        for item in sale_items:
            if shown >= 8:
                break
            expiry = _parse_sale_expiry(item["sale"])
            days_since_expiry = (today - expiry).days if expiry else None
            if expiry and expiry < today and days_since_expiry is not None and days_since_expiry > 3:
                continue
            score_str = f"Score {item['score']:.1f}" if item["score"] else ""
            regular_price = item.get("regular_price", "")
            if expiry is None or expiry >= today:
                sale_cost   = item.get("cost")
                sale_price  = item.get("price")
                sale_margin = item.get("margin")
                card_price_parts = []
                if sale_cost is not None:
                    after_part = ""
                    if regular_price:
                        try:
                            reg = float(str(regular_price).replace("$", "").replace(",", ""))
                            after_part = f" → ${reg:,.2f} after"
                        except (ValueError, TypeError):
                            pass
                    card_price_parts.append(f"${sale_cost:,.2f} sale{after_part}")
                if sale_price is not None:
                    card_price_parts.append(f"eBay ${sale_price:,.2f}")
                if sale_price is not None and sale_margin is not None:
                    net = sale_price * sale_margin
                    card_price_parts.append(f"Net ${net:,.2f} ({sale_margin*100:.0f}%)")
                card_price_line = ""
                if card_price_parts:
                    card_price_line = (
                        f'<div style="font-size:0.75rem;color:#39D353;margin:3px 0;">'
                        f'{" &nbsp;|&nbsp; ".join(card_price_parts)}</div>'
                    )
                st.markdown(
                    f'<div class="alert-card sale">'
                    f'<b style="color:#F5A623;">{item["sale"]}</b> &nbsp;'
                    f'<span style="color:#E5E7EB;">{item["title"][:50]}</span><br>'
                    f'{card_price_line}'
                    f'<span style="color:#6B7280; font-size:0.75rem;">{item["stock"]} &nbsp;|&nbsp; {score_str}</span>'
                    f'</div>',
                    unsafe_allow_html=True
                )
            else:
                if regular_price:
                    try:
                        reg = float(str(regular_price).replace("$", "").replace(",", ""))
                        reprice_note = f"reprice to ${reg:,.2f}"
                    except (ValueError, TypeError):
                        reprice_note = "check Costco"
                else:
                    reprice_note = "check Costco"
                st.markdown(
                    f'<div class="alert-card" style="background:#1A1A1A;border-left-color:#888;">'
                    f'<b style="color:#888;">⚠️ Sale ended {expiry}</b> &nbsp;'
                    f'<span style="color:#9CA3AF;">{item["title"][:50]}</span><br>'
                    f'<span style="color:#6B7280; font-size:0.75rem;">{reprice_note}</span>'
                    f'</div>',
                    unsafe_allow_html=True
                )
            shown += 1
        if shown == 0:
            st.markdown('<div style="color:#6B7280; font-size:0.85rem; margin:4px 0 12px;">No active sales detected.</div>',
                        unsafe_allow_html=True)
    else:
        st.markdown('<div style="color:#6B7280; font-size:0.85rem; margin:4px 0 12px;">No active sales detected.</div>',
                    unsafe_allow_html=True)

    # ── Free ship alerts ──────────────────────────────────────────────────────
    if ship_items:
        st.markdown("## Free Shipping")
        for item in ship_items[:5]:
            score_str = f"Score {item['score']:.1f}" if item["score"] else ""
            st.markdown(
                f'<div class="alert-card ship">'
                f'<b style="color:#2DD4BF;">📦 FREE SHIP</b> &nbsp;'
                f'<span style="color:#E5E7EB;">{item["title"][:50]}</span><br>'
                f'<span style="color:#6B7280; font-size:0.75rem;">{score_str}</span>'
                f'</div>',
                unsafe_allow_html=True
            )

    # ── AUDIT_REVIEW queue ────────────────────────────────────────────────────
    audit_review_rows = [
        r for r in rows
        if safe(r, A_STATUS, "").upper() == "AUDIT_REVIEW"
    ]
    if audit_review_rows:
        sheet_url = (
            f"https://docs.google.com/spreadsheets/d/"
            f"{os.getenv('GOOGLE_SHEET_ID', '')}/edit"
        )
        st.markdown("## ⚠️ Needs Your Decision")
        for r in audit_review_rows[:5]:
            title  = safe(r, C_TITLE, "Unknown")[:52]
            net    = _safe_float(safe(r, I_PROFIT, "0"))
            vel    = safe(r, K_SOLD90, "0")
            notes  = safe(r, AV_NOTES, "")
            reason = ""
            if "AUDIT:" in notes:
                reason = notes.split("AUDIT:")[-1].strip()[:80]
            reason_html = (
                f'<div style="font-size:0.72rem;color:#FF9500;margin-top:3px;">'
                f'{reason}</div>'
                if reason else ""
            )
            st.markdown(
                f'<div class="alert-card urgent" style="border-left-color:#FF6D00;">'
                f'<div style="font-size:0.68rem;color:#FF6D00;text-transform:uppercase;'
                f'letter-spacing:0.1em;margin-bottom:3px;">Audit Review</div>'
                f'<div style="font-size:0.88rem;color:#F9FAFB;margin-bottom:2px;">{title}</div>'
                f'<div style="font-size:0.75rem;color:#6B7280;">'
                f'Net ${net:.2f} &nbsp;|&nbsp; {vel} sold/90d</div>'
                f'{reason_html}'
                f'<div style="margin-top:6px;">'
                f'<a href="{sheet_url}" target="_blank" style="font-size:0.72rem;'
                f'color:#4A9EFF;text-decoration:none;">'
                f'→ Open Sheet to decide KEEP or DELETE</a>'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True
            )

    # ── Focus recommendation ──────────────────────────────────────────────────
    st.markdown("## Where to Focus")
    if top_pending:
        best = top_pending[0]
        st.markdown(
            f'<div class="alert-card" style="border-left-color:#39D353;">'
            f'<div style="font-family:\'Barlow Condensed\',sans-serif; font-size:0.7rem; '
            f'color:#6B7280; text-transform:uppercase; letter-spacing:0.1em;">Top PENDING → APPROVE</div>'
            f'<div style="font-size:0.92rem; margin:4px 0; color:#F9FAFB;">{best["title"][:60]}</div>'
            f'<div style="font-family:\'JetBrains Mono\',monospace; font-size:0.78rem; color:#39D353;">'
            f'Score {best["score"]:.1f}</div>'
            f'</div>',
            unsafe_allow_html=True
        )
    if n_ready > 0:
        st.markdown(
            f'<div class="alert-card urgent">'
            f'<b style="color:#FF4444;">ACTION:</b> {n_ready} product(s) READY — '
            f'export CSV and list on eBay</div>',
            unsafe_allow_html=True
        )

st.markdown("<hr class='sec-divider'>", unsafe_allow_html=True)

# ── Category ROI ──────────────────────────────────────────────────────────────

st.markdown("## Category ROI Breakdown")

cat_table = []
for cat_name, d in sorted(cat_stats.items()):
    avg_score_c  = d["score_sum"] / d["score_n"] if d["score_n"] else 0
    avg_margin_c = d["margin_sum"] / d["margin_n"] * 100 if d["margin_n"] else 0
    cat_table.append({
        "Category": cat_name,
        "Products": d["count"],
        "Avg Score": f"{avg_score_c:.1f}",
        "T1 Count": d["t1"],
        "Avg Margin": f"{avg_margin_c:.1f}%",
        "margin_raw": avg_margin_c,
    })
cat_table.sort(key=lambda x: -x["margin_raw"])

if cat_table:
    cat1, cat2 = st.columns([2, 3])
    with cat1:
        df_cat = pd.DataFrame([{k: v for k, v in r.items() if k != "margin_raw"} for r in cat_table])
        st.dataframe(df_cat, use_container_width=True, hide_index=True)
    with cat2:
        chart_data = pd.DataFrame({
            "Category": [r["Category"] for r in cat_table],
            "Avg Margin %": [r["margin_raw"] for r in cat_table],
        }).set_index("Category")
        st.bar_chart(chart_data, color="#F5A623")

# ── Graveyard Insights ───────────────────────────────────────────────────────

if graveyard:
    from collections import Counter
    st.markdown("<hr class='sec-divider'>", unsafe_allow_html=True)
    st.markdown("## 🪦 Graveyard Insights")

    g_cols = st.columns([2, 2, 2, 1])

    reasons     = Counter()
    cat_removed = Counter()
    for g in graveyard:
        raw = g.get("REASON", "Unknown")
        if "Negative"   in raw:          reasons["Negative net"]   += 1
        elif "floor"    in raw:          reasons["Below floor"]    += 1
        elif "velocity" in raw:          reasons["Zero velocity"]  += 1
        elif "OOS"      in raw:          reasons["OOS 45+ days"]   += 1
        elif "Stale"    in raw:          reasons["Stale 60+ days"] += 1
        elif "wrong"    in raw.lower():  reasons["Bad comps"]      += 1
        else:                            reasons["Other"]           += 1
        cat_removed[g.get("CATEGORY", "Unknown")] += 1

    with g_cols[0]:
        st.markdown("**Removal Reasons**")
        max_r = max(reasons.values()) if reasons else 1
        for reason, count in reasons.most_common():
            bar_w = int(count / max_r * 100)
            st.markdown(
                f'<div style="margin-bottom:6px;">'
                f'<div style="font-size:0.75rem;color:#E5E7EB;margin-bottom:2px;">'
                f'{reason} <span style="color:#6B7280;">({count})</span></div>'
                f'<div style="height:4px;background:#1E2D45;border-radius:2px;">'
                f'<div style="width:{bar_w}%;height:4px;background:#FF4444;'
                f'border-radius:2px;"></div>'
                f'</div></div>',
                unsafe_allow_html=True
            )

    with g_cols[1]:
        st.markdown("**Removed by Category**")
        for cat, count in cat_removed.most_common():
            flag = " ⛔" if count >= 3 else ""
            st.markdown(
                f'<div style="font-size:0.78rem;margin-bottom:4px;">'
                f'<span style="color:#E5E7EB;">{cat}</span>'
                f'<span style="color:#FF4444;margin-left:8px;">×{count}</span>'
                f'<span style="color:#FF4444;">{flag}</span>'
                f'</div>',
                unsafe_allow_html=True
            )
        if any(v >= 3 for v in cat_removed.values()):
            st.markdown(
                '<div style="font-size:0.68rem;color:#FF6D00;margin-top:8px;">'
                '⛔ = removed 3+ times. Stop researching this category.</div>',
                unsafe_allow_html=True
            )

    with g_cols[2]:
        st.markdown("**Recent Removals**")
        for g in sorted(graveyard, key=lambda x: x.get("DATE_REMOVED", ""), reverse=True)[:5]:
            title = g.get("TITLE", "Unknown")[:38]
            date  = g.get("DATE_REMOVED", "")[:10]
            net   = g.get("NET_PROFIT", "?")
            st.markdown(
                f'<div style="font-size:0.73rem;color:#6B7280;margin-bottom:5px;">'
                f'<span style="color:#E5E7EB;">{title}</span><br>'
                f'<span>{date} &nbsp;·&nbsp; net ${net}</span>'
                f'</div>',
                unsafe_allow_html=True
            )

    with g_cols[3]:
        st.markdown("**Stats**")
        st.metric("Total Removed", len(graveyard))
        st.metric("Categories Culled", len(cat_removed))
        most_recent = sorted(graveyard, key=lambda x: x.get("DATE_REMOVED", ""), reverse=True)[0]
        st.markdown(
            f'<div style="font-size:0.7rem;color:#6B7280;margin-top:4px;">'
            f'Last: {most_recent.get("DATE_REMOVED", "")[:10]}</div>',
            unsafe_allow_html=True
        )

st.markdown("<hr class='sec-divider'>", unsafe_allow_html=True)

# ── Run history log ───────────────────────────────────────────────────────────

st.markdown("## Recent Run Activity")

run_history = load_run_history()
if run_history:
    log_lines = []
    for entry in reversed(run_history[-20:]):
        ts     = entry.get("started_at", entry.get("timestamp", ""))[:16]
        mode   = entry.get("mode", "?").upper()
        status = entry.get("status", "?")
        notes  = entry.get("notes", "")
        t1     = entry.get("tier1", "")
        t1_str = f" | T1:{t1}" if t1 else ""
        icon   = "✓" if status == "ok" else "✗"
        log_lines.append(f"[{ts}] {icon} {mode:<14} {status}{t1_str}  {notes}"[:110])
    log_html = "<br>".join(log_lines)
    st.markdown(f'<div class="run-log">{log_html}</div>', unsafe_allow_html=True)
else:
    st.markdown('<div style="color:#6B7280; font-size:0.82rem;">No run history yet. '
                'Check the Run Log tab in Google Sheets instead.</div>', unsafe_allow_html=True)

# ── Footer ────────────────────────────────────────────────────────────────────

st.markdown(
    f'<div style="margin-top:2rem; font-family:\'Barlow Condensed\',sans-serif; '
    f'font-size:0.7rem; color:#374151; letter-spacing:0.1em; text-transform:uppercase;">'
    f'JA_LIQUIDATIONS · WAT Framework · Last load: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
    f'</div>',
    unsafe_allow_html=True
)
