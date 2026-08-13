"""
fetch_annual_20260805.py
========================
Annual (10-K) financial-statement fetcher — SEC EDGAR R-file edition (v4).

Replaces PDF parsing as the PRIMARY source of annual data: for every
company in companies.json (same registry as fetch_quarterly_20260805.py),
this script downloads the latest N 10-K filings from SEC EDGAR and rebuilds
the five consolidated financial statements from each filing's own rendered
R-report tables (FilingSummary.xml) — the same tables SEC's "Financial
Report" viewer shows. Statement presentation (line order, as-filed labels,
units) is preserved, and the stockholders' equity statement comes out as a
proper component matrix (an upgrade over the PDF pipeline's raw lines).

Output is schema-identical to the old PDF extractor's financial_data_all.json,
so dashboard_20260805.py needs no data changes. Cross-filing merge and the
17-ratio calculation are REUSED from extract_financials_20260717.py (which
stays in the project as the offline/non-US backup pipeline).

Run (finan conda env):
    python fetch_annual_20260805.py add TICKER IR_URL      (shared registry)
    python fetch_annual_20260805.py remove TICKER
    python fetch_annual_20260805.py list
    python fetch_annual_20260805.py fetch [TICKER ...] [--filings 3]

Notes:
  - ~3 filings ≈ 5 fiscal years after the cross-filing merge (newer filing
    wins on restatements — same CONFLICT rule as the PDF pipeline).
  - US-listed (SEC-filing) tickers only; R files exist for ~2010+ filings.
  - MSFT's fiscal year ends June 30, so its latest filings run one FY ahead
    of December-FYE companies (e.g. FY2026 already filed in 2026).
"""

import json
import re
import sys
from datetime import date
from html import unescape
from pathlib import Path

import pandas as pd

import extract_financials_20260717 as pdfx   # merge + ratios + number cleaning
import fetch_quarterly_20260805 as fq        # registry + HTTP + User-Agent

BASE_DIR = Path(__file__).parent
DEFAULT_OUT = BASE_DIR / "financial_data_all.json"
DEFAULT_CSV_DIR = BASE_DIR / "statement_csvs"
DEFAULT_FILINGS = 3

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}"

# R-report ShortName -> canonical statement. Matching is on norm_title()
# so case/punctuation don't matter; "(Parenthetical)" reports are skipped.
R_STATEMENT_ALIASES = {
    "Consolidated Balance Sheets": [
        "Consolidated Balance Sheets", "Balance Sheets",
        "Consolidated Balance Sheet",
    ],
    "Consolidated Statements of Income": [
        "Consolidated Statements of Income",
        "Consolidated Statements of Operations",      # AMZN
        "Income Statements",                          # MSFT
        "Consolidated Income Statements",
    ],
    "Consolidated Statements of Comprehensive Income": [
        "Consolidated Statements of Comprehensive Income",
        "Consolidated Statements of Comprehensive Income (Loss)",
        "Comprehensive Income Statements",            # MSFT
        "Consolidated Comprehensive Income Statements",
    ],
    "Consolidated Statements of Stockholders’ Equity": [
        "Consolidated Statements of Stockholders' Equity",
        "Consolidated Statements of Stockholders' Equity (Deficit)",  # ORCL
        "Consolidated Statements of Stockholders Equity",
        "Consolidated Statements of Shareholders' Equity",
        "Consolidated Statements of Equity",
        "Stockholders' Equity Statements",            # MSFT
    ],
    "Consolidated Statements of Cash Flows": [
        "Consolidated Statements of Cash Flows",
        "Cash Flows Statements",                      # MSFT
        "Consolidated Statement of Cash Flows",
    ],
}
NORM_R_ALIAS = {pdfx.norm_title(a): canon
                for canon, aliases in R_STATEMENT_ALIASES.items()
                for a in aliases}

EQUITY_STMT = pdfx.EQUITY_STMT

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


# --------------------------------------------------------------------------
# R-file HTML table parsing (regex-based; no lxml dependency)
# --------------------------------------------------------------------------
def _strip_html(cell: str) -> str:
    """Cell HTML -> clean text; footnote markers like [1] removed."""
    txt = re.sub(r"<[^>]+>", " ", cell)
    txt = unescape(txt)
    txt = re.sub(r"\[\d+\]", " ", txt)          # footnote references
    return re.sub(r"\s+", " ", txt).strip()


def parse_r_table(html: str) -> list:
    """First <table> of an R file -> list of row-lists (cleaned text)."""
    m = re.search(r"<table[^>]*>(.*?)</table>", html, re.S | re.I)
    if not m:
        return []
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S | re.I):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)
        if cells:
            rows.append([_strip_html(c) for c in cells])
    return rows


def detect_scale(title: str) -> float:
    """R-table titles declare units, e.g. '... - USD ($) $ in Millions'."""
    t = title.lower()
    if "in millions" in t:
        return 1.0
    if "in thousands" in t:
        return 1e-3          # convert to millions
    if "in billions" in t:
        return 1e3
    return 1e-6              # raw USD -> millions


def parse_date_header(cell: str):
    """'Dec. 31, 2025' / 'Jun. 30, 2026' -> ('2025'|'2026', month)."""
    m = re.search(r"([A-Za-z]{3})[a-z]*\.?\s+\d{1,2},\s*(\d{4})", cell)
    if not m:
        return None, None
    month = MONTHS.get(m.group(1).lower()[:3])
    return m.group(2), month


def rows_to_year_df(rows: list, scale: float):
    """Statement rows -> DataFrame('Line Item', <year>...), keeping only
    rows that carry at least one numeric value (same behavior as the PDF
    parser, which skipped pure section headers). Returns (df, fye_month)."""
    # find the header row whose cells (beyond the first) are dates
    header_years, fye_month, header_idx = None, None, None
    for i, row in enumerate(rows[:4]):
        years, months = [], []
        for cell in row[1:] if len(row) > 1 else []:
            y, mo = parse_date_header(cell)
            if y:
                years.append(y)
                months.append(mo)
        if years and len(years) == len(row) - 1:
            header_years, header_idx = years, i
            fye_month = months[0]
            break
        # '12 Months Ended' spanning row: dates come on the NEXT row,
        # offset by one (no leading label cell)
        if any("months ended" in c.lower() for c in row) and i + 1 < len(rows):
            nxt = rows[i + 1]
            years, months = [], []
            for cell in nxt:
                y, mo = parse_date_header(cell)
                if y:
                    years.append(y)
                    months.append(mo)
            if years:
                header_years, header_idx = years, i + 1
                fye_month = months[0]
                break
    if not header_years:
        return None, None

    out = []
    for row in rows[header_idx + 1:]:
        if len(row) < 2 or not row[0]:
            continue
        label = row[0].rstrip(":").strip()
        cells = list(row[1:])
        # some issuers' R tables (e.g. ORCL) carry an extra empty spacer
        # cell before/after the values — drop empty edge cells only while
        # the row is wider than the year columns, so genuinely blank years
        # in exact-width rows are preserved
        while len(cells) > len(header_years) and cells and cells[0] == "":
            cells.pop(0)
        while len(cells) > len(header_years) and cells and cells[-1] == "":
            cells.pop()
        # empty cells in R files mean "no value", not zero
        vals = [pdfx._clean_number(c) if c else None for c in cells]
        vals = (vals + [None] * len(header_years))[:len(header_years)]
        if all(v is None for v in vals):
            continue                      # pure section header
        scaled = [v if v is None or scale == 1.0 else round(v * scale, 4)
                  for v in vals]
        out.append([label] + scaled)
    if not out:
        return None, fye_month
    df = pd.DataFrame(out, columns=["Line Item"] + header_years)
    # drop duplicate labels keeping first occurrence (R files occasionally
    # repeat a line for footnoted variants)
    df = df.drop_duplicates(subset=["Line Item"], keep="first")
    return df, fye_month


def rows_to_equity_df(rows: list):
    """Equity statement -> component-matrix DataFrame:
    'Line Item' + one column per equity component (Total, APIC, AOCI, ...)."""
    if not rows:
        return None
    header = rows[0]
    comps = [c for c in header[1:] if c]
    if not comps:
        return None
    out = []
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        label = row[0].strip()
        vals = [pdfx._clean_number(c) if c else None for c in row[1:len(comps) + 1]]
        vals += [None] * (len(comps) - len(vals))
        if all(v is None for v in vals) and "balance" not in label.lower():
            continue
        out.append([label] + vals)
    if not out:
        return None
    return pd.DataFrame(out, columns=["Line Item"] + comps)


# --------------------------------------------------------------------------
# Per-filing extraction from EDGAR
# --------------------------------------------------------------------------
def _collect_10ks(block: dict, filings: list, n: int):
    for form, acc, rdate, fdate in zip(block["form"], block["accessionNumber"],
                                       block["reportDate"], block["filingDate"]):
        if form != "10-K":
            continue
        fy = int(rdate[:4])           # fiscal year = report period's year
        filings.append((fy, acc.replace("-", ""), fdate))
        if len(filings) == n:
            return


def list_10k_filings(cik: int, n: int) -> list:
    """Latest n 10-K filings -> [(fiscal_year, accession_nodash, filed)].
    Heavy filers (e.g. META) overflow the 1000-entry 'recent' window, so
    older paginated submission files are walked when needed."""
    sub = fq.http_json(SUBMISSIONS_URL.format(cik=cik))
    filings = []
    _collect_10ks(sub["filings"]["recent"], filings, n)
    for older in sub["filings"].get("files", []):
        if len(filings) >= n:
            break
        page = fq.http_json(
            f"https://data.sec.gov/submissions/{older['name']}")
        _collect_10ks(page, filings, n)
    return filings


def http_text(url: str) -> str:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": fq.USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def extract_filing_from_edgar(cik: int, acc: str) -> tuple:
    """One 10-K accession -> ({statement: DataFrame}, fye_month)."""
    base = ARCHIVES_URL.format(cik=cik, acc=acc)
    summary = http_text(f"{base}/FilingSummary.xml")

    # map canonical statement -> R html file (first non-parenthetical match)
    r_files = {}
    for block in re.findall(r"<Report[^>]*>(.*?)</Report>", summary, re.S):
        name_m = re.search(r"<ShortName>(.*?)</ShortName>", block, re.S)
        file_m = re.search(r"<HtmlFileName>(.*?)</HtmlFileName>", block, re.S)
        if not name_m or not file_m:
            continue
        short = unescape(name_m.group(1)).strip()
        if "parenthetical" in short.lower():
            continue
        norm_short = pdfx.norm_title(short)
        canon = NORM_R_ALIAS.get(norm_short)
        if canon is None:
            # prefix fallback: catches trailing qualifiers not in the alias
            # list, e.g. "... STOCKHOLDERS' EQUITY (DEFICIT)" (ORCL) or
            # "... OPERATIONS (UNAUDITED)"
            canon = next((c for alias_norm, c in NORM_R_ALIAS.items()
                          if norm_short.startswith(alias_norm)), None)
        if canon and canon not in r_files:
            r_files[canon] = file_m.group(1).strip()

    missing = [s for s in pdfx.STATEMENT_NAMES if s not in r_files]
    if missing:
        raise RuntimeError(f"accession {acc}: statements not found in "
                           f"FilingSummary: {missing}")

    dataframes, fye_month = {}, None
    for canon, fn in r_files.items():
        rows = parse_r_table(http_text(f"{base}/{fn}"))
        if not rows:
            print(f"      WARNING {acc}: empty R table for '{canon}'")
            continue
        title = rows[0][0] if rows[0] else ""
        scale = detect_scale(title)
        if canon == EQUITY_STMT:
            df = rows_to_equity_df(rows)
        else:
            df, fm = rows_to_year_df(rows[1:] if len(rows[0]) == 1 else rows, scale)
            fye_month = fye_month or fm
        if df is None or df.empty:
            print(f"      WARNING {acc}: could not parse '{canon}'")
            continue
        dataframes[canon] = df
    return dataframes, fye_month


# --------------------------------------------------------------------------
# Company pipeline (merge + ratios reused from the PDF extractor)
# --------------------------------------------------------------------------
def fye_name(month) -> str:
    """Month number -> 'May 31' style label (non-leap-year month end)."""
    if not month:
        return "December 31"
    import calendar
    return f"{fq.MONTH_NAMES[month]} {calendar.monthrange(2025, month)[1]}"


def build_company_annual(ticker: str, cik: int, n_filings: int) -> dict:
    filings_meta = list_10k_filings(cik, n_filings)
    if not filings_meta:
        raise RuntimeError(f"{ticker}: no 10-K filings on EDGAR")

    filings, sources, fye_month = [], [], None
    for fy, acc, fdate in sorted(filings_meta):
        print(f"   [{ticker}] 10-K FY{fy} (accession {acc}, filed {fdate})")
        dfs, fm = extract_filing_from_edgar(cik, acc)
        fye_month = fye_month or fm
        for name, df in dfs.items():
            print(f"      {name}: {len(df)} line items")
        filings.append((fy, dfs))
        sources.append(f"10-K FY{fy} (EDGAR): "
                       f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}")

    merged = pdfx.merge_company_statements(filings)

    # every fiscal year appearing in any statement (equity-matrix component
    # columns are not years and are filtered out by the digit match)
    years = sorted({c for df in merged.values()
                    for c in df.columns if re.fullmatch(r"\d{4}", str(c))})
    ratios_by_year = {}
    for i, y in enumerate(years):
        prior = years[i - 1] if i > 0 else None
        ratios_by_year[y] = pdfx.calculate_ratios(merged, y, prior)

    return {
        "sources": sources,
        "fiscal_year_end": fye_name(fye_month),
        "statements": {name: json.loads(df.to_json(orient="records"))
                       for name, df in merged.items()},
        "ratios_by_year": ratios_by_year,
    }


def fetch_companies(tickers=None, n_filings=DEFAULT_FILINGS,
                    out_path=DEFAULT_OUT, csv_dir=DEFAULT_CSV_DIR, log=print):
    reg = fq.load_registry()
    if not reg:
        raise ValueError("companies.json is empty — add a company first")
    tickers = [t.upper() for t in tickers] if tickers else sorted(reg)
    unknown = [t for t in tickers if t not in reg]
    if unknown:
        raise ValueError(f"Not registered: {', '.join(unknown)}")

    out_path = Path(out_path)
    doc = (json.loads(out_path.read_text(encoding="utf-8"))
           if out_path.exists() else {})
    doc.setdefault("companies", {})
    doc["annual_source"] = "SEC EDGAR 10-K R-file tables (FilingSummary.xml)"
    doc["ratio_categories"] = pdfx.RATIO_CATEGORIES
    doc["ratio_formats"] = pdfx.RATIO_FORMATS

    csv_dir = Path(csv_dir)
    for t in tickers:
        entry = reg[t]
        log(f"[{t}] fetching annual data (CIK {entry['cik']}, "
            f"latest {n_filings} 10-Ks) ...")
        built = build_company_annual(t, entry["cik"], n_filings)
        doc["companies"][t] = built
        years = sorted(built["ratios_by_year"])
        log(f"[{t}] fiscal years {years[0]}–{years[-1]}"
            if years else f"[{t}] WARNING: no fiscal years resolved")
        # per-company CSVs, same layout as the PDF pipeline
        tdir = csv_dir / t
        tdir.mkdir(parents=True, exist_ok=True)
        for name, records in built["statements"].items():
            safe = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
            pd.DataFrame(records).to_csv(tdir / f"{safe}.csv",
                                         index=False, encoding="utf-8-sig")

    for t in list(doc["companies"]):
        if t not in reg:
            del doc["companies"][t]

    doc["generated"] = date.today().isoformat()
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    log(f"Wrote {out_path}")
    return doc


# --------------------------------------------------------------------------
# CLI (registry commands shared with the quarterly fetcher)
# --------------------------------------------------------------------------
def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1].lower()

    if cmd == "add":
        if len(argv) != 4:
            print("Usage: fetch_annual_20260805.py add TICKER IR_URL")
            return 1
        entry = fq.add_company(argv[2], argv[3])
        print(f"Registered {argv[2].upper()}: {entry['name']} "
              f"(CIK {entry['cik']})\nNow run:\n"
              f"  python fetch_annual_20260805.py fetch {argv[2].upper()}\n"
              f"  python fetch_quarterly_20260805.py fetch {argv[2].upper()}")
        return 0

    if cmd == "remove":
        ok = fq.remove_company(argv[2])
        print(("Removed " if ok else "Not registered: ") + argv[2].upper())
        return 0 if ok else 1

    if cmd == "list":
        reg = fq.load_registry()
        if not reg:
            print("No companies registered.")
        for t, e in sorted(reg.items()):
            print(f"{t:6s} CIK {e['cik']:>10d}  {e['name']}\n"
                  f"       IR: {e['ir_url']}")
        return 0

    if cmd == "fetch":
        rest = argv[2:]
        n = DEFAULT_FILINGS
        if "--filings" in rest:
            i = rest.index("--filings")
            n = int(rest[i + 1])
            rest = rest[:i] + rest[i + 2:]
        fetch_companies(rest or None, n_filings=n)
        return 0

    print(f"Unknown command: {cmd}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
