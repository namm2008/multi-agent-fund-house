"""Data layer: universe, daily bars (IBKR), fundamentals (SEC EDGAR), macro (FRED).

Everything is cached to parquet/json under data/. Every agent works from these
files — agents never invent numbers.
"""
from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- #
# Universe
# --------------------------------------------------------------------------- #
def ib_symbol(sym: str) -> str:
    """S&P list uses BRK.B; IBKR uses 'BRK B'."""
    return sym.replace(".", " ").replace("-", " ")


def load_universe(cfg, refresh: bool = False) -> pd.DataFrame:
    path = cfg.data_dir / "universe.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path, dtype={"CIK": str})
    import requests
    txt = requests.get(cfg.universe_source, timeout=30).text
    df = pd.read_csv(io.StringIO(txt), dtype={"CIK": str})
    df = df.rename(columns={"Symbol": "symbol", "Security": "name",
                            "GICS Sector": "sector",
                            "GICS Sub-Industry": "industry"})
    df["ib_symbol"] = df["symbol"].map(ib_symbol)
    df["CIK"] = df["CIK"].str.zfill(10)
    df = df[["symbol", "ib_symbol", "name", "sector", "industry", "CIK"]]
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return df


# --------------------------------------------------------------------------- #
# Daily bars from IBKR
# --------------------------------------------------------------------------- #
def bars_path(cfg, sym: str) -> Path:
    return cfg.data_dir / "bars" / f"{sym.replace(' ', '_')}.parquet"


def load_bars(cfg, sym: str) -> Optional[pd.DataFrame]:
    p = bars_path(cfg, sym)
    if not p.exists():
        return None
    return pd.read_parquet(p)


def load_all_bars(cfg, symbols: List[str]) -> Dict[str, pd.DataFrame]:
    out = {}
    for s in symbols:
        df = load_bars(cfg, s)
        if df is not None and len(df) > 0:
            out[s] = df
    return out


def _bars_df(bars) -> pd.DataFrame:
    return pd.DataFrame([{"date": pd.Timestamp(b.date), "open": b.open, "high": b.high,
                          "low": b.low, "close": b.close, "volume": b.volume}
                         for b in bars]).set_index("date")


def update_bars(ib, cfg, symbols: List[str], log=print, fallback: bool = True,
                timeout: float = 20.0, dead_after: int = 6) -> Dict[str, str]:
    """Download/refresh adjusted daily bars. Incremental: fetches only what's
    missing (a full 2Y pull the first time, ~10 days afterwards).

    If IBKR's US historical-data farm is down (seen 16-17 Oct 2026: 'ushmds not
    connected', every request timed out after 60 s, the 06:30 job ran for 5 hours and
    saved nothing), `dead_after` consecutive empty/failed answers switch the rest of
    the run - and every symbol that failed - to Yahoo Finance (fallback=True)."""
    from fh.ib import stock
    status: Dict[str, str] = {}
    misses = 0
    for i, sym in enumerate(symbols):
        if misses >= dead_after:
            break
        old = load_bars(cfg, sym)
        if old is not None and len(old) > 200:
            gap = (pd.Timestamp.now().normalize() - old.index[-1]).days
            duration = f"{min(max(gap + 5, 10), 360)} D"
        else:
            duration = "2 Y"
        try:
            c = stock(sym)
            ib.qualifyContracts(c)
            bars = ib.reqHistoricalData(
                c, endDateTime="", durationStr=duration, barSizeSetting="1 day",
                whatToShow="ADJUSTED_LAST", useRTH=True, formatDate=1, timeout=timeout)
            if not bars:
                status[sym] = "empty"
            else:
                df = _bars_df(bars)
                if old is not None and duration != "2 Y":
                    # ADJUSTED_LAST can re-base history after a split/dividend: if
                    # the overlap disagrees by >0.5% re-pull everything.
                    ov = old.index.intersection(df.index)
                    if len(ov) and (abs(old.loc[ov, "close"] / df.loc[ov, "close"] - 1)
                                    .max() > 0.005):
                        # re-pull the SAME span we already hold (a 2-year re-pull used to
                        # silently cut 12-year backtest history after a dividend/split)
                        yrs = max(2, int((pd.Timestamp.now() - old.index[0]).days / 365.25) + 1)
                        df = _bars_df(ib.reqHistoricalData(
                            c, endDateTime="", durationStr=f"{yrs} Y",
                            barSizeSetting="1 day", whatToShow="ADJUSTED_LAST",
                            useRTH=True, formatDate=1, timeout=max(timeout, 60)))
                    else:
                        df = pd.concat([old[~old.index.isin(df.index)], df]).sort_index()
                df.to_parquet(bars_path(cfg, sym))
                status[sym] = "ok"
        except Exception as e:  # noqa: BLE001
            status[sym] = f"error: {e}"
        misses = 0 if status.get(sym) == "ok" else misses + 1
        if i % 25 == 24:
            log(f"  bars {i + 1}/{len(symbols)}")
        ib.sleep(0.35)   # pacing: stay well under IBKR historical-data limits
    todo = [s for s in symbols if status.get(s) != "ok"]
    if misses >= dead_after:
        log(f"  IBKR historical data not answering ({dead_after} misses in a row) - "
            f"{len(todo)} symbols to {'Yahoo Finance' if fallback else 'nothing (fallback off)'}")
    elif todo and fallback:
        log(f"  {len(todo)} IBKR misses -> Yahoo Finance")
    if todo and fallback:
        ys = yahoo_update_bars(cfg, todo, log)
        for s in todo:
            status[s] = ys.get(s, status.get(s, "empty"))
    return status


def yahoo_symbol(sym: str) -> str:
    return sym.replace(" ", "-").replace(".", "-")          # 'BRK B' -> 'BRK-B'


def _merge_external(old: Optional[pd.DataFrame], new: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Append another source's adjusted bars to our history without a seam.

    Both feeds are split+dividend adjusted but can sit on a different adjustment base;
    old history is rescaled by the ratio on the latest overlapping day (returns are
    unchanged). Returns None when the two disagree by more than 2x (an unadjusted
    split) - the caller then re-pulls the whole span."""
    if old is None or len(old) < 200:
        return new
    ov = old.index.intersection(new.index)
    if not len(ov):
        return None if new.index[0] > old.index[-1] + pd.Timedelta(days=10) else \
            pd.concat([old, new[new.index > old.index[-1]]]).sort_index()
    k = float(new.loc[ov[-1], "close"] / old.loc[ov[-1], "close"])
    if not 0.5 < k < 2.0:
        return None
    if abs(k - 1) > 0.005:
        old = old.copy()
        old[["open", "high", "low", "close"]] *= k
    return pd.concat([old[~old.index.isin(new.index)], new]).sort_index()


def _yahoo_frame(h: pd.DataFrame) -> pd.DataFrame:
    d = h.rename(columns=str.lower)
    d = d[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])
    idx = pd.to_datetime(d.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    d.index = idx.normalize()
    d.index.name = "date"
    return d.astype(float)


def yahoo_update_bars(cfg, symbols: List[str], log=print, chunk: int = 50) -> Dict[str, str]:
    """Refresh bars from Yahoo Finance (auto-adjusted), batch-downloaded."""
    try:
        import yfinance as yf
    except ImportError:
        log("  Yahoo fallback unavailable: run  python3 -m pip install yfinance")
        return {s: "error: yfinance not installed" for s in symbols}
    status: Dict[str, str] = {}
    for i in range(0, len(symbols), chunk):
        part = symbols[i:i + chunk]
        ymap = {yahoo_symbol(s): s for s in part}
        try:
            h = yf.download(list(ymap), period="1mo", auto_adjust=True, group_by="ticker",
                            progress=False, threads=True)
        except Exception as e:  # noqa: BLE001
            status.update({s: f"error: {e}" for s in part})
            continue
        for ysym, sym in ymap.items():
            try:
                raw = h[ysym] if isinstance(h.columns, pd.MultiIndex) else h
                new = _yahoo_frame(raw)
                if new.empty:
                    status[sym] = "empty"
                    continue
                old = load_bars(cfg, sym)
                df = _merge_external(old, new)
                if df is None:                    # seam too big: re-pull the span we hold
                    start = old.index[0] if old is not None else None
                    full = yf.Ticker(ysym).history(start=start, auto_adjust=True) if start \
                        is not None else yf.Ticker(ysym).history(period="2y", auto_adjust=True)
                    df = _yahoo_frame(full)
                    if df.empty:
                        status[sym] = "empty"
                        continue
                df.to_parquet(bars_path(cfg, sym))
                status[sym] = "ok"
            except Exception as e:  # noqa: BLE001
                status[sym] = f"error: {e}"
        log(f"  yahoo bars {min(i + chunk, len(symbols))}/{len(symbols)}")
    ok = sum(v == "ok" for v in status.values())
    log(f"  Yahoo Finance: {ok}/{len(symbols)} refreshed")
    return status


STALE_LIMIT = 0.20     # >20% of symbols without a bar for the as-of session = stale pack


def bars_freshness(bars: Dict[str, pd.DataFrame], asof: str) -> dict:
    last = pd.Series({s: b.index[-1] for s, b in bars.items() if len(b)})
    if last.empty:
        return {"asof": asof, "symbols": 0, "stale": 0, "stale_share": 1.0, "is_stale": True}
    a = pd.Timestamp(asof)
    stale = last[last < a].sort_values()
    share = len(stale) / len(last)
    return {"asof": asof, "symbols": int(len(last)), "stale": int(len(stale)),
            "stale_share": round(share, 3), "is_stale": bool(share > STALE_LIMIT),
            "latest_bar": str(last.max().date()),
            "median_last_bar": str(last.sort_values().iloc[len(last) // 2].date()),
            "examples": [f"{s} {d.date()}" for s, d in stale.head(8).items()]}


# --------------------------------------------------------------------------- #
# Fundamentals — SEC EDGAR companyfacts (free, US GAAP, point-in-time filings)
# --------------------------------------------------------------------------- #
GAAP_TAGS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "op_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "assets": ["Assets"],
    "lt_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "shares": ["EntityCommonStockSharesOutstanding",
               "CommonStockSharesOutstanding"],
}
FLOW_ITEMS = {"revenue", "gross_profit", "operating_income", "net_income",
              "op_cash_flow", "capex"}


def fetch_companyfacts(cfg, cik: str, max_age_days: int = 7) -> Optional[dict]:
    path = cfg.data_dir / "fundamentals" / f"{cik}.json"
    if path.exists() and (time.time() - path.stat().st_mtime) < max_age_days * 86400:
        return json.loads(path.read_text())
    import requests
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    r = requests.get(url, headers={"User-Agent": cfg.sec_user_agent}, timeout=30)
    time.sleep(0.15)   # SEC fair-access: <= 10 requests/second
    if r.status_code != 200:
        return None
    path.write_text(r.text)
    return r.json()


PERIODIC_FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A", "10-KT", "10-QT", "20-F", "20-F/A",
                  "40-F", "40-F/A", "6-K"}


def _series(facts: dict, tags: List[str], flow: bool) -> Optional[pd.DataFrame]:
    """Return rows (end, val, filed, fp, form, [start, days]) for a concept, MERGED across
    all its candidate tags, earlier tags winning where two report the same period.

    Bug fixed 2026-10-18: this used to return the first tag that had any data at all. Many
    companies switched revenue tags years ago (AAPL/META/AVGO stopped using `Revenues`
    in 2018, MSFT in 2010), so the screen got a stale revenue figure for 155 of 500 names:
    revenue growth came out as exactly 0% and margins were computed on an old revenue
    (e.g. DXCM operating margin 125%)."""
    parts = []
    for ns in ("us-gaap", "dei"):
        for pri, tag in enumerate(tags):
            node = facts.get("facts", {}).get(ns, {}).get(tag)
            if not node:
                continue
            units = node.get("units", {})
            vals = units.get("USD") or units.get("shares") or next(iter(units.values()), [])
            if not vals:
                continue
            df = pd.DataFrame(vals)
            if "end" not in df:
                continue
            # Only periodic reports. Proxy statements (DEF 14A pay-vs-performance tables)
            # re-tag NetIncomeLoss in thousands/millions and, being filed later, used to
            # override the 10-K figure (e.g. ANET net income 3,511 instead of 3.51bn).
            if "form" in df:
                df = df[df["form"].isin(PERIODIC_FORMS)]
                if df.empty:
                    continue
            df = df.copy()
            df["end"] = pd.to_datetime(df["end"])
            df["filed"] = pd.to_datetime(df["filed"])
            if flow and "start" in df:
                df["start"] = pd.to_datetime(df["start"])
                df["days"] = (df["end"] - df["start"]).dt.days
            df["_pri"] = pri
            parts.append(df)
        if parts:
            break                       # never mix us-gaap with dei for one concept
    if not parts:
        return None
    df = pd.concat(parts, ignore_index=True)
    # One row per period PER FILING (best tag wins within a filing). Keeping every filing
    # matters for point-in-time use: ttm()/instant() first drop filings made after `asof`,
    # then take the latest remaining one. Deduplicating across filings here (as a first
    # version did) let a later filing's row replace the one known at the time.
    key = ["start", "end", "filed"] if (flow and "start" in df.columns) else ["end", "filed"]
    df = df.sort_values(["_pri", "filed"], ascending=[True, False])
    df = df.drop_duplicates(key, keep="first").drop(columns="_pri")
    return df.sort_values(["end", "filed"]).reset_index(drop=True)


def ttm(df: pd.DataFrame, asof: pd.Timestamp) -> Optional[float]:
    """Trailing-twelve-month value known at `asof` (uses filing date => no look-ahead).

    Prefer an annual (~365d) figure ending in the last 100 days; otherwise sum
    the latest four ~quarterly figures; otherwise latest annual.
    """
    d = df[df["filed"] <= asof]
    if d.empty or "days" not in d:
        return None
    d = d.sort_values(["end", "filed"]).drop_duplicates(["start", "end"], keep="last")
    annual = d[(d["days"] > 350) & (d["days"] < 380)]
    q = d[(d["days"] > 80) & (d["days"] < 100)].drop_duplicates("end", keep="last")
    latest_end = d["end"].max()
    if not annual.empty and annual["end"].max() >= latest_end - pd.Timedelta(days=5):
        return float(annual.sort_values("end")["val"].iloc[-1])
    # Most precise: last fiscal year + current year-to-date - prior year-to-date
    ytd_all = d[(d["end"] == latest_end) & (d["days"] > 80) & (d["days"] < 300)]
    if not ytd_all.empty and not annual.empty:
        ytd = ytd_all.sort_values("days").iloc[-1]
        fy = annual[(annual["end"] - (ytd["start"] - pd.Timedelta(days=1))).abs() <= pd.Timedelta(days=5)]
        prior = d[((d["days"] - ytd["days"]).abs() <= 7)
                  & ((ytd["end"] - d["end"]).dt.days.between(355, 375))]
        if not fy.empty and not prior.empty:
            return float(fy.sort_values("end")["val"].iloc[-1] + ytd["val"]
                         - prior.sort_values("end")["val"].iloc[-1])
    if len(q) >= 4:
        last4 = q.sort_values("end").tail(4)
        if (last4["end"].iloc[-1] - last4["end"].iloc[0]).days < 300:
            return float(last4["val"].sum())
    if not annual.empty:
        return float(annual.sort_values("end")["val"].iloc[-1])
    return None


def ttm_year_ago(df: pd.DataFrame, asof: pd.Timestamp) -> Optional[float]:
    return ttm(df[df["end"] <= asof - pd.Timedelta(days=330)], asof)


def instant(df: pd.DataFrame, asof: pd.Timestamp) -> Optional[float]:
    d = df[df["filed"] <= asof]
    if d.empty:
        return None
    return float(d.sort_values(["end", "filed"])["val"].iloc[-1])


def fundamentals_row(facts: dict, asof: Optional[pd.Timestamp] = None) -> dict:
    asof = asof or pd.Timestamp.now()
    v = {}
    for k, tags in GAAP_TAGS.items():
        s = _series(facts, tags, k in FLOW_ITEMS)
        if s is None:
            v[k] = None
            continue
        v[k] = ttm(s, asof) if k in FLOW_ITEMS else instant(s, asof)
        if k == "revenue":
            v["revenue_1y_ago"] = ttm_year_ago(s, asof)
        if k == "net_income":
            v["net_income_1y_ago"] = ttm_year_ago(s, asof)
        if k == "shares":
            d = s[s["filed"] <= asof].sort_values("end")
            old = d[d["end"] <= asof - pd.Timedelta(days=330)]
            v["shares_1y_ago"] = float(old["val"].iloc[-1]) if len(old) else None

    def ratio(a, b):
        return (a / b) if (a is not None and b not in (None, 0)) else None

    out = {
        "revenue_ttm": v.get("revenue"),
        "revenue_growth": (ratio(v.get("revenue"), v.get("revenue_1y_ago")) - 1)
        if ratio(v.get("revenue"), v.get("revenue_1y_ago")) is not None else None,
        "gross_margin": ratio(v.get("gross_profit"), v.get("revenue")),
        "operating_margin": ratio(v.get("operating_income"), v.get("revenue")),
        "net_income_ttm": v.get("net_income"),
        "eps_growth_proxy": (ratio(v.get("net_income"), v.get("net_income_1y_ago")) - 1)
        if (v.get("net_income_1y_ago") or 0) > 0 and v.get("net_income") is not None
        else None,
        "roe": ratio(v.get("net_income"), v.get("equity")) if (v.get("equity") or 0) > 0
        else None,
        "roa": ratio(v.get("net_income"), v.get("assets")),
        "fcf_ttm": (v["op_cash_flow"] - (v.get("capex") or 0))
        if v.get("op_cash_flow") is not None else None,
        "debt_to_equity": ratio(v.get("lt_debt"), v.get("equity"))
        if (v.get("equity") or 0) > 0 else None,
        "shares_out": v.get("shares"),
        "share_change_1y": (ratio(v.get("shares"), v.get("shares_1y_ago")) - 1)
        if ratio(v.get("shares"), v.get("shares_1y_ago")) is not None else None,
    }
    return out


def update_fundamentals(cfg, universe: pd.DataFrame, log=print) -> pd.DataFrame:
    rows = []
    for i, r in enumerate(universe.itertuples()):
        try:
            facts = fetch_companyfacts(cfg, r.CIK)
            row = fundamentals_row(facts) if facts else {}
        except Exception as e:  # noqa: BLE001
            row = {"error": str(e)}
        row["symbol"] = r.ib_symbol
        rows.append(row)
        if i % 100 == 99:
            log(f"  fundamentals {i + 1}/{len(universe)}")
    df = pd.DataFrame(rows).set_index("symbol")
    df.to_parquet(cfg.data_dir / "fundamentals.parquet")
    return df


# --------------------------------------------------------------------------- #
# Macro — FRED (no API key needed for the CSV endpoint)
# --------------------------------------------------------------------------- #
FRED_SERIES = {
    "DGS10": "10Y Treasury yield (%)",
    "DGS2": "2Y Treasury yield (%)",
    "T10Y2Y": "10Y-2Y curve (%)",
    "DFF": "Fed funds effective (%)",
    "BAMLH0A0HYM2": "High-yield credit spread (%)",
    "VIXCLS": "VIX close",
    "ICSA": "Initial jobless claims",
    "UNRATE": "Unemployment rate (%)",
    "CPIAUCSL": "CPI index",
    "NFCI": "Chicago Fed financial conditions (neg = loose)",
    "DCOILWTICO": "WTI crude ($)",
    "DTWEXBGS": "Broad USD index",
}


FRED_UA = {"User-Agent": "Mozilla/5.0 (FundHouse research; contact "
                         + os.environ.get("FH_CONTACT_EMAIL", "you@example.com") + ")",
           "Accept": "text/csv,text/plain;q=0.9,*/*;q=0.5"}


def _fred_fetch(sid: str, attempts: int = 3, timeout: int = 30) -> pd.Series:
    """Download one FRED series as CSV, validating that the body really is CSV.

    FRED occasionally answers with an HTML page (maintenance / bot check / error) and a
    200 status; pandas then fails with 'Error tokenizing data'. We check status and the
    header line, retry with backoff, and raise with a snippet of the body so the log
    shows *what* FRED actually returned.
    """
    import requests
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
    last_err = None
    for i in range(attempts):
        try:
            r = requests.get(url, headers=FRED_UA, timeout=timeout)
            body = r.text.lstrip("\ufeff")
            head = body.splitlines()[0].strip() if body else ""
            if r.status_code != 200:
                raise ValueError(f"HTTP {r.status_code}")
            if "," not in head or sid.lower() not in head.lower():
                raise ValueError("not CSV")
            s = pd.read_csv(io.StringIO(body))
            s.columns = ["date", sid]
            s["date"] = pd.to_datetime(s["date"], errors="coerce")
            s[sid] = pd.to_numeric(s[sid], errors="coerce")
            s = s.dropna(subset=["date"]).set_index("date")[sid]
            if s.dropna().empty:
                raise ValueError("no numeric observations")
            return s
        except Exception as e:  # noqa: BLE001
            snippet = ""
            try:
                snippet = " | ctype=%s body=%r" % (r.headers.get("Content-Type"),
                                                   r.text[:160])
            except Exception:  # noqa: BLE001
                pass
            last_err = f"{e}{snippet}"
            if i < attempts - 1:
                time.sleep(5 * (i + 1))
    raise RuntimeError(f"{attempts} attempts failed: {last_err}")


def update_macro(cfg, log=print) -> pd.DataFrame:
    """Refresh FRED series. A series that fails keeps its cached history, so one bad
    response never wipes data, and the cache is never overwritten with an empty frame."""
    mp = cfg.data_dir / "macro.parquet"
    cache = pd.read_parquet(mp) if mp.exists() else pd.DataFrame()
    frames, failed = {}, []
    for sid in FRED_SERIES:
        try:
            frames[sid] = _fred_fetch(sid)
        except Exception as e:  # noqa: BLE001
            failed.append(sid)
            log(f"  FRED {sid} failed: {e}")
            if sid in cache:
                frames[sid] = cache[sid].dropna()
    if len(failed) == len(FRED_SERIES):
        raise RuntimeError(f"all {len(failed)} FRED series failed; cache left untouched")
    df = pd.DataFrame(frames).sort_index()
    df.index = pd.to_datetime(df.index)
    df = df[df.index >= pd.Timestamp.now() - pd.Timedelta(days=365 * 6)]
    df.to_parquet(mp)
    if failed:
        log(f"  FRED: {len(failed)}/{len(FRED_SERIES)} series kept from cache: {failed}")
    df.attrs["fred_failed"] = failed
    return df


def macro_summary(df: pd.DataFrame) -> List[dict]:
    """Latest value plus 1M / 3M / 12M change for each series."""
    out = []
    for sid, label in FRED_SERIES.items():
        if sid not in df:
            continue
        s = df[sid].dropna()
        if s.empty:
            continue
        last_date = s.index[-1]

        def at(days):
            prev = s[s.index <= last_date - pd.Timedelta(days=days)]
            return prev.iloc[-1] if len(prev) else np.nan
        row = {"series": sid, "label": label, "date": str(last_date.date()),
               "last": round(float(s.iloc[-1]), 3)}
        for tag, d in (("chg_1m", 30), ("chg_3m", 91), ("chg_12m", 365)):
            p = at(d)
            if sid in ("CPIAUCSL", "ICSA", "DCOILWTICO", "DTWEXBGS"):
                row[tag] = round(float(s.iloc[-1] / p - 1) * 100, 2) if p == p else None
                row["chg_unit"] = "%"
            else:
                row[tag] = round(float(s.iloc[-1] - p), 3) if p == p else None
                row["chg_unit"] = "abs"
        out.append(row)
    return out


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
