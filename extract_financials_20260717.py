"""
extract_financials_20260717.py
==============================
Multi-company 10-K extraction pipeline (GOOG / AMZN / MSFT).

For every PDF in the input folder (named like TICKER-10-K-YYYY.pdf):
  1. Locate the 5 core financial statements. Primary path is the filing's
     own "Index to Consolidated Financial Statements"; if that fails
     (e.g. Microsoft's layout), fall back to scanning every page for a
     statement title line.
  2. Parse each statement's text into a DataFrame (label + one number
     per year column).
  3. Merge the three filings of each company into one history per
     statement. Conflict rule: when two filings disagree on the same
     year's value, the NEWER filing wins (restatements are logged).
  4. Calculate the full ratio set per company per year.
  5. Save everything into one combined financial_data_all.json plus
     per-company CSVs, for dashboard_20260717.py to consume.

Since v4 (2026-08-05) this is the BACKUP pipeline: annual data normally
comes from SEC EDGAR via fetch_annual_20260805.py; use this script for
non-SEC companies or when EDGAR is unavailable. Successfully parsed PDFs
are moved from the input folder into parsed_pdf_file/.

Run (finan conda env):
    python extract_financials_20260717.py [input_dir] [out_json] [csv_dir]
Defaults:
    python extract_financials_20260717.py input_pdf_file financial_data_all.json statement_csvs
"""

import io
import json
import re
import sys
from collections import OrderedDict
from datetime import date
from pathlib import Path

import pandas as pd
import pdfplumber

try:
    import fitz  # PyMuPDF, used only for the OCR fallback rasterization
    import pytesseract
    from PIL import Image
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False


# --------------------------------------------------------------------------
# Canonical statement names + per-company title variants
# --------------------------------------------------------------------------
STATEMENT_ALIASES = {
    "Consolidated Balance Sheets": [
        "Consolidated Balance Sheets",
        "Balance Sheets",
    ],
    "Consolidated Statements of Income": [
        "Consolidated Statements of Income",
        "Consolidated Statements of Operations",   # Amazon
        "Income Statements",                       # Microsoft
    ],
    "Consolidated Statements of Comprehensive Income": [
        "Consolidated Statements of Comprehensive Income",
        "Consolidated Statements of Comprehensive Income (Loss)",  # Amazon 2023
        "Comprehensive Income Statements",         # Microsoft
    ],
    "Consolidated Statements of Stockholders’ Equity": [
        "Consolidated Statements of Stockholders’ Equity",
        "Stockholders’ Equity Statements",    # Microsoft
    ],
    "Consolidated Statements of Cash Flows": [
        "Consolidated Statements of Cash Flows",
        "Cash Flows Statements",                   # Microsoft
    ],
}
STATEMENT_NAMES = list(STATEMENT_ALIASES.keys())


def norm_title(s: str) -> str:
    """Uppercase and strip everything but letters/digits so that OCR-ish
    spacing artifacts ('B alance Sheets') and quote variants still match."""
    return re.sub(r"[^A-Z0-9]", "", s.upper())


NORM_ALIAS_TO_CANON = {
    norm_title(alias): canon
    for canon, aliases in STATEMENT_ALIASES.items()
    for alias in aliases
}


# --------------------------------------------------------------------------
# Line-item label synonyms across the three companies (ordered by priority;
# matching tries exact label first, then prefix, then substring)
# --------------------------------------------------------------------------
LABEL_SYNONYMS = {
    "Revenues": {
        # "Revenue" (singular) is the XBRL preferred label used in EDGAR
        # R-file tables by MSFT and META; kept last so exact printed labels
        # still win for the PDF pipeline
        "syn": ["Revenues", "Total revenue", "Total net sales", "Total revenues",
                "Net sales", "Revenue"],
        "exclude": ["cost"],
    },
    "Cost of Revenue": {
        "syn": ["Cost of revenues", "Total cost of revenue", "Cost of revenue", "Cost of sales"],
        "exclude": [],
    },
    "Operating Income": {
        "syn": ["Income from operations", "Operating income (loss)", "Operating income"],
        "exclude": [],
    },
    "Net Income": {
        "syn": ["Net income", "Net income (loss)"],
        "exclude": ["per share", "comprehensive", "attributable"],
    },
    "Total Assets": {"syn": ["Total assets"], "exclude": []},
    "Total Liabilities": {"syn": ["Total liabilities"], "exclude": ["equity"]},
    "Total Current Assets": {"syn": ["Total current assets"], "exclude": []},
    "Total Current Liabilities": {"syn": ["Total current liabilities"], "exclude": []},
    "Total Stockholders' Equity": {
        "syn": ["Total stockholders’ equity", "Total stockholders' equity",
                "Total shareholders’ equity", "Total shareholders' equity"],
        "exclude": ["liabilities"],
    },
    "Accounts Receivable": {"syn": ["Accounts receivable"], "exclude": []},
    "Operating Cash Flow": {
        "syn": ["Net cash provided by operating activities",
                "Net cash from operations",
                "Net cash provided by (used in) operating activities"],
        "exclude": [],
    },
    "CapEx": {
        "syn": ["Purchases of property and equipment",
                "Additions to property and equipment",
                "Purchases of property, equipment"],
        "exclude": [],
    },
    "Dividends Paid": {
        "syn": ["Dividends paid", "Dividend payments",       # GOOG
                "Common stock dividends paid",                # MSFT
                "Payments of dividends", "Dividends and dividend equivalents paid"],
        "exclude": [],
    },
}


# --------------------------------------------------------------------------
# PDF text access (with OCR fallback for scanned pages)
# --------------------------------------------------------------------------
class PdfTextSource:
    """Gives 1-indexed access to page text, using pdfplumber first and
    falling back to OCR page-by-page if the text layer is empty."""

    def __init__(self, pdf_path: str):
        self.pdf_path = pdf_path
        self.plumber_pdf = pdfplumber.open(pdf_path)
        self.n_pages = len(self.plumber_pdf.pages)
        self._fitz_doc = None  # lazily opened only if OCR is needed
        self._cache = {}

    def _ocr_page(self, page_index_0based: int) -> str:
        if not OCR_AVAILABLE:
            return ""
        if self._fitz_doc is None:
            self._fitz_doc = fitz.open(self.pdf_path)
        page = self._fitz_doc[page_index_0based]
        pix = page.get_pixmap(dpi=200)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        return pytesseract.image_to_string(img)

    def text(self, page_number_1based: int) -> str:
        if page_number_1based in self._cache:
            return self._cache[page_number_1based]
        idx = page_number_1based - 1
        if not (0 <= idx < self.n_pages):
            return ""
        raw = self.plumber_pdf.pages[idx].extract_text() or ""
        if len(raw.strip()) < 20:  # essentially empty -> likely scanned page
            raw = self._ocr_page(idx)
        self._cache[page_number_1based] = raw
        return raw

    def close(self):
        self.plumber_pdf.close()
        if self._fitz_doc is not None:
            self._fitz_doc.close()


# --------------------------------------------------------------------------
# Statement page location
# --------------------------------------------------------------------------
def find_index_page(src: PdfTextSource) -> int:
    """Locate the filing's own 'Index to Consolidated Financial Statements'."""
    for p in range(1, src.n_pages + 1):
        u = src.text(p).upper()
        if "INDEX TO" in u and "FINANCIAL STATEMENTS" in u:
            return p
    return -1


def statement_pages_via_index(src: PdfTextSource) -> dict:
    """Primary locator: read the statements index page, which lists each
    statement's printed page number; derive the printed->physical offset
    from the index page's own footer number."""
    index_page = find_index_page(src)
    if index_page < 0:
        raise RuntimeError("No financial-statements index page found.")
    t = src.text(index_page)

    # printed page number in the footer of the index page itself
    matches = re.findall(r"(?m)^(\d{1,3})\.?\s*$", t)
    if not matches:
        raise RuntimeError("Could not read the index page's footer number.")
    offset = index_page - int(matches[-1])

    found = {}
    for line in t.splitlines():
        m = re.match(r"^(.*?)\s+(\d{1,3})\s*$", line.strip())
        if not m:
            continue
        canon = NORM_ALIAS_TO_CANON.get(norm_title(m.group(1)))
        if canon and canon not in found:
            found[canon] = int(m.group(2)) + offset
    return found


def statement_pages_via_scan(src: PdfTextSource, already_found: dict) -> dict:
    """Fallback locator: a statement page is one whose first few lines
    contain a bare statement title (no trailing page number, so index/TOC
    entries don't match)."""
    found = dict(already_found)
    for p in range(1, src.n_pages + 1):
        if len(found) == len(STATEMENT_NAMES):
            break
        lines = [l.strip() for l in src.text(p).splitlines() if l.strip()][:8]
        for line in lines:
            canon = NORM_ALIAS_TO_CANON.get(norm_title(line))
            if canon and canon not in found:
                found[canon] = p
    return found


def locate_statements(src: PdfTextSource) -> dict:
    found = {}
    try:
        found = statement_pages_via_index(src)
    except RuntimeError as e:
        print(f"      index-based location failed ({e}); scanning pages instead")
    # verify index hits actually carry the statement title; drop bogus ones
    for name, page in list(found.items()):
        lines = [l.strip() for l in src.text(page).splitlines() if l.strip()][:8]
        if not any(NORM_ALIAS_TO_CANON.get(norm_title(l)) == name for l in lines):
            del found[name]
    if len(found) < len(STATEMENT_NAMES):
        found = statement_pages_via_scan(src, found)
    missing = [n for n in STATEMENT_NAMES if n not in found]
    if missing:
        raise RuntimeError(f"Could not locate statement pages for: {missing}")
    return found


# --------------------------------------------------------------------------
# Statement text -> DataFrame
# --------------------------------------------------------------------------
# a value cell: number (optionally $-prefixed and/or parenthesised, in either
# order — "$ (2,722)" appears in loss years) or a dash placeholder meaning zero
NUM_TOKEN = r"\(?\$?\s?\(?(?:-?[\d,]+(?:\.\d+)?|—|-)\)?"


def _clean_number(tok: str):
    if tok is None:
        return None
    tok = tok.strip()
    neg = "(" in tok and ")" in tok
    tok = tok.strip("()$ ").replace(",", "")
    if tok in ("", "-", "—"):
        return 0.0
    try:
        val = float(tok)
    except ValueError:
        return None
    return -val if neg else val


def extract_year_headers(text: str):
    """Find the fiscal-year column headers near the top of a statement page.
    Handles 'Year Ended December 31,' (GOOG/AMZN) and 'Year Ended June 30,'
    (MSFT), with the years on the same or the following line. Also returns
    the fiscal year-end phrase when one is seen."""
    fye = None
    m = re.search(
        r"(?:Year[s]? Ended|As of|Ended)?\s*(December\s*31|June\s*30),?\s*\n?([\d\s,]+)",
        text)
    if m:
        fye = re.sub(r"\s+", " ", m.group(1))
        years = re.findall(r"(?:19|20)\d{2}", m.group(2))
        if 2 <= len(years) <= 4:
            return years, fye
    # fallback: first of the top lines made up almost entirely of years
    for line in text.splitlines()[:15]:
        years = re.findall(r"\b(?:19|20)\d{2}\b", line)
        leftover = re.sub(r"\b(?:19|20)\d{2}\b", "", line)
        if 2 <= len(years) <= 4 and len(re.sub(r"[\s,$]", "", leftover)) <= 4:
            return years, fye
    return None, fye


def parse_statement_page(text: str, year_headers: list) -> pd.DataFrame:
    """Generic line-item parser: for each line that ends in one number per
    year column, split off the leading label from the trailing numbers."""
    rows = []
    n_years = len(year_headers)
    # "$" is allowed inside labels so lines like "..., net of allowance for
    # doubtful accounts of $830 and $650" (MSFT) still split correctly
    line_re = re.compile(
        r"^(?P<label>[A-Za-z][A-Za-z0-9 ,.’'()/&%$-]*?)\s+"
        r"(?P<nums>(?:" + NUM_TOKEN + r"\s*){" + str(n_years) + r"})$"
    )
    for raw_line in text.splitlines():
        line = raw_line.strip().replace("–", "-")
        if not line:
            continue
        m = line_re.match(line)
        if not m:
            continue
        label = m.group("label").strip().rstrip(" $")
        nums_str = m.group("nums").strip()
        tokens = re.findall(NUM_TOKEN, nums_str)
        if len(tokens) != n_years:
            continue
        values = [_clean_number(t) for t in tokens]
        if any(v is None for v in values):
            continue
        rows.append([label] + values)

    return pd.DataFrame(rows, columns=["Line Item"] + year_headers)


# --------------------------------------------------------------------------
# Per-filing extraction
# --------------------------------------------------------------------------
EQUITY_STMT = "Consolidated Statements of Stockholders’ Equity"


def parse_equity_page(text: str) -> pd.DataFrame:
    """The stockholders' equity statement has share/amount matrix columns
    rather than one column per fiscal year, so it can't be parsed into the
    year schema. Keep it as raw display lines for the dashboard."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return pd.DataFrame({"Raw Line": lines})


def _looks_like_continuation(text: str, statement_name: str) -> bool:
    """A next page counts as a continuation only if it repeats the statement
    title (SEC filings re-print '... (Continued)' headers) — otherwise we
    would swallow the following statement or notes."""
    lines = [l.strip() for l in text.splitlines() if l.strip()][:8]
    for line in lines:
        cleaned = re.sub(r"\(?continued\)?", "", line, flags=re.I)
        if NORM_ALIAS_TO_CANON.get(norm_title(cleaned)) == statement_name:
            return True
    return False


def extract_filing(pdf_path: Path) -> tuple:
    """Extract the 5 statements from one 10-K. Returns (dataframes, meta)."""
    src = PdfTextSource(str(pdf_path))
    try:
        pages = locate_statements(src)
        dataframes, fye_seen = {}, None
        for name, phys in pages.items():
            text = src.text(phys)
            if name == EQUITY_STMT:
                dataframes[name] = parse_equity_page(text)
                continue
            years, fye = extract_year_headers(text)
            fye_seen = fye_seen or fye
            if not years:
                print(f"      WARNING {pdf_path.name}: no year headers on "
                      f"'{name}' (physical page {phys}); statement skipped")
                continue
            df = parse_statement_page(text, years)
            # statements can spill onto the next page (e.g. cash flows)
            next_phys = phys + 1
            while next_phys <= src.n_pages and next_phys not in pages.values():
                next_text = src.text(next_phys)
                if not _looks_like_continuation(next_text, name):
                    break
                df = pd.concat([df, parse_statement_page(next_text, years)],
                               ignore_index=True)
                next_phys += 1
            dataframes[name] = df
        meta = {"pages": pages, "fiscal_year_end": fye_seen}
        return dataframes, meta
    finally:
        src.close()


# --------------------------------------------------------------------------
# Merge filings per company (newer filing wins on conflicts)
# --------------------------------------------------------------------------
def merge_company_statements(filings: list) -> dict:
    """filings: list of (filing_year, {statement: df}) sorted ascending.
    Returns {statement: merged df}. Conflict rule: for the same statement /
    line item / fiscal year, the value from the NEWER filing replaces any
    older value; differences are logged as restatements."""
    merged = {}
    for name in STATEMENT_NAMES:
        if name == EQUITY_STMT:
            # raw display lines, no year columns to merge: newest filing only
            for filing_year, dfs in sorted(filings, key=lambda x: x[0], reverse=True):
                df = dfs.get(name)
                if df is not None and not df.empty:
                    merged[name] = df
                    break
            continue
        values = OrderedDict()          # label -> {year: (value, filing_year)}
        latest_order, older_order = [], []
        for filing_year, dfs in sorted(filings, key=lambda x: x[0]):
            df = dfs.get(name)
            if df is None or df.empty:
                continue
            year_cols = [c for c in df.columns if c != "Line Item"]
            seen_this_filing = set()
            order_this_filing = []
            for _, row in df.iterrows():
                label = row["Line Item"]
                if label in seen_this_filing:
                    continue  # keep first occurrence within one filing
                seen_this_filing.add(label)
                order_this_filing.append(label)
                slot = values.setdefault(label, {})
                for y in year_cols:
                    val = row[y]
                    if pd.isna(val):
                        continue
                    if y in slot and slot[y][0] != val:
                        old_val, old_filing = slot[y][0], slot[y][1]
                        print(f"      CONFLICT {name} / '{label}' FY{y}: "
                              f"{old_val:,.0f} (10-K {old_filing}) -> "
                              f"{val:,.0f} (10-K {filing_year}); newer filing wins")
                    slot[y] = (val, filing_year)
            older_order = latest_order + [l for l in older_order if l not in latest_order]
            latest_order = order_this_filing
        if not values:
            continue
        all_years = sorted({y for slot in values.values() for y in slot})
        order = latest_order + [l for l in older_order if l not in latest_order]
        order += [l for l in values if l not in order]
        rows = [[label] + [values[label].get(y, (None,))[0] for y in all_years]
                for label in order]
        merged[name] = pd.DataFrame(rows, columns=["Line Item"] + all_years)
    return merged


# --------------------------------------------------------------------------
# Ratio calculation
# --------------------------------------------------------------------------
def norm_label(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().replace("’", "'")).lower()


def get_value(df: pd.DataFrame, concept: str, year: str):
    """Look a canonical concept up in a statement DataFrame, trying exact
    label match first, then prefix, then substring, across all synonyms."""
    if df is None or df.empty or year not in df.columns:
        return None
    spec = LABEL_SYNONYMS[concept]
    labels = df["Line Item"].map(norm_label)
    for syn in spec["syn"]:
        s = norm_label(syn)
        for mask in (labels == s,
                     labels.str.startswith(s),
                     labels.str.contains(re.escape(s), regex=True)):
            for idx in df.index[mask]:
                if any(e in labels[idx] for e in spec["exclude"]):
                    continue
                val = df.at[idx, year]
                if val is not None and not pd.isna(val):
                    return float(val)
    return None


RATIO_CATEGORIES = {
    "Gross Margin": "Profitability",
    "Operating Margin": "Profitability",
    "Net Profit Margin": "Profitability",
    "ROA": "Profitability",
    "ROE": "Profitability",
    "Current Ratio": "Liquidity",
    "Operating CF Ratio": "Liquidity",
    "Debt Ratio": "Leverage",
    "Debt-to-Equity": "Leverage",
    "Asset Turnover": "Efficiency",
    "Receivables Turnover": "Efficiency",
    "DSO (days)": "Efficiency",
    "Free Cash Flow ($M)": "Cash Flow",
    "FCF Margin": "Cash Flow",
    "Cash Flow to Net Income": "Cash Flow",
    "CapEx Intensity": "Cash Flow",
    "Dividend Payout Ratio": "Cash Flow",
}

RATIO_FORMATS = {  # hint for the dashboard: percent / x (multiple) / days / $M
    "Gross Margin": "percent", "Operating Margin": "percent",
    "Net Profit Margin": "percent", "ROA": "percent", "ROE": "percent",
    "Current Ratio": "x", "Operating CF Ratio": "x",
    "Debt Ratio": "percent", "Debt-to-Equity": "x",
    "Asset Turnover": "x", "Receivables Turnover": "x", "DSO (days)": "days",
    "Free Cash Flow ($M)": "musd", "FCF Margin": "percent",
    "Cash Flow to Net Income": "x", "CapEx Intensity": "percent",
    "Dividend Payout Ratio": "percent",
}


def calculate_ratios(statements: dict, year: str, prior_year: str) -> dict:
    bs = statements.get("Consolidated Balance Sheets")
    inc = statements.get("Consolidated Statements of Income")
    cf = statements.get("Consolidated Statements of Cash Flows")

    def bal(concept, y):
        return get_value(bs, concept, y) if y else None

    revenues = get_value(inc, "Revenues", year)
    cogs = get_value(inc, "Cost of Revenue", year)
    op_income = get_value(inc, "Operating Income", year)
    net_income = get_value(inc, "Net Income", year)
    total_assets = bal("Total Assets", year)
    total_liabilities = bal("Total Liabilities", year)
    if total_liabilities is None:
        # Amazon's balance sheet has no standalone "Total liabilities" line —
        # derive it from the accounting identity assets = liabilities + equity
        eq_tmp = bal("Total Stockholders' Equity", year)
        if total_assets is not None and eq_tmp is not None:
            total_liabilities = total_assets - eq_tmp
    tca = bal("Total Current Assets", year)
    tcl = bal("Total Current Liabilities", year)
    equity = bal("Total Stockholders' Equity", year)
    receivables = bal("Accounts Receivable", year)
    ocf = get_value(cf, "Operating Cash Flow", year)
    capex = get_value(cf, "CapEx", year)
    dividends = get_value(cf, "Dividends Paid", year)

    def avg(cur, prev):
        if cur is None:
            return None
        return (cur + prev) / 2 if prev is not None else cur

    avg_assets = avg(total_assets, bal("Total Assets", prior_year))
    avg_equity = avg(equity, bal("Total Stockholders' Equity", prior_year))
    avg_receivables = avg(receivables, bal("Accounts Receivable", prior_year))

    def safe_div(a, b):
        return round(a / b, 4) if a is not None and b not in (None, 0) else None

    fcf = ocf - abs(capex) if ocf is not None and capex is not None else None
    rec_turnover = safe_div(revenues, avg_receivables)

    ratios = {
        "Gross Margin": safe_div(revenues - cogs, revenues)
                        if revenues is not None and cogs is not None else None,
        "Operating Margin": safe_div(op_income, revenues),
        "Net Profit Margin": safe_div(net_income, revenues),
        "ROA": safe_div(net_income, avg_assets),
        "ROE": safe_div(net_income, avg_equity),
        "Current Ratio": safe_div(tca, tcl),
        "Operating CF Ratio": safe_div(ocf, tcl),
        "Debt Ratio": safe_div(total_liabilities, total_assets),
        "Debt-to-Equity": safe_div(total_liabilities, equity),
        "Asset Turnover": safe_div(revenues, avg_assets),
        "Receivables Turnover": rec_turnover,
        "DSO (days)": round(365 / rec_turnover, 1) if rec_turnover else None,
        "Free Cash Flow ($M)": round(fcf, 1) if fcf is not None else None,
        "FCF Margin": safe_div(fcf, revenues),
        "Cash Flow to Net Income": safe_div(ocf, net_income),
        "CapEx Intensity": safe_div(abs(capex) if capex is not None else None, revenues),
        "Dividend Payout Ratio": safe_div(abs(dividends) if dividends is not None else None,
                                          net_income),
        "_raw": {
            "Revenues": revenues, "Cost of Revenue": cogs,
            "Operating Income": op_income, "Net Income": net_income,
            "Total Assets": total_assets, "Total Liabilities": total_liabilities,
            "Total Current Assets": tca, "Total Current Liabilities": tcl,
            "Total Stockholders' Equity": equity, "Accounts Receivable": receivables,
            "Operating Cash Flow": ocf, "CapEx": capex, "Dividends Paid": dividends,
            "Average Total Assets": avg_assets, "Average Equity": avg_equity,
        },
    }
    return ratios


# --------------------------------------------------------------------------
# Main pipeline
# --------------------------------------------------------------------------
FILENAME_RE = re.compile(r"([A-Za-z]+)-10-?K-(\d{4})", re.IGNORECASE)


# successfully parsed PDFs are moved here, out of the input queue
PARSED_DIR = Path(__file__).parent / "parsed_pdf_file"


def run(input_dir: str, out_json: str, csv_dir: str):
    pdfs = sorted(Path(input_dir).glob("*.pdf"))
    if not pdfs:
        raise SystemExit(
            f"No PDFs found in {input_dir} — drop TICKER-10-K-YYYY.pdf files "
            f"there (already-parsed ones live in {PARSED_DIR.name}/)")

    by_company = {}
    for pdf in pdfs:
        m = FILENAME_RE.search(pdf.name)
        if not m:
            print(f"Skipping {pdf.name}: filename doesn't look like TICKER-10-K-YYYY.pdf")
            continue
        ticker, filing_year = m.group(1).upper(), int(m.group(2))
        by_company.setdefault(ticker, []).append((filing_year, pdf))

    if not by_company:
        # never overwrite the output with an empty company set
        raise SystemExit(
            f"No TICKER-10-K-YYYY.pdf files found in {input_dir}; "
            "nothing extracted and the output file was left untouched.")

    companies_out = {}
    for ticker, filings in sorted(by_company.items()):
        print(f"\n===== {ticker} ({len(filings)} filings) =====")
        extracted, fye, sources = [], None, []
        for filing_year, pdf in sorted(filings):
            print(f"  [{ticker} 10-K {filing_year}] {pdf.name}")
            dfs, meta = extract_filing(pdf)
            fye = fye or meta["fiscal_year_end"]
            # parsed OK -> move out of the input queue into parsed_pdf_file/
            if pdf.parent.resolve() != PARSED_DIR.resolve():
                PARSED_DIR.mkdir(exist_ok=True)
                dest = PARSED_DIR / pdf.name
                pdf.rename(dest)
                print(f"      parsed OK -> moved to {PARSED_DIR.name}/{pdf.name}")
                pdf = dest
            sources.append(str(pdf).replace("\\", "/"))
            for name, df in dfs.items():
                print(f"      {name}: {len(df)} line items "
                      f"(years {[c for c in df.columns if c != 'Line Item']})")
            extracted.append((filing_year, dfs))

        print(f"  Merging {ticker} filings (newer filing wins on conflicts) ...")
        merged = merge_company_statements(extracted)

        # ratios for every fiscal year that appears in any statement
        # (the equity statement's "Raw Line" column is not a year)
        all_years = sorted({c for df in merged.values()
                            for c in df.columns if re.fullmatch(r"\d{4}", str(c))})
        ratios_by_year = {}
        for i, y in enumerate(all_years):
            prior = all_years[i - 1] if i > 0 else None
            ratios_by_year[y] = calculate_ratios(merged, y, prior)

        # per-company CSVs
        out_dir = Path(csv_dir) / ticker
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, df in merged.items():
            csv_name = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") + ".csv"
            df.to_csv(out_dir / csv_name, index=False)

        companies_out[ticker] = {
            "sources": sources,
            "fiscal_year_end": fye or "December 31",
            "statements": {
                name: df.where(pd.notna(df), None).to_dict(orient="records")
                for name, df in merged.items()
            },
            "ratios_by_year": ratios_by_year,
        }
        print(f"  {ticker}: years {all_years}, CSVs -> {out_dir}")

    output = {
        "generated": date.today().isoformat(),
        "ratio_categories": RATIO_CATEGORIES,
        "ratio_formats": RATIO_FORMATS,
        "companies": companies_out,
    }
    Path(out_json).parent.mkdir(parents=True, exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\nDone. Combined data for {list(companies_out)} written to {out_json}")
    return output


if __name__ == "__main__":
    input_dir = sys.argv[1] if len(sys.argv) > 1 else "input_pdf_file"
    out_json = sys.argv[2] if len(sys.argv) > 2 else "financial_data_all.json"
    csv_dir = sys.argv[3] if len(sys.argv) > 3 else "statement_csvs"
    run(input_dir, out_json, csv_dir)
