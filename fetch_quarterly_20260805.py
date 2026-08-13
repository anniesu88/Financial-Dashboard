"""
fetch_quarterly_20260805.py
===========================
Quarterly financial data fetcher (SEC EDGAR XBRL API) + company registry.

The user registers a company with its ticker and investor-relations URL.
The IR URL is stored as a reference link (shown in the dashboard); the
actual quarterly numbers are downloaded from SEC EDGAR's companyfacts API,
which is reliable for any US-listed ticker and needs no page scraping.

Registry lives in companies.json; output goes to quarterly_data.json,
consumed by dashboard_20260805.py.

Run (finan conda env):
    python fetch_quarterly_20260805.py add META https://investor.atmeta.com/financials/
    python fetch_quarterly_20260805.py remove META
    python fetch_quarterly_20260805.py list
    python fetch_quarterly_20260805.py fetch [TICKER ...] [--quarters 12]

Quarterly derivation rules:
  - Discrete-quarter facts (~3-month duration) are used directly.
  - Cash-flow items are filed year-to-date in 10-Qs -> discrete quarters
    are computed by differencing successive YTD periods.
  - Q4 has no 10-Q -> Q4 = full-year 10-K value minus Q1+Q2+Q3.
  - Same period reported in several filings -> the latest-filed value wins
    (mirrors the v2 annual conflict rule: newer filing wins).
All monetary values are converted to USD millions (EPS stays in USD).
"""

import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
REGISTRY_FILE = BASE_DIR / "companies.json"
DEFAULT_OUT = BASE_DIR / "quarterly_data.json"
DEFAULT_QUARTERS = 12

# SEC requires a descriptive User-Agent with a contact address. Set the
# SEC_CONTACT_EMAIL environment variable before running any fetcher —
# see README.md "Quick Start".
USER_AGENT = (f"CSP Financial Dashboard "
             f"{os.environ.get('SEC_CONTACT_EMAIL', 'your-email@example.com')}")

TICKER_INDEX_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]

# --------------------------------------------------------------------------
# Concept map: dashboard line item -> ordered us-gaap tag candidates.
# Companies tag the same concept differently (and switch tags over time —
# e.g. GOOG moved from RevenueFromContractWithCustomer... back to Revenues
# in 2025), so facts from ALL listed tags are merged, latest filing wins.
# --------------------------------------------------------------------------
INCOME_CONCEPTS = {
    "Revenues": ["RevenueFromContractWithCustomerExcludingAssessedTax",
                 "Revenues", "SalesRevenueNet"],
    "Cost of Revenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold",
                        "CostOfSales"],
    "Gross Profit": ["GrossProfit"],  # missing for GOOG/AMZN -> computed below
    "Research and Development": ["ResearchAndDevelopmentExpense"],
    "Operating Income": ["OperatingIncomeLoss"],
    "Net Income": ["NetIncomeLoss"],
    "EPS (Diluted)": ["EarningsPerShareDiluted"],
}
BALANCE_CONCEPTS = {
    "Cash and Cash Equivalents": ["CashAndCashEquivalentsAtCarryingValue"],
    "Marketable Securities (Current)": ["MarketableSecuritiesCurrent",
                                        "ShortTermInvestments",
                                        "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
    "Accounts Receivable, Net": ["AccountsReceivableNetCurrent"],
    "Total Current Assets": ["AssetsCurrent"],
    "Total Assets": ["Assets"],
    "Total Current Liabilities": ["LiabilitiesCurrent"],
    "Long-term Debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "Total Liabilities": ["Liabilities"],  # AMZN lacks it -> Assets - Equity
    "Total Stockholders' Equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
}
CASHFLOW_CONCEPTS = {
    "Operating Cash Flow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "Capital Expenditures": ["PaymentsToAcquirePropertyPlantAndEquipment",
                             "PaymentsToAcquireProductiveAssets"],
}
# EPS is in USD (not millions); everything else is scaled to USD millions
UNSCALED_ITEMS = {"EPS (Diluted)"}

QUARTERLY_RATIO_FORMATS = {
    "Gross Margin": "percent",
    "Operating Margin": "percent",
    "Net Profit Margin": "percent",
    "Current Ratio": "x",
    "Debt Ratio": "percent",
    "Free Cash Flow ($M)": "musd",
    "FCF Margin": "percent",
}

ACCEPTED_FORMS = {"10-Q", "10-Q/A", "10-K", "10-K/A"}


# --------------------------------------------------------------------------
# HTTP + registry helpers
# --------------------------------------------------------------------------
def http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_registry() -> dict:
    if REGISTRY_FILE.exists():
        return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    return {}


def save_registry(reg: dict):
    REGISTRY_FILE.write_text(json.dumps(reg, indent=2, ensure_ascii=False),
                             encoding="utf-8")


def resolve_cik(ticker: str):
    """Look the ticker up in SEC's official ticker->CIK index."""
    index = http_json(TICKER_INDEX_URL)
    t = ticker.upper()
    for row in index.values():
        if row["ticker"].upper() == t:
            return int(row["cik_str"]), row["title"]
    return None, None


def add_company(ticker: str, ir_url: str) -> dict:
    """Register (or update) a company: ticker + IR reference URL -> CIK."""
    ticker = ticker.upper()
    cik, title = resolve_cik(ticker)
    if cik is None:
        raise ValueError(
            f"Ticker '{ticker}' not found in SEC EDGAR's ticker index — "
            "check the spelling (US-listed tickers only).")
    reg = load_registry()
    reg[ticker] = {"name": title, "cik": cik, "ir_url": ir_url}
    save_registry(reg)
    return reg[ticker]


def remove_company(ticker: str) -> bool:
    reg = load_registry()
    if ticker.upper() in reg:
        del reg[ticker.upper()]
        save_registry(reg)
        return True
    return False


# --------------------------------------------------------------------------
# Fact collection
# --------------------------------------------------------------------------
def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def collect_facts(gaap: dict, tags: list, unit_keys=("USD", "USD/shares")):
    """Merge the fact lists of every candidate tag, keep accepted forms only."""
    facts = []
    for tag in tags:
        node = gaap.get(tag)
        if not node:
            continue
        for unit, rows in node.get("units", {}).items():
            if unit not in unit_keys:
                continue
            for r in rows:
                if r.get("form") in ACCEPTED_FORMS and r.get("val") is not None:
                    facts.append(r)
    return facts


def dedupe_by_period(facts, key_fields):
    """One value per period; when several filings report the same period,
    the latest-filed one wins (restatement rule, same as the annual v2)."""
    best = {}
    for r in facts:
        key = tuple(r.get(k) for k in key_fields)
        if None in key:
            continue
        prev = best.get(key)
        if prev is None or r.get("filed", "") > prev.get("filed", ""):
            best[key] = r
    return {k: v["val"] for k, v in best.items()}


def quarterly_from_durations(facts) -> dict:
    """Turn duration facts into one value per discrete quarter end.

    Pass 1: ~3-month facts used directly.
    Pass 2: YTD differencing (same period start, ends one quarter apart).
    Pass 3: Q4 = full-year value minus the three quarters inside it.
    """
    by_period = dedupe_by_period(facts, ("start", "end"))

    quarters = {}   # end_date -> value
    starts = defaultdict(list)   # period start -> [(end, value)]
    fy_periods = []              # [(start, end, value)] for ~12-month facts

    for (s, e), val in by_period.items():
        ds, de = parse_date(s), parse_date(e)
        days = (de - ds).days
        if 80 <= days <= 100:
            quarters[de] = val
        if days <= 380:
            starts[ds].append((de, val))
        if 350 <= days <= 380:
            fy_periods.append((ds, de, val))

    # Pass 2 — YTD differencing
    for ds, seq in starts.items():
        seq.sort()
        for (e1, v1), (e2, v2) in zip(seq, seq[1:]):
            gap = (e2 - e1).days
            if 80 <= gap <= 100 and e2 not in quarters:
                quarters[e2] = v2 - v1

    # Pass 3 — Q4 = FY − (Q1+Q2+Q3)
    for ds, de, fy_val in fy_periods:
        if de in quarters:
            continue
        inside = [v for e, v in quarters.items() if ds <= e < de]
        if len(inside) == 3:
            quarters[de] = fy_val - sum(inside)

    return quarters


def instants_by_end(facts) -> dict:
    return {parse_date(e): v
            for (e,), v in dedupe_by_period(facts, ("end",)).items()}


def fiscal_year_end_month(gaap: dict) -> int:
    """Most common end month of ~12-month Net Income facts = FYE month."""
    counts = defaultdict(int)
    for r in collect_facts(gaap, ["NetIncomeLoss"]):
        if r.get("start") and r.get("end"):
            days = (parse_date(r["end"]) - parse_date(r["start"])).days
            if 350 <= days <= 380:
                counts[parse_date(r["end"]).month] += 1
    return max(counts, key=counts.get) if counts else 12


def fiscal_label(end: date, fye_month: int):
    """(fiscal_year, quarter_no) for a quarter ending on `end`."""
    fy = end.year + (1 if end.month > fye_month else 0)
    fy_start_month = fye_month % 12 + 1
    q = ((end.month - fy_start_month) % 12) // 3 + 1
    return fy, q


# --------------------------------------------------------------------------
# Per-company build
# --------------------------------------------------------------------------
def scale(name: str, val):
    if val is None:
        return None
    if name in UNSCALED_ITEMS:
        return round(val, 2)
    return round(val / 1e6, 1)


def build_company_quarters(cik: int, n_quarters: int) -> dict:
    facts_doc = http_json(COMPANYFACTS_URL.format(cik=cik))
    gaap = facts_doc.get("facts", {}).get("us-gaap", {})
    if not gaap:
        raise ValueError(f"CIK {cik}: no us-gaap facts on EDGAR")

    fye_month = fiscal_year_end_month(gaap)

    duration_series = {}
    for name, tags in {**INCOME_CONCEPTS, **CASHFLOW_CONCEPTS}.items():
        duration_series[name] = quarterly_from_durations(collect_facts(gaap, tags))
    instant_series = {name: instants_by_end(collect_facts(gaap, tags))
                      for name, tags in BALANCE_CONCEPTS.items()}

    # Quarter calendar = balance-sheet dates (every 10-Q/10-K carries one),
    # restricted to ends where an income figure also resolved.
    ends = sorted(e for e in instant_series["Total Assets"]
                  if e in duration_series["Net Income"])[-n_quarters:]

    quarters = []
    for e in ends:
        income = {n: scale(n, duration_series[n].get(e)) for n in INCOME_CONCEPTS}
        if income.get("Gross Profit") is None and None not in (
                income.get("Revenues"), income.get("Cost of Revenue")):
            income["Gross Profit"] = round(
                income["Revenues"] - income["Cost of Revenue"], 1)

        balance = {n: scale(n, instant_series[n].get(e)) for n in BALANCE_CONCEPTS}
        if balance.get("Total Liabilities") is None and None not in (
                balance.get("Total Assets"), balance.get("Total Stockholders' Equity")):
            balance["Total Liabilities"] = round(
                balance["Total Assets"] - balance["Total Stockholders' Equity"], 1)

        cashflow = {n: scale(n, duration_series[n].get(e)) for n in CASHFLOW_CONCEPTS}
        if None not in (cashflow.get("Operating Cash Flow"),
                        cashflow.get("Capital Expenditures")):
            cashflow["Free Cash Flow"] = round(
                cashflow["Operating Cash Flow"] - cashflow["Capital Expenditures"], 1)
        else:
            cashflow["Free Cash Flow"] = None

        def div(a, b):
            return round(a / b, 4) if a is not None and b not in (None, 0) else None

        ratios = {
            "Gross Margin": div(income.get("Gross Profit"), income.get("Revenues")),
            "Operating Margin": div(income.get("Operating Income"), income.get("Revenues")),
            "Net Profit Margin": div(income.get("Net Income"), income.get("Revenues")),
            "Current Ratio": div(balance.get("Total Current Assets"),
                                 balance.get("Total Current Liabilities")),
            "Debt Ratio": div(balance.get("Total Liabilities"), balance.get("Total Assets")),
            "Free Cash Flow ($M)": cashflow.get("Free Cash Flow"),
            "FCF Margin": div(cashflow.get("Free Cash Flow"), income.get("Revenues")),
        }

        fy, q = fiscal_label(e, fye_month)
        quarters.append({
            "label": f"FY{fy} Q{q}",
            "end": e.isoformat(),
            "fiscal_year": fy,
            "quarter": q,
            "calendar_quarter": f"{e.year} Q{(e.month - 1) // 3 + 1}",
            "income": income,
            "balance": balance,
            "cashflow": cashflow,
            "ratios": ratios,
        })

    return {
        "entity_name": facts_doc.get("entityName"),
        "fiscal_year_end_month": fye_month,
        "fiscal_year_end": MONTH_NAMES[fye_month],
        "quarters": quarters,
    }


def fetch_companies(tickers=None, n_quarters=DEFAULT_QUARTERS,
                    out_path=DEFAULT_OUT, log=print) -> dict:
    """Fetch quarterly data for the given (or all registered) tickers and
    write quarterly_data.json. Returns the written document."""
    reg = load_registry()
    if not reg:
        raise ValueError("companies.json is empty — add a company first "
                         "(fetch_quarterly_20260805.py add TICKER IR_URL)")
    tickers = [t.upper() for t in tickers] if tickers else sorted(reg)
    unknown = [t for t in tickers if t not in reg]
    if unknown:
        raise ValueError(f"Not registered: {', '.join(unknown)}")

    out_path = Path(out_path)
    doc = (json.loads(out_path.read_text(encoding="utf-8"))
           if out_path.exists() else {})
    doc.setdefault("companies", {})
    doc["source"] = "SEC EDGAR XBRL companyfacts API"
    doc["unit"] = "USD millions (EPS in USD)"
    doc["ratio_formats"] = QUARTERLY_RATIO_FORMATS

    for t in tickers:
        entry = reg[t]
        log(f"[{t}] fetching CIK {entry['cik']} ({entry['name']}) ...")
        built = build_company_quarters(entry["cik"], n_quarters)
        built.update({"name": entry["name"], "cik": entry["cik"],
                      "ir_url": entry["ir_url"]})
        doc["companies"][t] = built
        q = built["quarters"]
        log(f"[{t}] {len(q)} quarters: "
            f"{q[0]['label']} ({q[0]['end']}) → {q[-1]['label']} ({q[-1]['end']})"
            if q else f"[{t}] WARNING: no quarters resolved")

    # drop companies no longer in the registry
    for t in list(doc["companies"]):
        if t not in reg:
            del doc["companies"][t]

    doc["generated"] = date.today().isoformat()
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    log(f"Wrote {out_path}")
    return doc


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1].lower()

    if cmd == "add":
        if len(argv) != 4:
            print("Usage: fetch_quarterly_20260805.py add TICKER IR_URL")
            return 1
        entry = add_company(argv[2], argv[3])
        print(f"Registered {argv[2].upper()}: {entry['name']} "
              f"(CIK {entry['cik']})\n  IR page: {entry['ir_url']}\n"
              f"Now run:  python fetch_quarterly_20260805.py fetch {argv[2].upper()}")
        return 0

    if cmd == "remove":
        ok = remove_company(argv[2])
        print(("Removed " if ok else "Not registered: ") + argv[2].upper())
        return 0 if ok else 1

    if cmd == "list":
        reg = load_registry()
        if not reg:
            print("No companies registered.")
        for t, e in sorted(reg.items()):
            print(f"{t:6s} CIK {e['cik']:>10d}  {e['name']}\n"
                  f"       IR: {e['ir_url']}")
        return 0

    if cmd == "fetch":
        rest = argv[2:]
        n = DEFAULT_QUARTERS
        if "--quarters" in rest:
            i = rest.index("--quarters")
            n = int(rest[i + 1])
            rest = rest[:i] + rest[i + 2:]
        fetch_companies(rest or None, n_quarters=n)
        return 0

    print(f"Unknown command: {cmd}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
