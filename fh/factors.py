"""Quant screen + market regime. Deterministic, backtestable, no LLM.

The screen ranks the universe on four families (cross-sectional percentiles):
  momentum  (12-1 month return, 6-month return)       — the best-documented equity factor
  trend     (price vs 50/200-day averages, 52w-high proximity)
  quality   (ROE, margins, FCF, low leverage, no dilution)
  growth    (revenue growth, earnings growth)
minus a penalty for very high volatility. The agents may only choose from the
top `candidate_pool` names of this list.
"""
from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd

WEIGHTS = {"momentum": 0.35, "trend": 0.20, "quality": 0.25, "growth": 0.20}


def technicals(df: pd.DataFrame) -> dict:
    c = df["close"]
    if len(c) < 210:
        return {}
    ret = lambda n: float(c.iloc[-1] / c.iloc[-1 - n] - 1) if len(c) > n else np.nan  # noqa: E731
    sma50, sma200 = c.rolling(50).mean(), c.rolling(200).mean()
    hi52 = c.tail(252).max()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - c.shift()).abs(),
                    (df["low"] - c.shift()).abs()], axis=1).max(axis=1)
    atr20 = tr.rolling(20).mean().iloc[-1]
    delta = c.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / 14).mean()
    rsi = 100 - 100 / (1 + up.iloc[-1] / dn.iloc[-1]) if dn.iloc[-1] > 0 else 100.0
    mom_12_1 = float(c.iloc[-22] / c.iloc[-253] - 1) if len(c) > 253 else np.nan
    return {
        "price": float(c.iloc[-1]),
        "ret_1m": ret(21), "ret_3m": ret(63), "ret_6m": ret(126),
        "mom_12_1": mom_12_1,
        "above_sma50": bool(c.iloc[-1] > sma50.iloc[-1]),
        "above_sma200": bool(c.iloc[-1] > sma200.iloc[-1]),
        "sma50_above_sma200": bool(sma50.iloc[-1] > sma200.iloc[-1]),
        "sma200_slope_1m": float(sma200.iloc[-1] / sma200.iloc[-22] - 1),
        "dist_52w_high": float(c.iloc[-1] / hi52 - 1),
        "vol_63d": float(c.pct_change().tail(63).std() * np.sqrt(252)),
        "atr20_pct": float(atr20 / c.iloc[-1]),
        "rsi14": float(rsi),
        "dollar_vol_20d": float((c * df["volume"]).tail(20).mean()),
    }


def _pct(s: pd.Series, higher_better: bool = True) -> pd.Series:
    r = s.rank(pct=True, na_option="keep")
    r = r if higher_better else 1 - r + 1 / max(s.notna().sum(), 1)
    return r.fillna(0.5)   # missing data = neutral, not rewarded


def screen(bars: Dict[str, pd.DataFrame], fundamentals: pd.DataFrame,
           universe: pd.DataFrame, cfg) -> pd.DataFrame:
    tech = pd.DataFrame({s: technicals(df) for s, df in bars.items()}).T
    tech = tech.dropna(subset=["price"]) if "price" in tech else tech
    if tech.empty:
        return tech
    meta = universe.set_index("ib_symbol")[["name", "sector", "industry"]]
    df = tech.join(meta, how="left").join(fundamentals, how="left")
    def num(col):           # a missing column (e.g. no fundamentals feed) is all-NaN = neutral
        if col not in df.columns:
            return pd.Series(np.nan, index=df.index, dtype=float)
        return pd.to_numeric(df[col], errors="coerce")

    # eligibility
    df["eligible"] = ((num("price") >= cfg.min_price)
                      & (num("dollar_vol_20d") >= cfg.min_dollar_volume))

    # valuation where we can compute it (for agents; not in the score by default)
    mcap = num("price") * num("shares_out")
    df["market_cap"] = mcap
    pe = np.where(num("net_income_ttm") > 0, mcap / num("net_income_ttm"), np.nan)
    df["pe_ttm"] = np.where((pe > 0) & (pe < 1000), pe, np.nan)   # outside = data error / n.m.
    df["fcf_yield"] = num("fcf_ttm") / mcap
    # Markets whose statements are in another currency (HK: often CNY/USD) supply these
    # already converted to the trading currency; they win over the local computation.
    for col in ("market_cap", "pe_ttm", "fcf_yield"):
        ext = col + "_ext"
        if ext in df.columns:
            v = num(ext)
            df[col] = v.where(v.notna(), df[col])

    fam = pd.DataFrame(index=df.index)
    fam["momentum"] = (_pct(num("mom_12_1")) + _pct(num("ret_6m"))) / 2
    fam["trend"] = (df["above_sma200"].astype(float) * 0.4
                    + df["sma50_above_sma200"].astype(float) * 0.3
                    + _pct(num("dist_52w_high")) * 0.3)
    fam["quality"] = (_pct(num("roe")) + _pct(num("operating_margin"))
                      + _pct(num("fcf_yield")) + _pct(num("debt_to_equity"), False)
                      + _pct(num("share_change_1y"), False)) / 5
    fam["growth"] = (_pct(num("revenue_growth")) + _pct(num("eps_growth_proxy"))) / 2
    score = sum(fam[k] * w for k, w in WEIGHTS.items())
    score -= 0.10 * (_pct(num("vol_63d")) > 0.90).astype(float)   # lottery-ticket penalty
    df = df.join(fam.add_prefix("f_"))
    df["score"] = score
    df.loc[~df["eligible"].fillna(False).astype(bool), "score"] = np.nan
    df = df.sort_values("score", ascending=False)
    df["rank"] = np.arange(1, len(df) + 1)
    return df


# --------------------------------------------------------------------------- #
# Regime (no ETFs => breadth & equal-weight index built from the universe)
# --------------------------------------------------------------------------- #
def ew_daily_returns(bars: Dict[str, pd.DataFrame]) -> pd.Series:
    """Equal-weight average of each stock's daily return (one value per session)."""
    closes = pd.DataFrame({s: d["close"] for s, d in bars.items()}).sort_index()
    rets = closes.pct_change(fill_method=None).clip(-0.5, 0.5)
    return rets.mean(axis=1, skipna=True).fillna(0)


def chained_ew_level(bars: Dict[str, pd.DataFrame], prev_date: str | None,
                     prev_level: float | None, asof: str) -> float:
    """Benchmark level for `asof`, chained from the previous recorded level.

    Only the sessions after prev_date (up to asof) move it, so changing how much
    price HISTORY is cached can never move the benchmark (bug fixed 2026-10-15:
    the level used to be a cumulative product from the first cached bar, and
    downloading 12 years of history multiplied it by 4.3x overnight)."""
    if prev_date is None or prev_level is None:
        return 1.0
    r = ew_daily_returns(bars)
    win = r[(r.index > pd.Timestamp(prev_date)) & (r.index <= pd.Timestamp(asof))]
    return float(prev_level * (1 + win).prod())


def equal_weight_index(bars: Dict[str, pd.DataFrame]) -> pd.Series:
    closes = pd.DataFrame({s: d["close"] for s, d in bars.items()})
    rets = closes.pct_change(fill_method=None).clip(-0.5, 0.5)
    return (1 + rets.mean(axis=1, skipna=True).fillna(0)).cumprod()


def market_regime(bars: Dict[str, pd.DataFrame], macro: pd.DataFrame | None,
                  cfg) -> dict:
    ew = equal_weight_index(bars)
    closes = pd.DataFrame({s: d["close"] for s, d in bars.items()})
    sma200 = closes.rolling(200).mean()
    breadth = float((closes.iloc[-1] > sma200.iloc[-1]).mean())
    # 10-month (~210d) trend on the equal-weight index: the rule that held
    # S&P drawdowns under ~20% since 1950 in our monthly stress test.
    ew_trend_up = bool(ew.iloc[-1] > ew.rolling(210).mean().iloc[-1])
    ew_dd = float(ew.iloc[-1] / ew.cummax().iloc[-1] - 1)
    ew_ret_1m = float(ew.iloc[-1] / ew.iloc[-22] - 1) if len(ew) > 22 else 0.0

    credit_widening = None
    vix = None
    if macro is not None and "BAMLH0A0HYM2" in macro:
        hy = macro["BAMLH0A0HYM2"].dropna()
        if len(hy) > 70:
            credit_widening = bool(hy.iloc[-1] - hy.iloc[-64] > 1.0)   # +100bp in ~3m
    if macro is not None and "VIXCLS" in macro:
        v = macro["VIXCLS"].dropna()
        vix = float(v.iloc[-1]) if len(v) else None

    points = 0
    points += 2 if ew_trend_up else -2
    points += 1 if breadth > 0.60 else (-1 if breadth < 0.40 else 0)
    points += -1 if credit_widening else 0
    points += -1 if (vix is not None and vix > 30) else 0
    regime = "risk_on" if points >= 2 else ("risk_off" if points <= -2 else "neutral")
    return {
        "regime": regime, "score": points,
        "exposure_cap": cfg.regime_caps[regime],
        "ew_index_trend_up": ew_trend_up, "breadth_above_200d": round(breadth, 3),
        "ew_index_drawdown": round(ew_dd, 4), "ew_index_ret_1m": round(ew_ret_1m, 4),
        "hy_spread_widening_3m": credit_widening, "vix": vix,
        "asof": str(closes.index[-1].date()),
    }


def sector_table(scr: pd.DataFrame) -> pd.DataFrame:
    """Sector momentum/breadth from constituents (ETF substitutes)."""
    g = scr.groupby("sector")
    num = lambda c: pd.to_numeric(scr[c], errors="coerce")  # noqa: E731
    scr = scr.assign(_r3=num("ret_3m"), _r6=num("ret_6m"),
                     _a200=scr["above_sma200"].astype(float), _sc=num("score"))
    g = scr.groupby("sector")
    t = pd.DataFrame({
        "names": g.size(),
        "median_ret_3m": g["_r3"].median(),
        "median_ret_6m": g["_r6"].median(),
        "pct_above_200d": g["_a200"].mean(),
        "median_score": g["_sc"].median(),
        "in_top40": g["rank"].apply(lambda r: int((r <= 40).sum())),
    }).sort_values("median_ret_6m", ascending=False)
    return t.round(3)
