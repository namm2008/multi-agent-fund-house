"""Backtest the deterministic core (screen + regime cap + drawdown ladder + stops).

The LLM committee CANNOT be backtested honestly (models have read the future),
but this quant backbone can. Run it before trusting anything.

  python scripts/download_history.py --years 12      # once, via IB Gateway
  python scripts/backtest_core.py --start 2015-01-01

CAVEAT — survivorship bias: the universe is TODAY's S&P 500. Names that were
dropped or went bust are missing, which flatters results (literature: roughly
1–3%/yr for momentum-style US large-cap tests). Treat the output as an upper bound.
Fundamentals use SEC filing dates (point-in-time), so no look-ahead there.
"""
import _boot  # noqa: F401
import argparse

import numpy as np
import pandas as pd

from fh.config import CONFIG as cfg
CFG_DEFAULT = cfg
RESETS: list = []          # dates the backtest restarted after a hard-floor halt
from fh import data, factors
from fh.ledger import drawdown_cap


def run(bars, universe, fundamentals_at, start, end, cost_bps=10.0, use_fund=True,
        top_n=12, log=print, cfg_=None, restart_after_halt=True):
    """restart_after_halt: the live fund stays in cash after the -15% hard floor until a
    human runs reset_hwm.py. The backtest models that human decision as "restart (new
    high-water mark) at the first month-end when the market regime is risk_on again";
    without it one early halt would leave the backtest in cash for ever."""
    cfg = cfg_ or CFG_DEFAULT
    RESETS.clear()
    closes = pd.DataFrame({s: d["close"] for s, d in bars.items()}).sort_index()
    closes = closes[closes.index <= end]
    dates = closes.index[closes.index >= start]
    month_ends = pd.Series(dates, index=dates).groupby([dates.year, dates.month]).last()
    nav, hwm, cash = 1.0, 1.0, 1.0
    qty, entry, peak = {}, {}, {}
    rets = closes.pct_change(fill_method=None)
    curve, expo = [], []
    fund_cache = {}
    for d in dates:
        px = closes.loc[d]
        # mark
        mv = sum(q * px.get(s, np.nan) for s, q in qty.items() if px.get(s) == px.get(s))
        nav = cash + mv
        hwm = max(hwm, nav)
        dd = nav / hwm - 1
        for s in qty:
            if px.get(s) == px.get(s):
                peak[s] = max(peak[s], px[s])
        # daily stops + ladder
        cap_l = drawdown_cap(dd, cfg.dd_ladder)
        for s in list(qty):
            p = px.get(s)
            if p != p:
                continue
            if p / entry[s] - 1 <= -cfg.stop_loss_from_entry or \
               (peak[s] > entry[s] and p / peak[s] - 1 <= -cfg.trailing_stop_from_peak):
                cash += qty[s] * p * (1 - cost_bps / 1e4)
                del qty[s], entry[s], peak[s]
        mv = sum(q * px[s] for s, q in qty.items() if px.get(s) == px.get(s))
        if nav > 0 and mv / nav > cap_l + 0.02:
            k = cap_l * nav / mv
            for s in list(qty):
                sell = qty[s] * (1 - k)
                cash += sell * px[s] * (1 - cost_bps / 1e4)
                qty[s] -= sell
                if qty[s] <= 1e-12:
                    del qty[s], entry[s], peak[s]
        # monthly rebalance
        if d in set(month_ends.values):
            hist = {s: b[b.index <= d] for s, b in bars.items()}
            hist = {s: h for s, h in hist.items() if len(h) > 260}
            if len(hist) < 30:
                curve.append((d, nav)); expo.append(0); continue
            key = (d.year, d.month)
            if use_fund and fundamentals_at is not None:
                fund_cache[key] = fundamentals_at(d)
            fund = fund_cache.get(key, pd.DataFrame())
            scr = factors.screen(hist, fund, universe, cfg)
            reg = factors.market_regime(hist, None, cfg)
            if restart_after_halt and cap_l == 0.0 and reg["regime"] == "risk_on":
                hwm = nav
                cap_l = 1.0
                RESETS.append(str(d.date()))
            cap = min(reg["exposure_cap"], cap_l, 1 - cfg.cash_buffer)
            picks, secs = [], {}
            for s, row in scr.dropna(subset=["score"]).iterrows():
                if secs.get(row["sector"], 0) >= max(1, int(cfg.max_sector_weight * top_n)):
                    continue
                picks.append(s); secs[row["sector"]] = secs.get(row["sector"], 0) + 1
                if len(picks) == top_n:
                    break
            target = {s: cap / len(picks) for s in picks} if picks and cap > 0 else {}
            nav = cash + sum(q * px[s] for s, q in qty.items() if px.get(s) == px.get(s))
            new_qty = {s: w * nav / px[s] for s, w in target.items() if px.get(s) == px.get(s)}
            traded = sum(abs(new_qty.get(s, 0) - qty.get(s, 0)) * px.get(s, 0)
                         for s in set(new_qty) | set(qty) if px.get(s) == px.get(s))
            cash = nav - sum(q * px[s] for s, q in new_qty.items()) - traded * cost_bps / 1e4
            for s in new_qty:
                if s not in qty:
                    entry[s], peak[s] = px[s], px[s]
            for s in list(entry):
                if s not in new_qty:
                    entry.pop(s); peak.pop(s)
            qty = new_qty
        mv = sum(q * px[s] for s, q in qty.items() if px.get(s) == px.get(s))
        curve.append((d, cash + mv)); expo.append(mv / (cash + mv) if cash + mv > 0 else 0)
    eq = pd.Series(dict(curve))
    ew = (1 + rets.loc[eq.index].mean(axis=1).fillna(0)).cumprod()
    return eq, pd.Series(expo, index=eq.index), ew


def stats(eq):
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    r = eq.pct_change().dropna()
    return {"CAGR": eq.iloc[-1] ** (1 / yrs) - 1, "MaxDD": (eq / eq.cummax() - 1).min(),
            "Vol": r.std() * np.sqrt(252), "Sharpe(rf=0)": r.mean() / r.std() * np.sqrt(252)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default=pd.Timestamp.now().strftime("%Y-%m-%d"))
    ap.add_argument("--no-fundamentals", action="store_true")
    ap.add_argument("--cost-bps", type=float, default=10.0)
    a = ap.parse_args()
    uni = data.load_universe(cfg)
    bars = data.load_all_bars(cfg, uni["ib_symbol"].tolist())
    print(f"{len(bars)} symbols with bars")
    facts = {}
    if not a.no_fundamentals:
        for r in uni.itertuples():
            p = cfg.data_dir / "fundamentals" / f"{r.CIK}.json"
            if p.exists():
                facts[r.ib_symbol] = __import__("json").loads(p.read_text())

    def fundamentals_at(d):
        return pd.DataFrame({s: data.fundamentals_row(f, d) for s, f in facts.items()}).T

    eq, ex, ew = run(bars, uni, fundamentals_at if facts else None,
                     pd.Timestamp(a.start), pd.Timestamp(a.end), a.cost_bps)
    out = pd.DataFrame({"Core strategy": stats(eq), "Equal-weight universe (B&H)": stats(ew)}).T
    print(out.to_string(float_format=lambda x: f"{x:.2%}" if abs(x) < 5 else f"{x:.2f}"))
    print(f"average gross exposure {ex.mean():.0%}")
    yr = eq.resample("YE").last().pct_change()
    yr.iloc[0] = eq.resample("YE").last().iloc[0] / eq.iloc[0] - 1
    print("\nCalendar-year returns:\n", yr.map(lambda x: f"{x:+.1%}").to_string())
    cfg.research_dir.mkdir(exist_ok=True)
    pd.DataFrame({"core": eq, "ew_universe": ew, "exposure": ex}).to_csv(
        cfg.research_dir / "backtest_core.csv")
    print("\nSaved research/backtest_core.csv.  REMEMBER: survivorship-biased upper bound.")


if __name__ == "__main__":
    main()
