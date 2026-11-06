"""Research pack: the single source of numbers for the agent committee.

Written to research/<date>/pack.json (+ a readable pack.md). Agents cite it;
anything not in the pack must come from a cited web source.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Dict

import numpy as np
import pandas as pd

from fh.ledger import Ledger
from fh.risk import RiskCaps

CAND_COLS = ["name", "sector", "industry", "score", "f_momentum", "f_trend", "f_quality",
             "f_growth", "price", "ret_1m", "ret_3m", "ret_6m", "mom_12_1", "dist_52w_high",
             "above_sma200", "sma50_above_sma200", "rsi14", "vol_63d", "atr20_pct",
             "market_cap", "pe_ttm", "fcf_yield", "revenue_growth", "eps_growth_proxy",
             "gross_margin", "operating_margin", "roe", "debt_to_equity", "share_change_1y"]


def _clean(v):
    if isinstance(v, (np.floating, float)):
        return None if not np.isfinite(v) else round(float(v), 4)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    return v


def records(df: pd.DataFrame, cols) -> list:
    cols = [c for c in cols if c in df.columns]
    out = []
    for sym, row in df[cols].iterrows():
        r = {"symbol": sym}
        r.update({c: _clean(row[c]) for c in cols})
        out.append(r)
    return out


def shadow_portfolio(screen: pd.DataFrame, cap: float, cfg) -> Dict[str, float]:
    """The quant-only benchmark: top names equal-weighted, sector-capped,
    scaled to the same exposure cap. The committee must beat THIS to justify itself."""
    n = cfg.target_names[1]
    picks, sec_count = [], {}
    max_per_sector = max(1, int(cfg.max_sector_weight * n))
    for s, row in screen.dropna(subset=["score"]).iterrows():
        sec = row.get("sector", "?")
        if sec_count.get(sec, 0) >= max_per_sector:
            continue
        picks.append(s)
        sec_count[sec] = sec_count.get(sec, 0) + 1
        if len(picks) == n:
            break
    if not picks:
        return {}
    w = min(cap, 1 - cfg.cash_buffer) / len(picks)
    return {s: round(w, 4) for s in picks}


def build_pack(cfg, asof: str, screen: pd.DataFrame, sectors: pd.DataFrame,
               regime: dict, macro_rows: list, ledger: Ledger, prices: Dict[str, float],
               caps: RiskCaps, nav_hist: pd.DataFrame | None) -> dict:
    cand = screen.dropna(subset=["score"]).head(cfg.candidate_pool)
    holdings = []
    w = ledger.weights(prices)
    for s, p in ledger.positions.items():
        px = prices.get(s, p.avg_cost)
        row = screen.loc[s] if s in screen.index else {}
        holdings.append({
            "symbol": s, "qty": p.qty, "avg_cost": round(p.avg_cost, 2), "price": px,
            "weight": round(w.get(s, 0), 4), "pnl_pct": round(px / p.avg_cost - 1, 4),
            "entry_date": p.entry_date,
            "from_peak_close": round(px / p.peak_close - 1, 4),
            "stop_from_entry_at": round(p.avg_cost * (1 - cfg.stop_loss_from_entry), 2),
            "screen_rank": _clean(row.get("rank")) if len(row) else None,
            "screen_score": _clean(row.get("score")) if len(row) else None,
            "sector": p.sector or (row.get("sector") if len(row) else ""),
        })
    nav = ledger.nav(prices)
    pack = {
        "asof": asof,
        "mandate": {
            "fund": cfg.fund_name, "market": "US single stocks (ETFs barred: PRIIPs)",
            "style": "mid-to-long-term; holding period months; weekly review",
            "starting_capital": cfg.starting_capital,
            "max_drawdown_mandate": cfg.max_drawdown_mandate,
            "drawdown_ladder": cfg.dd_ladder, "regime_caps": cfg.regime_caps,
            "names": cfg.target_names, "weight_range": [cfg.min_weight, cfg.max_weight],
            "max_sector_weight": cfg.max_sector_weight,
            "candidate_pool": cfg.candidate_pool,
            "max_monthly_turnover": cfg.max_monthly_turnover,
            "stops": {"from_entry": cfg.stop_loss_from_entry,
                      "trailing_from_peak": cfg.trailing_stop_from_peak,
                      "server_side_gtc": cfg.server_side_stop},
        },
        "fund": {
            "nav": round(nav, 2), "cash": round(ledger.cash, 2),
            "gross_exposure": round(ledger.gross_exposure(prices), 4),
            "hwm": round(ledger.hwm, 2), "drawdown": round(ledger.drawdown(prices), 4),
            "since_inception": round(nav / cfg.starting_capital - 1, 4),
            "halted": ledger.halted, "halt_reason": ledger.halt_reason,
            "buys_frozen_until": ledger.buys_frozen_until,
            "holdings": holdings,
        },
        "risk_caps": dict(asdict(caps), gross_cap=caps.gross_cap),
        "regime": regime,
        "macro": macro_rows,
        "sectors": sectors.reset_index().to_dict(orient="records"),
        "candidates": records(cand, CAND_COLS + ["rank"]),
        "shadow_quant_portfolio": shadow_portfolio(screen, caps.gross_cap, cfg),
        "nav_history_tail": nav_hist.tail(30).to_dict(orient="records")
        if nav_hist is not None and len(nav_hist) else [],
    }
    return json.loads(json.dumps(pack, default=_clean))


def pack_markdown(p: dict) -> str:
    f, r = p["fund"], p["regime"]
    L = [f"# Research pack — {p['asof']}", "",
         f"**NAV** ${f['nav']:,.2f} | cash ${f['cash']:,.2f} | gross {f['gross_exposure']:.0%} | "
         f"drawdown {f['drawdown']:.1%} (HWM ${f['hwm']:,.2f}) | since inception "
         f"{f['since_inception']:+.1%}", "",
         f"**Regime (code):** {r['regime']} (score {r['score']}) → exposure cap "
         f"{r['exposure_cap']:.0%}; breadth {r['breadth_above_200d']:.0%} above 200d; "
         f"EW-index trend up: {r['ew_index_trend_up']}; VIX {r['vix']}", "",
         f"**Binding gross cap:** {p['risk_caps']['gross_cap']:.0%} "
         f"(ladder {p['risk_caps']['ladder_cap']:.0%}, halted {p['risk_caps']['halted']}, "
         f"buys frozen {p['risk_caps']['buys_frozen']})", "", "## Macro (FRED)", "",
         "| Series | Last | 1M | 3M | 12M |", "|---|---|---|---|---|"]
    for m in p["macro"]:
        u = "%" if m.get("chg_unit") == "%" else ""
        L.append(f"| {m['label']} | {m['last']} | {m.get('chg_1m')}{u} | "
                 f"{m.get('chg_3m')}{u} | {m.get('chg_12m')}{u} |")
    L += ["", "## Sectors (built from constituents)", "",
          "| Sector | 3M med | 6M med | % >200d | top-40 names |", "|---|---|---|---|---|"]
    for s in p["sectors"]:
        L.append(f"| {s['sector']} | {s['median_ret_3m']:.1%} | {s['median_ret_6m']:.1%} | "
                 f"{s['pct_above_200d']:.0%} | {s['in_top40']} |")
    L += ["", "## Holdings", ""]
    if f["holdings"]:
        L += ["| Sym | Wt | P&L | From peak | Rank |", "|---|---|---|---|---|"]
        for h in f["holdings"]:
            L.append(f"| {h['symbol']} | {h['weight']:.1%} | {h['pnl_pct']:+.1%} | "
                     f"{h['from_peak_close']:.1%} | {h['screen_rank']} |")
    else:
        L.append("_All cash._")
    L += ["", f"## Candidate pool (top {len(p['candidates'])})", "",
          "| # | Sym | Sector | Score | Mom12-1 | 6M | PE | RevG | ROE | Vol |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    fmt = lambda v, s="{:.0%}": "–" if v is None else s.format(v)  # noqa: E731
    for c in p["candidates"]:
        L.append(f"| {c['rank']} | {c['symbol']} | {c.get('sector', '')} | "
                 f"{fmt(c['score'], '{:.2f}')} | {fmt(c.get('mom_12_1'))} | "
                 f"{fmt(c.get('ret_6m'))} | {fmt(c.get('pe_ttm'), '{:.0f}')} | "
                 f"{fmt(c.get('revenue_growth'))} | {fmt(c.get('roe'))} | "
                 f"{fmt(c.get('vol_63d'))} |")
    L += ["", "## Shadow quant portfolio (benchmark the committee must beat)", "",
          ", ".join(f"{s} {w:.1%}" for s, w in p["shadow_quant_portfolio"].items()) or "_none_"]
    return "\n".join(L) + "\n"
