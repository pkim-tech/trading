#!/usr/bin/env python3
"""
Import results.csv into the tickers table in trading_universe.db.
Safe to re-run — uses INSERT OR REPLACE.

Usage:
    python scripts/import_tickers.py
    python scripts/import_tickers.py path/to/other.csv
"""

import re
import sys
import sqlite3
import pandas as pd
from pathlib import Path


COMPANY_SUFFIXES = re.compile(
    r'\bInc\.?\b|\bCorp\.?\b|\bLtd\.?\b|\bLLC\b|'
    r'\bN\.?V\.?\b|\bHoldings?\b|\bGroup\b|Class [AB]\b|'
    r'\bADR\b|Sponsored|\bPlc\b|\bS\.?A\.?\b|\bAG\b|'
    r'\bCorporation\b|\bIncorporated\b',
    re.IGNORECASE,
)

LEVERAGE_RE = re.compile(r'\b(\d+(?:\.\d+)?)X\b', re.IGNORECASE)


def parse_leverage(description: str) -> float | None:
    desc = str(description)
    m = LEVERAGE_RE.search(desc)
    if m:
        return float(m.group(1))
    dl = desc.lower()
    if 'ultrapro' in dl:
        return 3.0
    if 'ultrashort' in dl or 'ultra short' in dl:
        return 2.0
    if 'ultra' in dl:
        return 2.0
    return None


SINGLE_STOCK_DESC = re.compile(
    r'\b(?:Long|Short)\s+[A-Z]{2,5}\s+(?:Daily|Option|ETF)\b'
    r'|[A-Z]{2,5}\s+WeeklyPay'
    r'|ADRhedged',
    re.IGNORECASE,
)


def classify_underlier(underlying_index: str, description: str = "") -> tuple[str | None, str | None]:
    """Returns (stock_underlier, index_underlier) — exactly one is non-None, or both None."""
    ui   = str(underlying_index).strip()
    desc = str(description).strip()

    if not ui or ui in ('--', 'No Underlying Index'):
        # Fall back to description-based detection
        if SINGLE_STOCK_DESC.search(desc):
            return desc, None
        return None, None

    if 'Index' in ui:
        return None, ui

    if COMPANY_SUFFIXES.search(ui):
        return ui, None

    if SINGLE_STOCK_DESC.search(desc):
        return ui, None

    # Crypto/commodity/currency — neither stock nor index
    return None, None


DB_PATH   = Path("./cache/research/trading_universe.db")
CACHE_DIR = Path("./cache/research")
CSV_PATH  = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./results.csv")


def clean_pct(val):
    if pd.isna(val) or str(val).strip() in ("--", ""):
        return None
    return float(str(val).replace("%", "").replace(",", "").strip())


def clean_assets(val):
    if pd.isna(val) or str(val).strip() in ("--", ""):
        return None
    return float(str(val).replace("$", "").replace(",", "").strip())


def clean_vol(val):
    if pd.isna(val) or str(val).strip() in ("--", ""):
        return None
    return float(str(val).replace(",", "").strip())


def clean_price(val):
    if pd.isna(val) or str(val).strip() in ("--", ""):
        return None
    return float(str(val).replace("$", "").replace(",", "").strip())


df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
df.columns = df.columns.str.strip()

rows = []
for _, r in df.iterrows():
    desc  = str(r.get("Description", "")).strip()
    ui    = str(r.get("Underlying Index", "")).strip()
    stock_underlier, index_underlier = classify_underlier(ui, desc)

    rows.append((
        str(r["Symbol"]).strip(),
        desc,
        str(r.get("Fund Type", "")).strip(),
        clean_pct(r.get("Price Change (Last 5 Years)")),
        clean_pct(r.get("Price Change (Last 3 Years)")),
        clean_pct(r.get("Price Change (Last 12 Months)")),
        clean_pct(r.get("Price Change (Last 6 Months)")),
        clean_pct(r.get("Price Change (Last 3 Months)")),
        clean_pct(r.get("Price Change (Last Month)")),
        clean_vol(r.get("Average Volume (10 Day)")),
        str(r.get("Price Range", "")).strip(),
        str(r.get("Leveraged ETP", "")).strip(),
        str(r.get("Diversified Portfolio", "")).strip(),
        str(r.get("MACD", "")).strip(),
        str(r.get("Relative Strength Index - 14 Day (RSI)", "")).strip(),
        str(r.get("50 & 200 Day SMA Cross", "")).strip(),
        str(r.get("Stochastic Oscillator (Bearish) - 5 Day", "")).strip(),
        str(r.get("Stochastic Oscillator (Bullish) - 5 Day", "")).strip(),
        clean_assets(r.get("Total Assets")),
        1 if ("Inverse" in str(r.get("Fund Type", "")) or
              any(w in desc for w in ("Short", "Inverse", "Bear"))) else 0,
        clean_price(r.get("Price Range")),
        1 if (CACHE_DIR / f"{str(r['Symbol']).strip()}_1h.csv").exists() else 0,
        parse_leverage(desc),
        stock_underlier,
        index_underlier,
    ))

with sqlite3.connect(DB_PATH) as conn:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tickers (
            symbol              TEXT PRIMARY KEY,
            description         TEXT,
            fund_type           TEXT,
            pct_chg_5y          REAL,
            pct_chg_3y          REAL,
            pct_chg_12m         REAL,
            pct_chg_6m          REAL,
            pct_chg_3m          REAL,
            pct_chg_1m          REAL,
            avg_vol_10d         REAL,
            price_range         TEXT,
            leveraged_etp       TEXT,
            diversified         TEXT,
            macd                TEXT,
            rsi                 TEXT,
            sma_cross           TEXT,
            stoch_bearish       TEXT,
            stoch_bullish       TEXT,
            total_assets        REAL,
            inverse             INTEGER DEFAULT 0,
            last_price          REAL,
            has_data            INTEGER DEFAULT 0,
            leverage            REAL,
            stock_underlier     TEXT,
            index_underlier     TEXT
        );
    """)
    conn.executemany("""
        INSERT OR REPLACE INTO tickers VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
    """, rows)
    conn.commit()

print(f"Imported {len(rows)} tickers from {CSV_PATH} → {DB_PATH}")
