"""Synthetic demo data so the dashboard can be seen before the fund has run.
Everything here is FAKE (tickers DMxx, random prices). Written to dashboard/_demo/."""
from __future__ import annotations

import json
import shutil

import numpy as np
import pandas as pd

from fh.approval import record_decision
from fh.ledger import Ledger
from fh.pipeline import run_after_close
from fh.runtime import alert, write_json

SECTORS = ["Information Technology", "Health Care", "Financials", "Industrials",
           "Consumer Discretionary", "Communication Services", "Energy", "Utilities"]


def _universe(n=80, days=520, seed=4):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-09-25", periods=days)
    mkt = rng.normal(0.0009, 0.007, days)
    mkt[495:499] -= 0.008                           # a small pullback for the demo
    bars, rows = {}, []
    for i in range(n):
        s = f"DM{i:02d}"
        beta = rng.uniform(0.7, 1.4)
        r = beta * mkt + rng.normal(rng.normal(0.0002, 0.0005), 0.014, days)
        c = rng.uniform(20, 400) * np.exp(np.cumsum(r))
        bars[s] = pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
                                "volume": rng.uniform(1e6, 8e6)}, index=idx)
        rows.append({"symbol": s, "ib_symbol": s, "name": f"Demo Company {i:02d}",
                     "sector": SECTORS[i % len(SECTORS)], "industry": "Demo", "CIK": f"{i:010d}"})
    uni = pd.DataFrame(rows)
    fund = pd.DataFrame({
        "roe": rng.normal(0.16, 0.1, n), "operating_margin": rng.normal(0.18, 0.08, n),
        "gross_margin": rng.normal(0.45, 0.15, n), "fcf_ttm": rng.normal(2e9, 1e9, n),
        "debt_to_equity": rng.uniform(0, 2, n), "share_change_1y": rng.normal(0, 0.02, n),
        "revenue_growth": rng.normal(0.08, 0.1, n), "eps_growth_proxy": rng.normal(0.1, 0.2, n),
        "net_income_ttm": rng.uniform(1e9, 2e10, n), "shares_out": rng.uniform(2e8, 3e9, n)},
        index=uni["ib_symbol"])
    return uni, bars, fund


def _macro(dates):
    rng = np.random.default_rng(3)
    n = len(dates)
    walk = lambda s, v: s + np.cumsum(rng.normal(0, v, n))  # noqa: E731
    return pd.DataFrame({"DGS10": walk(4.1, 0.03), "DGS2": walk(3.7, 0.03),
                         "T10Y2Y": walk(0.4, 0.02), "DFF": np.full(n, 3.88),
                         "BAMLH0A0HYM2": walk(3.1, 0.02), "VIXCLS": np.abs(walk(16, 0.6)) + 9,
                         "ICSA": walk(225000, 3000), "UNRATE": np.full(n, 4.3),
                         "CPIAUCSL": np.linspace(318, 327, n), "NFCI": walk(-0.5, 0.01),
                         "DCOILWTICO": walk(68, 0.8), "DTWEXBGS": walk(121, 0.2)}, index=dates)


REPORT = {
    "macro": ("Macro view", {"role": "macro", "cycle": "late-cycle expansion", "confidence": "medium",
                             "recommended_max_gross": 0.85, "favour_sectors": ["Information Technology", "Health Care"],
                             "avoid_sectors": ["Utilities"],
                             "event_risks": [{"date": "2026-10-28", "event": "FOMC decision (DEMO)"}]}),
    "sector": ("Sector leadership", {"role": "sector", "overweight": ["Information Technology", "Financials"],
                                     "underweight": ["Energy", "Utilities"],
                                     "theme_clusters": [{"theme": "AI infrastructure (DEMO)", "symbols": ["DM08", "DM16"]}],
                                     "crowding_warning": "IT is 30% of the top-40 pool — at the cap", "confidence": "medium"}),
    "fundamental": ("Fundamental review", {"role": "fundamental", "ratings": []}),
    "technical": ("Technical review", {"role": "technical", "signals": [], "breadth_comment": "Breadth healthy at 62% above 200d"}),
    "strategy": ("Strategy", {"role": "strategist", "target_gross": 0.85, "sector_tilts": {"Information Technology": "over", "Utilities": "under"},
                              "n_names": 12, "sizing": "conviction", "turnover_budget": 0.3, "staging_note": "Deploy in two tranches"}),
    "pm": ("Portfolio construction", None),
    "risk": ("Risk review", {"role": "risk", "verdict": "approve", "required_changes": [],
                             "stress_minus10_pct": -0.089, "earnings_in_10d": ["DM12"], "notes": "Start DM12 at half weight until after earnings"}),
}


def _write_committee(cfg, folder, pack, weeknum, pending=False):
    d = cfg.research_dir / folder
    (d / "reports").mkdir(parents=True, exist_ok=True)
    cands = pack["candidates"][:14]
    rng = np.random.default_rng(weeknum)
    ratings = [{"symbol": c["symbol"], "rating": ["Buy", "Buy", "Hold", "Avoid"][rng.integers(0, 4)],
                "conviction": ["low", "medium", "high"][rng.integers(0, 3)], "red_flags": [],
                "next_earnings": "unverified"} for c in cands]
    signals = [{"symbol": c["symbol"], "trend": "uptrend", "timing": ["buy_now", "wait"][rng.integers(0, 2)],
                "vol_flag": bool(c.get("vol_63d", 0) and c["vol_63d"] > 0.4)} for c in cands]
    for key, (title, js) in REPORT.items():
        if key == "fundamental":
            js = {**js, "ratings": ratings}
        if key == "technical":
            js = {**js, "signals": signals}
        body = (f"# {title} — {folder} (DEMO)\n\n**This is synthetic demo content.** In the live "
                f"system this report is written by the {key} agent from the research pack and cited web sources.\n\n"
                "## Key points\n- Fact: breadth above 200-day average is healthy (pack).\n"
                "- Interpretation: leadership is broadening beyond mega caps.\n"
                "- Opinion (medium confidence): stay invested near the regime cap.\n\n"
                "| Item | Value | Source |\n|---|---|---|\n| Regime | "
                f"{pack['regime']['regime']} | pack |\n| Breadth | {pack['regime']['breadth_above_200d']:.0%} | pack |\n")
        if js:
            body += "\n```json\n" + json.dumps(js, indent=1) + "\n```\n"
        (d / "reports" / f"{key}.md").write_text(body)
    buys = [r["symbol"] for r in ratings if r["rating"] != "Avoid"][:12]
    w = round(min(0.085, 0.9 / max(len(buys), 1)), 4)
    held = {h["symbol"] for h in pack["fund"]["holdings"]}
    decision = "initial_deployment" if not held else ("hold" if weeknum % 2 else "rebalance")
    if decision == "hold":
        holdings = [{"symbol": h["symbol"], "weight": max(0.04, min(0.1, h["weight"])), "conviction": "medium",
                     "thesis": "Thesis intact (DEMO)", "bear_case": "Momentum reversal"} for h in pack["fund"]["holdings"]]
    else:
        holdings = [{"symbol": s, "weight": w, "conviction": "medium",
                     "thesis": f"Top-decile momentum with improving margins (DEMO {s})",
                     "bear_case": "Crowded trade; multiple compression if yields rise",
                     "review_trigger": "Close below 200-day average"} for s in buys]
    prop = {"asof": folder, "status": "proposed", "decision": decision,
            "summary": f"(DEMO) {decision.replace('_', ' ').title()}: regime {pack['regime']['regime']}, "
                       f"{len(holdings)} names at ~{sum(h['weight'] for h in holdings):.0%} gross. "
                       "Committee favours IT and Health Care leaders; Utilities underweight.",
            "target_gross_exposure": 0.9, "holdings": holdings, "exits": [],
            "deviations_from_shadow": "Swapped two high-vol shadow names for Health Care quality names (DEMO).",
            "votes": {"macro": "late-cycle, stay ≤85%", "sector": "IT/Financials over",
                      "fundamental": "10 Buy, 2 Hold", "technical": "most uptrends intact",
                      "strategist": "85% gross, 12 names", "risk": "approve, DM12 half-weight"}}
    (d / "target_portfolio.json").write_text(json.dumps(prop, indent=2))
    (d / "committee_memo.md").write_text(
        f"# Committee memo — {folder} (DEMO)\n\n**Decision:** {decision.replace('_', ' ')} — "
        f"{len(holdings)} names, target gross ~{sum(h['weight'] for h in holdings):.0%}.\n\n"
        f"## Why\n1. Code regime is {pack['regime']['regime']} (breadth {pack['regime']['breadth_above_200d']:.0%} above 200-day).\n"
        "2. Top screen names pass fundamental review.\n"
        "3. Risk budget is intact (drawdown well above the −8% ladder step).\n\n"
        "## Main risk\nA rates shock would hit the IT overweight; the sentinel and ladder cap the damage.\n\n"
        "## Dissent\nMacro strategist preferred 80% gross.\n\n"
        f"## Action\n`python3 scripts/approve.py {folder}` — or approve in the dashboard.\n")
    return prop


def build_demo(cfg):
    root = cfg.state_dir.parent
    if root.exists():
        shutil.rmtree(root)
    cfg.ensure_dirs()
    uni, bars_full, fund = _universe()
    macro = _macro(bars_full["DM00"].index)
    all_days = bars_full["DM00"].index
    days = all_days[-60:]
    ledger = Ledger(cash=cfg.starting_capital, hwm=cfg.starting_capital,
                    hwm_date=str(days[0].date()), risk_period_start=str(days[0].date()))
    ledger.save(cfg)
    week = 0
    for i, d in enumerate(days):
        bars = {s: b[b.index <= d] for s, b in bars_full.items()}
        asof = str(d.date())
        fri = d.weekday() == 4
        ledger = Ledger.load(cfg)
        pk = run_after_close(cfg, asof, uni, bars, fund, macro[macro.index <= d], ledger,
                             rebalance_shadow=fri or i == 0, log=lambda *_: None)
        ex = json.loads((cfg.state_dir / "pending_risk_exits.json").read_text()).get("exits", [])
        if ex:
            nxt = days[min(i + 1, len(days) - 1)]
            ledger = Ledger.load(cfg)
            for e in ex:
                if e["symbol"] in ledger.positions:
                    ledger.apply_fill(e["symbol"], "SELL", ledger.positions[e["symbol"]].qty,
                                      float(bars_full[e["symbol"]].loc[nxt, "close"]), 1.0,
                                      when=f"{nxt.date()}T09:45:00", reason=f"risk:{e['rule']}")
            ledger.save(cfg)
            write_json(cfg.state_dir / "pending_risk_exits.json", {"exits": []})
        if fri:
            week += 1
            last = d == days[days.weekday == 4][-1]
            _write_committee(cfg, asof, pk, week)
            folder = cfg.research_dir / asof
            if not last:
                rec = record_decision(cfg, folder, approve=(week != 3),
                                      note="Looks consistent with the charter" if week != 3
                                      else "Too much turnover for a hold week", via="dashboard")
                if rec["decision"]:
                    # simulate execution at next close
                    nxt = days[min(i + 1, len(days) - 1)]
                    px = {s: float(b.loc[nxt, "close"]) for s, b in bars_full.items()}
                    from fh.orders import plan_orders
                    ledger = Ledger.load(cfg)
                    for o in plan_orders(rec["target"], ledger, px, cfg):
                        ledger.apply_fill(o.symbol, o.side, o.qty, o.ref_price, 1.0,
                                          uni.set_index("ib_symbol").loc[o.symbol, "sector"],
                                          when=f"{nxt.date()}T09:45:00", reason="rebalance")
                    ledger.save(cfg)
                    ap = json.loads((folder / "approval.json").read_text())
                    ap["executed"] = str(nxt.date())
                    (folder / "approval.json").write_text(json.dumps(ap, indent=2))
                    write_json(folder / "execution.json", {**ap, "results": [
                        {"symbol": s, "status": "Filled"} for s in rec["target"]]})
                    write_json(cfg.state_dir / "approved_target.json", ap)
        if i % 7 == 3:
            (cfg.research_dir / asof).mkdir(parents=True, exist_ok=True)
            (cfg.research_dir / asof / "daily_brief.md").write_text(
                f"# Daily risk brief — {asof} (DEMO)\n\n- Pipeline: data OK, sentinel ran all session.\n"
                "- NAV and drawdown within limits; no stops hit.\n- Holdings news: nothing material.\n")
    ap_ = cfg.state_dir / "alerts.jsonl"
    if ap_.exists():
        seen, keep = set(), []
        for line in ap_.read_text().splitlines():
            m = json.loads(line)["msg"]
            if m not in seen:
                seen.add(m); keep.append(line)
        ap_.write_text("\n".join(keep) + ("\n" if keep else ""))
    alert(cfg, "RISK", "Market shock: proxy -3.2% intraday. New buys frozen for 4 days. (DEMO)")
    alert(cfg, "INFO", "Executed 12 orders (DEMO)")
    alert(cfg, "WARN", "No close price for held names: [] (DEMO)")
    write_json(cfg.state_dir / "sentinel_state.json", {"last": {
        "time_et": "2026-09-25T15:55:00-04:00", "nav": None, "drawdown": -0.021, "gross": 0.84,
        "market_chg": -0.004, "three_day": 0.006, "vix": 17.2, "level": "normal", "proxy_n": 18}})
    led = Ledger.load(cfg)
    last_px = {s: float(b["close"].iloc[-1]) for s, b in bars_full.items()}
    write_json(cfg.state_dir / "account.json", {
        "time_hkt": "2026-09-26T06:40:00+08:00", "account": "DU0000000 (DEMO)", "connected": True,
        "net_liquidation": round(led.nav(last_px), 2), "cash": round(led.cash, 2),
        "unrealized_pnl": round(sum(p.qty * (last_px[s] - p.avg_cost) for s, p in led.positions.items()), 2),
        "realized_pnl": 0.0, "buying_power": round(led.cash * 2, 2),
        "positions": [{"symbol": s, "qty": p.qty, "avg_cost": p.avg_cost, "market_price": last_px[s],
                       "market_value": round(p.qty * last_px[s], 2),
                       "unrealized_pnl": round(p.qty * (last_px[s] - p.avg_cost), 2)}
                      for s, p in led.positions.items()],
        "open_orders": [{"symbol": s, "action": "SELL", "qty": p.qty, "type": "STP",
                         "price": round(p.avg_cost * 0.8, 2), "tif": "GTC", "ref": f"FH-STOP-{s}",
                         "status": "PreSubmitted"} for s, p in led.positions.items()]})
    # a "live" intraday mark so the refresh view can be previewed
    import sys as _sys
    from pathlib import Path as _P
    _sys.path.insert(0, str(_P(__file__).resolve().parent.parent / "scripts"))
    from account_snapshot import fund_live
    rng = np.random.default_rng(11)
    quotes = {s: {"price": round(last_px[s] * (1 + rng.normal(0.003, 0.01)), 2), "prev_close": last_px[s],
                  "source": "IBKR portfolio (DEMO)", "bid": None, "ask": None} for s in led.positions}
    for q in quotes.values():
        q["day_chg"] = q["price"] / q["prev_close"] - 1
    write_json(cfg.state_dir / "live_portfolio.json", {"time_hkt": "2026-09-26T02:15:00+08:00",
                                                       "market_open": True, **fund_live(led, quotes)})
    for n in ("daily_update", "sentinel", "execute"):
        (cfg.logs_dir / f"{n}.log").write_text("demo\n")
