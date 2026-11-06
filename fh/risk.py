"""Hard risk rules. Code, not prompts: an LLM can argue with a prompt, not with this.

validate_target() is the Risk Manager's veto. It clips what can be clipped
mechanically, drops what breaks the mandate, and reports every change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

import pandas as pd

from fh.ledger import Ledger, drawdown_cap


@dataclass
class RiskCaps:
    regime_cap: float
    ladder_cap: float
    halted: bool
    buys_frozen: bool

    @property
    def gross_cap(self) -> float:
        return 0.0 if self.halted else min(self.regime_cap, self.ladder_cap)


def current_caps(cfg, ledger: Ledger, prices: Dict[str, float], regime: dict,
                 today: Optional[str] = None) -> RiskCaps:
    today = today or date.today().isoformat()
    dd = ledger.drawdown(prices)
    frozen = bool(ledger.buys_frozen_until and today <= ledger.buys_frozen_until)
    return RiskCaps(regime_cap=float(regime.get("exposure_cap", 0.0)),
                    ladder_cap=drawdown_cap(dd, cfg.dd_ladder),
                    halted=ledger.halted or cfg.kill_switch.exists(),
                    buys_frozen=frozen)


@dataclass
class Validation:
    ok: bool
    target: Dict[str, float]
    changes: List[str] = field(default_factory=list)
    violations: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def validate_target(proposal: dict, screen: pd.DataFrame, ledger: Ledger,
                    prices: Dict[str, float], caps: RiskCaps, cfg) -> Validation:
    v = Validation(ok=True, target={})
    held = set(ledger.positions)
    pool = set(screen.dropna(subset=["score"]).head(cfg.candidate_pool).index)
    sectors = screen["sector"].to_dict() if "sector" in screen else {}

    raw = {}
    for h in proposal.get("holdings", []):
        s, w = str(h.get("symbol", "")).upper().strip(), float(h.get("weight", 0))
        if w <= 0:
            continue
        if s not in screen.index:
            v.violations.append(f"{s}: not in the stock universe (ETFs/unknowns barred) — dropped")
            continue
        if s not in pool and s not in held:
            v.violations.append(f"{s}: not in top-{cfg.candidate_pool} screen and not held — dropped")
            continue
        if s not in pool and s in held:
            cur = ledger.weights(prices).get(s, 0)
            if w > cur + 1e-4:
                v.changes.append(f"{s}: fell out of screen, cannot be ADDED to; held at {cur:.1%}")
                w = cur
        raw[s] = w

    # per-name cap
    for s in list(raw):
        if raw[s] > cfg.max_weight:
            v.changes.append(f"{s}: {raw[s]:.1%} clipped to max {cfg.max_weight:.0%}")
            raw[s] = cfg.max_weight
        elif raw[s] < cfg.min_weight:
            v.changes.append(f"{s}: {raw[s]:.1%} below min {cfg.min_weight:.0%} — removed")
            del raw[s]

    # sector cap
    by_sector: Dict[str, float] = {}
    for s, w in raw.items():
        by_sector[sectors.get(s, "?")] = by_sector.get(sectors.get(s, "?"), 0) + w
    for sec, tot in by_sector.items():
        if tot > cfg.max_sector_weight + 1e-9:
            k = cfg.max_sector_weight / tot
            for s in raw:
                if sectors.get(s, "?") == sec:
                    raw[s] *= k
            v.changes.append(f"sector {sec}: {tot:.0%} scaled to {cfg.max_sector_weight:.0%}")

    # gross cap = min(regime, drawdown ladder, 1 - cash buffer, committee's own target)
    cap = min(caps.gross_cap, 1 - cfg.cash_buffer,
              float(proposal.get("target_gross_exposure", 1.0)))
    gross = sum(raw.values())
    if gross > cap + 1e-9:
        k = cap / gross if gross > 0 else 0
        raw = {s: w * k for s, w in raw.items()}
        v.changes.append(f"gross {gross:.0%} scaled to cap {cap:.0%} "
                         f"(regime {caps.regime_cap:.0%}, ladder {caps.ladder_cap:.0%})")

    # buys frozen by the sentinel: no weight may increase
    if caps.buys_frozen:
        cur = ledger.weights(prices)
        for s in list(raw):
            if raw[s] > cur.get(s, 0) + 1e-4:
                v.changes.append(f"{s}: buys frozen by crash sentinel; held at {cur.get(s, 0):.1%}")
                raw[s] = cur.get(s, 0)
                if raw[s] == 0:
                    del raw[s]

    n = len([w for w in raw.values() if w > 0])
    if sum(raw.values()) >= 0.30 and not (cfg.target_names[0] <= n <= cfg.target_names[1]):
        v.warnings.append(f"{n} names; mandate is {cfg.target_names[0]}-{cfg.target_names[1]} "
                          "when >30% invested")

    # turnover (initial deployment from cash exempt)
    cur = ledger.weights(prices)
    if cur:
        turnover = sum(max(raw.get(s, 0) - cur.get(s, 0), 0) for s in set(raw) | set(cur))
        if turnover > cfg.max_monthly_turnover:
            v.warnings.append(f"one-way turnover {turnover:.0%} exceeds "
                              f"{cfg.max_monthly_turnover:.0%} monthly budget")

    v.target = {s: round(w, 4) for s, w in raw.items() if w > 0}
    v.ok = not caps.halted
    if caps.halted:
        v.violations.append("FUND HALTED (kill switch or hard floor). Only sells allowed.")
        v.target = {}
    return v


# --------------------------------------------------------------------------- #
# Position stops (close-basis; checked by the sentinel after the close)
# --------------------------------------------------------------------------- #
def stop_hits(ledger: Ledger, closes: Dict[str, float], cfg) -> List[dict]:
    hits = []
    for s, p in ledger.positions.items():
        px = closes.get(s)
        if px is None:
            continue
        loss = px / p.avg_cost - 1
        from_peak = px / p.peak_close - 1
        if loss <= -cfg.stop_loss_from_entry:
            hits.append({"symbol": s, "rule": "stop_from_entry",
                         "detail": f"{loss:.1%} vs entry {p.avg_cost:.2f}"})
        elif p.peak_close > p.avg_cost and from_peak <= -cfg.trailing_stop_from_peak:
            hits.append({"symbol": s, "rule": "trailing_stop",
                         "detail": f"{from_peak:.1%} from peak close {p.peak_close:.2f}"})
    return hits


# --------------------------------------------------------------------------- #
# Rank-buffer exit (weekly; see research/exit_study/EXIT_RULE_STUDY.md)
# --------------------------------------------------------------------------- #
def rank_watch(ledger: Ledger, screen: pd.DataFrame, watch: Dict[str, int], cfg,
               review: bool) -> tuple:
    """Update the weak-week counters of held names and return (watch, exits, ranks).

    On a weekly review a holding whose screen rank is worse than
    `cfg.rank_exit_threshold` (or that is no longer eligible / ranked) adds one weak week;
    any other result resets it to 0. `cfg.rank_exit_weeks` weak weeks in a row -> exit.
    Between reviews the counters are only reported, never changed."""
    th, need = cfg.rank_exit_threshold, cfg.rank_exit_weeks
    ranks, new, exits = {}, {}, []
    scored = screen.dropna(subset=["score"]) if "score" in screen else screen.iloc[0:0]
    for s in ledger.positions:
        r = float(scored.loc[s, "rank"]) if s in scored.index else None
        ranks[s] = r
        n = watch.get(s, 0)
        if review:
            n = n + 1 if (r is None or r > th) else 0
        new[s] = n
        if review and n >= need:
            exits.append({"symbol": s, "rule": "rank_exit",
                          "detail": f"rank {'unranked' if r is None else int(r)} > {th} "
                                    f"for {n} weekly reviews"})
    return new, exits, ranks


# --------------------------------------------------------------------------- #
# Crash rules (pure function; the sentinel feeds it live numbers)
# --------------------------------------------------------------------------- #
def crash_level(proxy_intraday: Optional[float], proxy_3day: Optional[float],
                vix: Optional[float], cfg) -> str:
    """'normal' | 'shock' | 'crash'."""
    if ((proxy_intraday is not None and proxy_intraday <= cfg.crash_intraday_drop)
            or (proxy_3day is not None and proxy_3day <= cfg.crash_3day_drop)
            or (vix is not None and vix >= cfg.vix_crash)):
        return "crash"
    if ((proxy_intraday is not None and proxy_intraday <= cfg.shock_intraday_drop)
            or (vix is not None and vix >= cfg.vix_alert)):
        return "shock"
    return "normal"


def deleverage_plan(ledger: Ledger, prices: Dict[str, float], target_gross: float,
                    vol: Dict[str, float] | None = None) -> Dict[str, float]:
    """Shares to SELL per symbol to bring gross exposure down to target_gross.

    Sells pro-rata (keeps the portfolio shape; avoids a panicked judgement call
    about which name is 'weakest' in the middle of a crash)."""
    nav = ledger.nav(prices)
    mv = ledger.market_value(prices)
    if nav <= 0 or mv <= target_gross * nav + 1e-6:
        return {}
    keep = (target_gross * nav) / mv
    out = {}
    for s, p in ledger.positions.items():
        sell = p.qty * (1 - keep)
        sell = float(int(sell + 0.999)) if sell > 0 else 0.0   # round up whole shares
        out[s] = min(sell, p.qty)
    return {s: q for s, q in out.items() if q > 0}
