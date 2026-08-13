"""
dashboard_20260805.py
=====================
v3 multi-company Streamlit dashboard: annual 10-K data (from
financial_data_all.json, produced by extract_financials_20260717.py) plus
quarterly 10-Q data (from quarterly_data.json, produced by
fetch_quarterly_20260805.py via the SEC EDGAR XBRL API).

Run (finan conda env):
    streamlit run dashboard_20260805.py -- --data financial_data_all.json

New in v3 (over dashboard_20260717.py):
  - Annual / Quarterly frequency toggle; quarterly views for every
    registered company (last 12 quarters)
  - "Add company" form in the sidebar: ticker + investor-relations URL ->
    registers the company and downloads its quarterly data from SEC EDGAR
    (the IR URL is kept as a reference link)
  - Unit caption under every table and chart title
  - Ratio Formula Reference: pick any ratio and see how it is calculated
  - Common-size columns: each income-statement line also shown as % of
    revenue, each balance-sheet line as % of total assets
"""

import argparse
import hmac
import json
import re
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import fetch_annual_20260805 as fa
import fetch_quarterly_20260805 as fq

CUSTOM_RATIOS_FILE = Path(__file__).parent / "custom_ratios.json"
QUARTERLY_FILE = Path(__file__).parent / "quarterly_data.json"

# fixed categorical slot order (validated palette) — color follows the
# company, never its position in a filtered view
COLOR_SLOTS = ["#2a78d6", "#1baf7a", "#eda100", "#008300",
               "#4a3aa7", "#e34948", "#e87ba4", "#eb6834"]
INK = "#0b0b0b"
MUTED = "#898781"
GRID = "#e1e0d9"

UNIT_MONEY = "Unit: USD millions"
UNIT_MONEY_EPS = "Unit: USD millions (except per-share amounts, in USD)"
UNIT_RATIOS = ("Units: % = percentage · × = times · days = days · "
               "Free Cash Flow in USD millions")

# --------------------------------------------------------------------------
# Ratio formula reference (requirement 4)
# --------------------------------------------------------------------------
RATIO_FORMULAS = {
    "Gross Margin": (
        r"\text{Gross Margin} = \frac{\text{Revenue} - \text{Cost of Revenue}}{\text{Revenue}}",
        "Share of revenue left after direct production/service costs. "
        "Higher means more pricing power or lower unit costs."),
    "Operating Margin": (
        r"\text{Operating Margin} = \frac{\text{Operating Income}}{\text{Revenue}}",
        "Profitability of the core business, after operating expenses but "
        "before interest, other income and taxes."),
    "Net Profit Margin": (
        r"\text{Net Profit Margin} = \frac{\text{Net Income}}{\text{Revenue}}",
        "How much of each revenue dollar ends up as bottom-line profit."),
    "ROA": (
        r"\text{ROA} = \frac{\text{Net Income}}{(\text{Total Assets}_{t} + \text{Total Assets}_{t-1})/2}",
        "Return on Assets — profit generated per dollar of assets. Uses the "
        "average of beginning and ending total assets."),
    "ROE": (
        r"\text{ROE} = \frac{\text{Net Income}}{(\text{Equity}_{t} + \text{Equity}_{t-1})/2}",
        "Return on Equity — profit generated per dollar shareholders have "
        "invested. Uses average stockholders' equity."),
    "Current Ratio": (
        r"\text{Current Ratio} = \frac{\text{Total Current Assets}}{\text{Total Current Liabilities}}",
        "Short-term liquidity: ability to cover obligations due within a "
        "year. Above 1× means current assets exceed current liabilities."),
    "Operating CF Ratio": (
        r"\text{Operating CF Ratio} = \frac{\text{Operating Cash Flow}}{\text{Total Current Liabilities}}",
        "Liquidity measured with actual cash generated instead of "
        "balance-sheet assets."),
    "Debt Ratio": (
        r"\text{Debt Ratio} = \frac{\text{Total Liabilities}}{\text{Total Assets}}",
        "Share of assets financed by liabilities. Higher = more leverage."),
    "Debt-to-Equity": (
        r"\text{Debt-to-Equity} = \frac{\text{Total Liabilities}}{\text{Total Stockholders' Equity}}",
        "Leverage relative to shareholders' capital."),
    "Asset Turnover": (
        r"\text{Asset Turnover} = \frac{\text{Revenue}}{(\text{Total Assets}_{t} + \text{Total Assets}_{t-1})/2}",
        "Revenue generated per dollar of assets — efficiency of the asset "
        "base. Uses average total assets."),
    "Receivables Turnover": (
        r"\text{Receivables Turnover} = \frac{\text{Revenue}}{(\text{AR}_{t} + \text{AR}_{t-1})/2}",
        "How many times a year the accounts-receivable balance is "
        "collected. Uses average accounts receivable (AR)."),
    "DSO (days)": (
        r"\text{DSO} = \frac{365}{\text{Receivables Turnover}}",
        "Days Sales Outstanding — average number of days it takes to "
        "collect payment after a sale."),
    "Free Cash Flow ($M)": (
        r"\text{FCF} = \text{Operating Cash Flow} - \text{Capital Expenditures}",
        "Cash left after reinvesting in property and equipment — available "
        "for dividends, buybacks, debt repayment. In USD millions."),
    "FCF Margin": (
        r"\text{FCF Margin} = \frac{\text{Free Cash Flow}}{\text{Revenue}}",
        "Free cash flow generated per revenue dollar."),
    "Cash Flow to Net Income": (
        r"\text{CF/NI} = \frac{\text{Operating Cash Flow}}{\text{Net Income}}",
        "Earnings-quality check: near or above 1× means accounting profit "
        "is backed by real cash."),
    "CapEx Intensity": (
        r"\text{CapEx Intensity} = \frac{\text{Capital Expenditures}}{\text{Revenue}}",
        "Share of revenue reinvested into property and equipment — shows "
        "how capital-hungry the business is."),
    "Dividend Payout Ratio": (
        r"\text{Payout Ratio} = \frac{\text{Dividends Paid}}{\text{Net Income}}",
        "Share of profit returned to shareholders as dividends. N/A for "
        "companies that pay none (e.g. AMZN)."),
}

# income-statement labels whose common-size % would be meaningless
NO_PCT_PATTERNS = ("per share", "eps", "shares", "share amounts")

# base-row synonyms for the common-size columns
REVENUE_BASE_LABELS = ["revenues", "total revenue", "total revenues",
                       "total net sales", "net sales"]
ASSET_BASE_LABELS = ["total assets"]


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------
@st.cache_data
def load_data(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_custom_ratios() -> list:
    if CUSTOM_RATIOS_FILE.exists():
        try:
            return json.loads(CUSTOM_RATIOS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def save_custom_ratios(ratios: list):
    CUSTOM_RATIOS_FILE.write_text(
        json.dumps(ratios, indent=2, ensure_ascii=False), encoding="utf-8")


def norm_label(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().replace("’", "'")).lower()


def lookup_line_item(statements: dict, statement: str, line_item: str, year: str):
    """Find a line item's value: exact (normalized) label match first, then
    substring — so a ratio defined on one company's wording still resolves
    on another company when the label is close enough."""
    df = statements.get(statement)
    if df is None or df.empty or year not in df.columns:
        return None
    labels = df["Line Item"].map(norm_label) if "Line Item" in df.columns else None
    if labels is None:
        return None
    target = norm_label(line_item)
    for mask in (labels == target, labels.str.contains(re.escape(target), regex=True)):
        for idx in df.index[mask]:
            val = df.at[idx, year]
            if val is not None and not pd.isna(val):
                return float(val)
    return None


def compute_custom_ratio(spec: dict, statements: dict, years: list) -> dict:
    """Evaluate one custom-ratio spec for every year. 'average' on a side
    means (current + prior year) / 2 — for balance-sheet denominators."""
    out = {}
    for i, y in enumerate(years):
        prior = years[i - 1] if i > 0 else None

        def side_value(side):
            val = lookup_line_item(statements, side["statement"], side["line_item"], y)
            if val is None:
                return None
            if side.get("average") and prior:
                pv = lookup_line_item(statements, side["statement"], side["line_item"], prior)
                if pv is not None:
                    val = (val + pv) / 2
            return abs(val) if side.get("absolute") else val

        num, den = side_value(spec["numerator"]), side_value(spec["denominator"])
        out[y] = round(num / den, 4) if num is not None and den not in (None, 0) else None
    return out


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------
def fmt(value, kind: str) -> str:
    if value is None:
        return "N/A"
    if kind == "percent":
        return f"{value * 100:.2f}%"
    if kind == "x":
        return f"{value:.2f}×"
    if kind == "days":
        return f"{value:.1f}"
    if kind == "musd":
        return f"{value:,.0f}"
    return f"{value:.4f}"


def delta_str(cur, prev, kind: str):
    if cur is None or prev is None:
        return None
    if kind == "percent":
        return f"{(cur - prev) * 100:+.2f} pp"
    if kind == "musd":
        return f"{cur - prev:+,.0f}"
    return f"{cur - prev:+.2f}"


def fmt_money(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "N/A"
    if isinstance(v, float) and abs(v) < 100 and v != int(v):
        return f"{v:,.2f}"   # per-share amounts and other small decimals
    return f"{v:,.0f}"


def unit_caption(text: str):
    """Requirement 3: unit note directly below every table/chart title."""
    st.caption(f"*{text}*")


def pct_of(value, base):
    if value is None or base in (None, 0) or (
            isinstance(value, float) and pd.isna(value)) or (
            isinstance(base, float) and pd.isna(base)):
        return ""
    return f"{value / base * 100:.1f}%"


def skip_pct(label: str) -> bool:
    lab = norm_label(label)
    return any(p in lab for p in NO_PCT_PATTERNS)


def find_base_row(df: pd.DataFrame, base_labels: list):
    """Return the row (Series) whose label matches one of base_labels
    (exact normalized match first, then prefix)."""
    labels = df["Line Item"].map(norm_label)
    for target in base_labels:
        exact = df.index[labels == target]
        if len(exact):
            return df.loc[exact[0]]
    for target in base_labels:
        pref = df.index[labels.str.startswith(target)]
        if len(pref):
            return df.loc[pref[0]]
    return None


def add_common_size(df: pd.DataFrame, base_labels: list, base_name: str):
    """Requirement 5: next to every value column add '% of <base>' so each
    account also shows its share of the statement's base (revenue for the
    income statement, total assets for the balance sheet)."""
    if "Line Item" not in df.columns:
        return df, None
    year_cols = [c for c in df.columns if c != "Line Item"]
    base_row = find_base_row(df, base_labels)
    if base_row is None:
        return df, None
    out = pd.DataFrame({"Line Item": df["Line Item"]})
    for y in year_cols:
        out[y] = df[y].map(fmt_money)
        out[f"{y} %"] = [
            "" if skip_pct(lab) else pct_of(v, base_row[y])
            for lab, v in zip(df["Line Item"], df[y])]
    return out, str(base_row["Line Item"])


def plotly_layout(fig, **kwargs):
    fig.update_layout(
        template="plotly_white",
        font=dict(color=INK, family='system-ui, -apple-system, "Segoe UI", sans-serif'),
        xaxis=dict(gridcolor=GRID, linecolor="#c3c2b7"),
        yaxis=dict(gridcolor=GRID, linecolor="#c3c2b7"),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(t=10, b=10),
        **kwargs,
    )
    return fig


# --------------------------------------------------------------------------
# Page setup + data
# --------------------------------------------------------------------------
st.set_page_config(page_title="Financial Dashboard (Annual + Quarterly)",
                   layout="wide")

parser = argparse.ArgumentParser()
parser.add_argument("--data", default="financial_data_all.json")
args, _ = parser.parse_known_args()

data_path = Path(args.data)
if not data_path.exists():
    st.error(
        f"Could not find {data_path}. Run the extractor first, e.g.:\n\n"
        "    python extract_financials_20260717.py input financial_data_all.json statement_csvs"
    )
    st.stop()

data = load_data(str(data_path))
qdata = load_data(str(QUARTERLY_FILE)) if QUARTERLY_FILE.exists() else {"companies": {}}

annual_companies = sorted(data["companies"].keys())
quarterly_companies = sorted(qdata.get("companies", {}).keys())
all_companies = sorted(set(annual_companies) | set(quarterly_companies))
company_colors = {t: COLOR_SLOTS[i % len(COLOR_SLOTS)]
                  for i, t in enumerate(all_companies)}
ratio_categories = data.get("ratio_categories", {})
ratio_formats = data.get("ratio_formats", {})
q_ratio_formats = qdata.get("ratio_formats", fq.QUARTERLY_RATIO_FORMATS)
custom_ratios = load_custom_ratios()


# --------------------------------------------------------------------------
# Admin gate for the features that WRITE files (add company / refresh data /
# custom-ratio builder).
#
# Local runs have no secrets file, so ADMIN_PASSWORD is unset and everything
# stays open — behaviour is unchanged. On a shared deployment (e.g. Streamlit
# Community Cloud) set ADMIN_PASSWORD in the app's secrets: visitors then get
# a read-only dashboard, because those writes hit one filesystem shared by
# every viewer and drive SEC requests that carry your contact email.
# --------------------------------------------------------------------------
def _admin_password():
    try:
        return st.secrets.get("ADMIN_PASSWORD")
    except Exception:
        return None          # no secrets file at all -> local run


def is_admin() -> bool:
    password = _admin_password()
    if not password:
        return True          # unconfigured => open (local development)
    if st.session_state.get("_admin_ok"):
        return True
    entered = st.text_input("Admin password", type="password", key="_admin_pw",
                            help="Unlocks adding companies, refreshing data, "
                                 "and the custom-ratio builder.")
    if entered:
        if hmac.compare_digest(entered, str(password)):
            st.session_state["_admin_ok"] = True
            return True
        st.error("Incorrect password.")
    return False


def company_statements(ticker: str) -> dict:
    return {name: pd.DataFrame(records)
            for name, records in data["companies"][ticker]["statements"].items()}


def company_years(ticker: str) -> list:
    return sorted(data["companies"][ticker]["ratios_by_year"].keys())


# --------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------
with st.sidebar:
    st.header("⚙️ View")
    frequency = st.radio("Data frequency",
                         ["Annual (10-K)", "Quarterly (10-Q)"])
    annual_mode = frequency.startswith("Annual")
    view_mode = st.radio("Mode", ["Single Company", "Compare Companies"])

    pool = annual_companies if annual_mode else quarterly_companies
    if not pool:
        st.error("No companies available for this frequency yet.")
        st.stop()
    ticker = st.selectbox("Company", pool,
                          index=pool.index("GOOG") if "GOOG" in pool else 0)

    if annual_mode:
        fye = data["companies"][ticker]["fiscal_year_end"]
        st.caption(f"Fiscal year end: {fye}")
        ir_url = fq.load_registry().get(ticker, {}).get("ir_url")
        if ir_url:
            st.markdown(f"[🔗 Investor-relations page]({ir_url})")
        st.caption("Sources:")
        for s in data["companies"][ticker]["sources"]:
            # EDGAR sources look like "10-K FY2025 (EDGAR): https://…";
            # PDF-pipeline sources are plain file paths
            if ": http" in s:
                label, url = s.split(": ", 1)
                st.markdown(f"- [{label}]({url})")
            else:
                st.caption(f"• {Path(s).name}")
    else:
        qc = qdata["companies"][ticker]
        st.caption(f"{qc.get('name', ticker)} · CIK {qc.get('cik', '?')}")
        st.caption(f"Fiscal year end: {qc.get('fiscal_year_end', '?')}")
        if qc.get("ir_url"):
            st.markdown(f"[🔗 Investor-relations page]({qc['ir_url']})")
        st.caption(f"Source: {qdata.get('source', 'SEC EDGAR')} · "
                   f"fetched {qdata.get('generated', '?')}")

    # ---- Data-management tools (write to disk -> admin only) -------------
    st.divider()
    admin = is_admin()
    if admin:
        if _admin_password():
            st.caption("🔓 Admin mode. On a hosted deployment these changes "
                       "are temporary — the container's filesystem resets on "
                       "restart.")

        with st.expander("➕ Add a company"):
            st.caption(
                "Give the stock ticker and the company's investor-relations "
                "URL. Annual (10-K) and quarterly (10-Q) financials are both "
                "downloaded from SEC EDGAR (the IR URL is stored as a "
                "reference link).")
            new_ticker = st.text_input("Ticker (US-listed)", key="new_ticker",
                                       placeholder="e.g. AAPL")
            new_url = st.text_input("Investor-relations URL", key="new_url",
                                    placeholder="https://investor.example.com/")
            if st.button("Add & fetch quarterly data", type="primary"):
                t = (new_ticker or "").strip().upper()
                u = (new_url or "").strip()
                if not t or not u:
                    st.error("Both ticker and IR URL are required.")
                else:
                    try:
                        with st.spinner(f"Registering {t} and fetching EDGAR "
                                        "data (annual + quarterly)…"):
                            fq.add_company(t, u)
                            fa.fetch_companies([t], log=lambda *_: None)
                            fq.fetch_companies([t], log=lambda *_: None)
                        load_data.clear()
                        st.success(f"Added {t}. Reloading…")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Could not add {t}: {exc}")

        if quarterly_companies or annual_companies:
            if st.button("🔄 Refresh all EDGAR data (annual + quarterly)"):
                try:
                    with st.spinner("Refreshing from SEC EDGAR…"):
                        fa.fetch_companies(log=lambda *_: None)
                        fq.fetch_companies(log=lambda *_: None)
                    load_data.clear()
                    st.rerun()
                except Exception as exc:
                    st.error(f"Refresh failed: {exc}")

st.title("📊 Financial Statements Dashboard")
st.caption(f"Annual data generated {data.get('generated', '?')} · quarterly "
           f"data fetched {qdata.get('generated', '—')} · all monetary "
           "values in USD millions")


# --------------------------------------------------------------------------
# Ratio Formula Reference (requirement 4) — shown in every view
# --------------------------------------------------------------------------
def render_formula_reference():
    with st.expander("📐 Ratio Formula Reference — how is each ratio calculated?"):
        names = list(ratio_categories.keys()) or list(RATIO_FORMULAS.keys())
        names += [r["name"] for r in custom_ratios]
        chosen = st.selectbox("Choose a ratio", names, key="formula_ratio")
        if chosen in RATIO_FORMULAS:
            latex, desc = RATIO_FORMULAS[chosen]
            st.latex(latex)
            st.markdown(desc)
            cat = ratio_categories.get(chosen)
            if cat:
                st.caption(f"Category: {cat} · Displayed as: "
                           f"{ratio_formats.get(chosen, 'ratio')}")
        else:
            spec = next((r for r in custom_ratios if r["name"] == chosen), None)
            if spec:
                def side_txt(side):
                    txt = side["line_item"]
                    if side.get("average"):
                        txt = f"({txt}_t + {txt}_{{t-1}}) / 2"
                    if side.get("absolute"):
                        txt = f"|{txt}|"
                    return txt
                st.latex(
                    rf"\text{{{chosen}}} = \frac{{\text{{{side_txt(spec['numerator'])}}}}}"
                    rf"{{\text{{{side_txt(spec['denominator'])}}}}}")
                st.markdown(
                    f"Custom ratio: **{spec['numerator']['line_item']}** "
                    f"({spec['numerator']['statement']}) ÷ "
                    f"**{spec['denominator']['line_item']}** "
                    f"({spec['denominator']['statement']}), displayed as "
                    f"{spec.get('format', 'x')}.")


# ==========================================================================
# ANNUAL VIEWS
# ==========================================================================
def annual_single():
    if ticker not in data["companies"]:
        st.info(f"No annual data for {ticker} yet — run "
                f"`python fetch_annual_20260805.py fetch {ticker}` "
                "(or use the Refresh button) to download its 10-K data "
                "from SEC EDGAR.")
        return
    ratios_by_year = data["companies"][ticker]["ratios_by_year"]
    statements = company_statements(ticker)
    years = company_years(ticker)
    latest, prior = years[-1], (years[-2] if len(years) > 1 else None)
    current = ratios_by_year[latest]
    previous = ratios_by_year.get(prior, {}) if prior else {}

    st.subheader(f"{ticker} — Key Ratios (FY{latest})")
    unit_caption(UNIT_RATIOS)
    card_metrics = ["Net Profit Margin", "Current Ratio", "Debt Ratio",
                    "ROE", "Free Cash Flow ($M)"]
    cols = st.columns(len(card_metrics))
    for col, metric in zip(cols, card_metrics):
        kind = ratio_formats.get(metric, "x")
        col.metric(
            metric,
            fmt(current.get(metric), kind),
            delta_str(current.get(metric), previous.get(metric), kind),
            delta_color="inverse" if metric == "Debt Ratio" else "normal",
        )

    render_formula_reference()
    st.divider()

    chart_col1, chart_col2 = st.columns(2)
    with chart_col1:
        st.markdown("#### Revenue & Net Income")
        unit_caption(UNIT_MONEY)
        rev = {y: ratios_by_year[y]["_raw"].get("Revenues") for y in years}
        ni = {y: ratios_by_year[y]["_raw"].get("Net Income") for y in years}
        fig = go.Figure()
        fig.add_bar(name="Revenues", x=years, y=[rev[y] for y in years],
                    marker=dict(color=COLOR_SLOTS[0]))
        fig.add_bar(name="Net Income", x=years, y=[ni[y] for y in years],
                    marker=dict(color=COLOR_SLOTS[1]))
        plotly_layout(fig, barmode="group",
                      yaxis_title="USD (millions)", xaxis_title="Fiscal Year",
                      legend=dict(orientation="h", yanchor="bottom", y=1.02))
        st.plotly_chart(fig, width="stretch")

    with chart_col2:
        st.markdown(f"#### Debt Ratio Gauge (FY{latest})")
        unit_caption("Unit: percent — Total Liabilities ÷ Total Assets")
        debt_ratio = current.get("Debt Ratio") or 0
        prior_debt = previous.get("Debt Ratio") if previous else None
        fig_gauge = go.Figure(go.Indicator(
            mode="gauge+number+delta" if prior_debt else "gauge+number",
            value=debt_ratio * 100,
            number={"suffix": "%"},
            delta={"reference": prior_debt * 100, "valueformat": ".2f"} if prior_debt else None,
            gauge={
                "axis": {"range": [0, 100]},
                "bar": {"color": "#1c5cab"},
                "steps": [
                    {"range": [0, 40], "color": "rgba(12,163,12,0.15)"},
                    {"range": [40, 70], "color": "rgba(250,178,25,0.20)"},
                    {"range": [70, 100], "color": "rgba(208,59,59,0.18)"},
                ],
            },
            title={"text": "Total Liabilities / Total Assets"},
        ))
        fig_gauge.update_layout(margin=dict(t=40, b=10),
                                paper_bgcolor="rgba(0,0,0,0)",
                                font=dict(color=INK))
        st.plotly_chart(fig_gauge, width="stretch")

    st.divider()

    st.subheader("All Financial Ratios by Year")
    unit_caption(UNIT_RATIOS)
    rows = []
    for metric, category in ratio_categories.items():
        kind = ratio_formats.get(metric, "x")
        row = {"Category": category, "Ratio": metric}
        for y in years:
            row[y] = fmt(ratios_by_year[y].get(metric), kind)
        rows.append(row)
    for spec in custom_ratios:
        values = compute_custom_ratio(spec, statements, years)
        row = {"Category": "Custom", "Ratio": spec["name"]}
        for y in years:
            row[y] = fmt(values[y], spec.get("format", "x"))
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                 height=min(38 * (len(rows) + 1), 800))

    # The builder writes custom_ratios.json, a file every viewer shares, so it
    # sits behind the same admin gate as the sidebar's data-management tools.
    # Saved ratios themselves stay visible to everyone in the table above.
    if admin:
        with st.expander("➕ Custom Ratio Builder"):
            st.markdown(
                "Define a new ratio as **numerator ÷ denominator**, picking any "
                "line item from the extracted statements. Saved ratios persist in "
                "`custom_ratios.json` and are computed for every company where the "
                "line items can be matched.")

            # only statements with fiscal-year columns work in year-based ratios
            # (the equity statement is a component matrix, not year columns)
            stmt_options = [s for s, df in statements.items()
                            if "Line Item" in df.columns
                            and any(re.fullmatch(r"\d{4}", str(c)) for c in df.columns)]

            def line_items_of(stmt):
                return list(dict.fromkeys(statements[stmt]["Line Item"].tolist()))

            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**Numerator**")
                num_stmt = st.selectbox("Statement", stmt_options, key="num_stmt")
                num_item = st.selectbox("Line item", line_items_of(num_stmt), key="num_item")
                num_avg = st.checkbox("Average with prior year", key="num_avg",
                                      help="Use (current + prior year) / 2 — for balance-sheet items")
                num_abs = st.checkbox("Use absolute value", key="num_abs",
                                      help="For cash-outflow items reported as negative")
            with c2:
                st.markdown("**Denominator**")
                den_stmt = st.selectbox("Statement", stmt_options, key="den_stmt")
                den_item = st.selectbox("Line item", line_items_of(den_stmt), key="den_item")
                den_avg = st.checkbox("Average with prior year", key="den_avg")
                den_abs = st.checkbox("Use absolute value", key="den_abs")

            c3, c4 = st.columns(2)
            with c3:
                ratio_name = st.text_input("Ratio name", key="ratio_name",
                                           placeholder="e.g. R&D Intensity")
            with c4:
                ratio_format = st.radio("Display as", ["percent", "x"], horizontal=True,
                                        key="ratio_format")

            if st.button("💾 Save custom ratio", type="primary"):
                name = (ratio_name or "").strip()
                if not name:
                    st.error("Please give the ratio a name.")
                elif any(r["name"] == name for r in custom_ratios):
                    st.error(f"A custom ratio named '{name}' already exists.")
                else:
                    custom_ratios.append({
                        "name": name,
                        "numerator": {"statement": num_stmt, "line_item": num_item,
                                      "average": num_avg, "absolute": num_abs},
                        "denominator": {"statement": den_stmt, "line_item": den_item,
                                        "average": den_avg, "absolute": den_abs},
                        "format": ratio_format,
                    })
                    save_custom_ratios(custom_ratios)
                    st.success(f"Saved '{name}'.")
                    st.rerun()

            if custom_ratios:
                st.markdown("**Saved custom ratios**")
                for i, spec in enumerate(custom_ratios):
                    col_a, col_b = st.columns([5, 1])
                    col_a.markdown(
                        f"- **{spec['name']}** = {spec['numerator']['line_item']} ÷ "
                        f"{spec['denominator']['line_item']} ({spec.get('format', 'x')})")
                    if col_b.button("🗑️ Delete", key=f"del_{i}"):
                        custom_ratios.pop(i)
                        save_custom_ratios(custom_ratios)
                        st.rerun()

    st.divider()

    st.subheader("Raw Extracted Financial Statements (merged across filings)")
    unit_caption(UNIT_MONEY_EPS)
    tabs = st.tabs(list(statements.keys()))
    for tab, (name, df) in zip(tabs, statements.items()):
        with tab:
            # requirement 5: common-size % next to each account
            if "Income" in name and "Comprehensive" not in name:
                shown, base = add_common_size(df, REVENUE_BASE_LABELS, None)
                unit_note = UNIT_MONEY_EPS
                base_note = f"'%' columns = share of **{base}** (common-size)" if base else None
            elif "Balance" in name:
                shown, base = add_common_size(df, ASSET_BASE_LABELS, None)
                unit_note = UNIT_MONEY
                base_note = f"'%' columns = share of **{base}** (common-size)" if base else None
            else:
                shown, base_note, unit_note = df, None, UNIT_MONEY
            unit_caption(unit_note)
            if base_note:
                st.caption(base_note)
            st.dataframe(shown, width="stretch", hide_index=True)


def annual_compare():
    st.subheader("Compare Companies — Annual")
    render_formula_reference()

    absolute_metrics = ["Revenues", "Net Income", "Operating Cash Flow", "Total Assets"]
    metric_options = (list(ratio_categories.keys()) + absolute_metrics
                      + [r["name"] for r in custom_ratios])
    metric = st.selectbox("Metric", metric_options)

    if metric in ratio_categories:
        kind = ratio_formats.get(metric, "x")
    elif metric in absolute_metrics:
        kind = "musd"
    else:
        spec = next(r for r in custom_ratios if r["name"] == metric)
        kind = spec.get("format", "x")

    values = {}
    all_years = set()
    for t in annual_companies:
        ry = data["companies"][t]["ratios_by_year"]
        yrs = sorted(ry.keys())
        all_years.update(yrs)
        if metric in ratio_categories:
            values[t] = {y: ry[y].get(metric) for y in yrs}
        elif metric in absolute_metrics:
            values[t] = {y: ry[y]["_raw"].get(metric) for y in yrs}
        else:
            values[t] = compute_custom_ratio(spec, company_statements(t), yrs)
    all_years = sorted(all_years)

    st.markdown(f"#### {metric} by Fiscal Year")
    unit_caption(UNIT_MONEY if kind == "musd" else
                 f"Unit: {'percent' if kind == 'percent' else 'ratio (×)' if kind == 'x' else 'days'}")
    fig = go.Figure()
    for t in annual_companies:
        fig.add_bar(name=t, x=all_years,
                    y=[values[t].get(y) for y in all_years],
                    marker=dict(color=company_colors[t]))
    y_title = "USD (millions)" if kind == "musd" else metric
    plotly_layout(fig, barmode="group", xaxis_title="Fiscal Year", yaxis_title=y_title,
                  legend=dict(orientation="h", yanchor="bottom", y=1.02))
    if kind == "percent":
        fig.update_yaxes(tickformat=".0%")
    st.plotly_chart(fig, width="stretch")
    non_dec = [f"{t} ({data['companies'][t]['fiscal_year_end']})"
               for t in annual_companies
               if not str(data['companies'][t].get('fiscal_year_end', '')
                          ).startswith("December")]
    if non_dec:
        st.caption("⚠️ Fiscal years are not perfectly aligned — non-December "
                   "fiscal year ends: " + ", ".join(non_dec) + ".")

    rows = []
    for t in annual_companies:
        row = {"Company": t}
        for y in all_years:
            row[y] = fmt(values[t].get(y), kind)
        rows.append(row)
    unit_caption(UNIT_MONEY if kind == "musd" else "Same unit as the chart above")
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


# ==========================================================================
# QUARTERLY VIEWS
# ==========================================================================
def q_statement_table(quarters: list, section: str, base_item: str = None):
    """Line items as rows, quarters as columns; when base_item is given,
    a '% ' column follows every quarter column (requirement 5)."""
    if not quarters:
        return pd.DataFrame()
    items = list(quarters[0][section].keys())
    out = {"Line Item": items}
    for q in quarters:
        col = q["label"]
        vals = [q[section].get(it) for it in items]
        base = q[section].get(base_item) if base_item else None
        out[col] = [f"{v:,.2f}" if it == "EPS (Diluted)" and v is not None
                    else fmt_money(v)
                    for it, v in zip(items, vals)]
        if base_item:
            out[f"{col} %"] = ["" if skip_pct(it) else pct_of(v, base)
                               for it, v in zip(items, vals)]
    return pd.DataFrame(out)


def quarterly_single():
    qc = qdata["companies"][ticker]
    quarters = qc["quarters"]
    if not quarters:
        st.info(f"No quarterly data for {ticker}.")
        return
    latest = quarters[-1]
    prior = quarters[-2] if len(quarters) > 1 else None

    st.subheader(f"{ticker} — Latest Quarter: {latest['label']} "
                 f"(ended {latest['end']})")
    unit_caption(UNIT_RATIOS + " · deltas vs prior quarter")
    cards = [
        ("Revenues", latest["income"].get("Revenues"),
         prior["income"].get("Revenues") if prior else None, "musd"),
        ("Net Income", latest["income"].get("Net Income"),
         prior["income"].get("Net Income") if prior else None, "musd"),
        ("Net Profit Margin", latest["ratios"].get("Net Profit Margin"),
         prior["ratios"].get("Net Profit Margin") if prior else None, "percent"),
        ("Current Ratio", latest["ratios"].get("Current Ratio"),
         prior["ratios"].get("Current Ratio") if prior else None, "x"),
        ("Free Cash Flow ($M)", latest["ratios"].get("Free Cash Flow ($M)"),
         prior["ratios"].get("Free Cash Flow ($M)") if prior else None, "musd"),
    ]
    cols = st.columns(len(cards))
    for col, (name, cur, prev, kind) in zip(cols, cards):
        col.metric(name, fmt(cur, kind), delta_str(cur, prev, kind))

    render_formula_reference()
    st.divider()

    labels = [q["label"] for q in quarters]
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### Quarterly Revenue & Net Income")
        unit_caption(UNIT_MONEY)
        fig = go.Figure()
        fig.add_bar(name="Revenues", x=labels,
                    y=[q["income"].get("Revenues") for q in quarters],
                    marker=dict(color=COLOR_SLOTS[0]))
        fig.add_bar(name="Net Income", x=labels,
                    y=[q["income"].get("Net Income") for q in quarters],
                    marker=dict(color=COLOR_SLOTS[1]))
        plotly_layout(fig, barmode="group", yaxis_title="USD (millions)",
                      xaxis_title="Fiscal Quarter",
                      legend=dict(orientation="h", yanchor="bottom", y=1.02))
        st.plotly_chart(fig, width="stretch")

    with c2:
        st.markdown("#### Margin Trend")
        unit_caption("Unit: percent")
        fig = go.Figure()
        for i, m in enumerate(["Gross Margin", "Operating Margin", "Net Profit Margin"]):
            fig.add_scatter(name=m, x=labels, mode="lines+markers",
                            y=[q["ratios"].get(m) for q in quarters],
                            line=dict(color=COLOR_SLOTS[i]))
        plotly_layout(fig, xaxis_title="Fiscal Quarter", yaxis_title="Margin",
                      legend=dict(orientation="h", yanchor="bottom", y=1.02))
        fig.update_yaxes(tickformat=".0%")
        st.plotly_chart(fig, width="stretch")

    st.divider()

    st.subheader("Quarterly Ratios")
    unit_caption(UNIT_RATIOS)
    rows = []
    for name, kind in q_ratio_formats.items():
        row = {"Ratio": name}
        for q in quarters:
            row[q["label"]] = fmt(q["ratios"].get(name), kind)
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    st.subheader("Quarterly Financial Statements")
    st.caption("Q4 income/cash-flow figures are derived: full-year 10-K "
               "value minus Q1–Q3 (no 10-Q is filed for Q4). Quarterly EPS "
               "differences are approximate when share counts move.")
    tab_is, tab_bs, tab_cf = st.tabs(
        ["Income Statement", "Balance Sheet", "Cash Flow"])
    with tab_is:
        unit_caption(UNIT_MONEY_EPS)
        st.caption("'%' columns = share of **Revenues** (common-size)")
        st.dataframe(q_statement_table(quarters, "income", "Revenues"),
                     width="stretch", hide_index=True)
    with tab_bs:
        unit_caption(UNIT_MONEY)
        st.caption("'%' columns = share of **Total Assets** (common-size)")
        st.dataframe(q_statement_table(quarters, "balance", "Total Assets"),
                     width="stretch", hide_index=True)
    with tab_cf:
        unit_caption(UNIT_MONEY)
        st.dataframe(q_statement_table(quarters, "cashflow"),
                     width="stretch", hide_index=True)


def quarterly_compare():
    st.subheader("Compare Companies — Quarterly")
    render_formula_reference()

    absolute_metrics = ["Revenues", "Net Income", "Operating Cash Flow",
                        "Total Assets"]
    metric = st.selectbox("Metric",
                          list(q_ratio_formats.keys()) + absolute_metrics)
    if metric in q_ratio_formats:
        kind = q_ratio_formats[metric]
    else:
        kind = "musd"

    def metric_value(q):
        if metric in q_ratio_formats:
            return q["ratios"].get(metric)
        section = "cashflow" if metric == "Operating Cash Flow" else (
            "balance" if metric == "Total Assets" else "income")
        return q[section].get(metric)

    # align on calendar quarters so Dec-FYE and Jun-FYE companies line up
    values, cal_quarters = {}, set()
    for t in quarterly_companies:
        values[t] = {}
        for q in qdata["companies"][t]["quarters"]:
            cq = q.get("calendar_quarter", q["label"])
            cal_quarters.add(cq)
            values[t][cq] = metric_value(q)
    cal_quarters = sorted(cal_quarters)

    st.markdown(f"#### {metric} by Calendar Quarter")
    unit_caption(UNIT_MONEY if kind == "musd" else
                 f"Unit: {'percent' if kind == 'percent' else 'ratio (×)'}")
    fig = go.Figure()
    for t in quarterly_companies:
        fig.add_bar(name=t, x=cal_quarters,
                    y=[values[t].get(cq) for cq in cal_quarters],
                    marker=dict(color=company_colors[t]))
    plotly_layout(fig, barmode="group", xaxis_title="Calendar Quarter",
                  yaxis_title="USD (millions)" if kind == "musd" else metric,
                  legend=dict(orientation="h", yanchor="bottom", y=1.02))
    if kind == "percent":
        fig.update_yaxes(tickformat=".0%")
    st.plotly_chart(fig, width="stretch")
    non_dec = [f"{t} ({qdata['companies'][t].get('fiscal_year_end', '?')})"
               for t in quarterly_companies
               if qdata['companies'][t].get('fiscal_year_end_month', 12) != 12]
    if non_dec:
        st.caption("⚠️ Aligned by nearest calendar quarter. Companies with "
                   "non-December fiscal year ends label their quarters "
                   "differently: " + ", ".join(non_dec) + ".")

    rows = []
    for t in quarterly_companies:
        row = {"Company": t}
        for cq in cal_quarters:
            row[cq] = fmt(values[t].get(cq), kind)
        rows.append(row)
    unit_caption(UNIT_MONEY if kind == "musd" else "Same unit as the chart above")
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


# ==========================================================================
# Dispatch
# ==========================================================================
# plain if/else statements, not conditional expressions — a bare expression
# on its own line triggers Streamlit's "magic" display and would print the
# functions' None return value at the bottom of the page
single = view_mode == "Single Company"
if annual_mode:
    if single:
        annual_single()
    else:
        annual_compare()
else:
    if single:
        quarterly_single()
    else:
        quarterly_compare()
