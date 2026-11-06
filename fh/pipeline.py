"""After-close pipeline: mark the book, check stops, screen, regime, research pack.

Pure orchestration over already-downloaded data, so it runs offline in tests.
"""
from __future__ import annotations

from typing import Dict

import pandas as pd

from fh import factors, pack as packmod
from fh.data import macro_summary
from fh.ledger import Ledger, append_nav
from fh.risk import current_caps, rank_watch, stop_hits
from fh.runtime import alert, read_json, write_json


def closes_from_bars(bars: Dict[str, pd.DataFrame]) -> Dict[str, float]:
    return {s: float(d["close"].iloc[-1]) for s, d in bars.items() if len(d)}


def _previous_nav_row(cfg, asof: str) -> dict:
    p = cfg.state_dir / "nav.csv"
    if not p.exists():
        return {}
    h = pd.read_csv(p)
    h = h[h["date"] < asof]
    if h.empty or "ew_level" not in h or pd.isna(h["ew_level"].iloc[-1]):
        return {}
    return {"date": h["date"].iloc[-1], "ew_level": float(h["ew_level"].iloc[-1])}


def update_shadow(cfg, target: Dict[str, float], prices: Dict[str, float], asof: str) -> dict:
    """Paper-simulate the quant-only shadow portfolio (5 bps per trade)."""
    p = cfg.state_dir / "shadow.json"
    st = read_json(p, {"cash": cfg.starting_capital, "qty": {}, "history": []})
    nav = st["cash"] + sum(q * prices.get(s, 0) for s, q in st["qty"].items())
    if target is not None:
        new_qty = {s: w * nav / prices[s] for s, w in target.items() if prices.get(s)}
        traded = sum(abs(new_qty.get(s, 0) - st["qty"].get(s, 0)) * prices.get(s, 0)
                     for s in set(new_qty) | set(st["qty"]))
        st["cash"] = nav - sum(q * prices[s] for s, q in new_qty.items()) - traded * 0.0005
        st["qty"] = new_qty
        nav = st["cash"] + sum(q * prices.get(s, 0) for s, q in st["qty"].items())
    st["history"] = [h for h in st["history"] if h["date"] != asof] + [
        {"date": asof, "nav": round(nav, 2)}]
    write_json(p, st)
    return {"date": asof, "shadow_nav": round(nav, 2)}


def run_after_close(cfg, asof: str, universe: pd.DataFrame, bars: Dict[str, pd.DataFrame],
                    fundamentals: pd.DataFrame, macro: pd.DataFrame | None,
                    ledger: Ledger, rebalance_shadow: bool, log=print) -> dict:
    prices = closes_from_bars(bars)
    missing = [s for s in ledger.positions if s not in prices]
    if missing:
        alert(cfg, "WARN", f"No close price for held names: {missing}")

    # 1. mark to market
    rec = ledger.mark(prices, asof)

    # 2. drawdown ladder bookkeeping
    dd = rec["drawdown"]
    floor = min(t for t, _ in cfg.dd_ladder)
    if dd <= floor and not ledger.halted:
        ledger.halted, ledger.halt_reason = True, f"hard floor hit: drawdown {dd:.1%}"
        alert(cfg, "CRITICAL", f"Drawdown {dd:.1%} hit hard floor {floor:.0%}: fund HALTED, "
                               "all positions queued for exit")
    ledger.save(cfg)

    # 3. position stops -> queue automatic exits for next open
    exits = stop_hits(ledger, prices, cfg)
    if ledger.halted:
        exits = [{"symbol": s, "rule": "halt", "detail": ledger.halt_reason}
                 for s in ledger.positions]
    for e in exits:
        alert(cfg, "RISK", f"Stop: {e['symbol']} {e['rule']} ({e['detail']}) — exit queued")

    # 4. screen + regime + caps
    scr = factors.screen(bars, fundamentals, universe, cfg)

    # 4b. rank-buffer exit, reviewed weekly (the Saturday run = Friday's close); never on
    #     stale data, because stale prices give meaningless ranks
    from fh.data import bars_freshness
    fresh = bars_freshness(bars, asof)
    wpath = cfg.state_dir / "rank_watch.json"
    watch = (read_json(wpath, {}) or {}).get("weak_weeks", {})
    review = bool(rebalance_shadow) and not fresh["is_stale"] and not ledger.halted
    watch, rank_exits, held_ranks = rank_watch(ledger, scr, watch, cfg, review)
    write_json(wpath, {"asof": asof, "reviewed": review, "weak_weeks": watch,
                       "ranks": held_ranks, "threshold": cfg.rank_exit_threshold,
                       "weeks": cfg.rank_exit_weeks})
    already = {e["symbol"] for e in exits}
    for e in rank_exits:
        if e["symbol"] not in already:
            exits.append(e)
            alert(cfg, "RISK", f"Rank exit: {e['symbol']} ({e['detail']}) — exit queued")
    write_json(cfg.state_dir / "pending_risk_exits.json",
               {"created": asof, "exits": exits})
    regime = factors.market_regime(bars, macro, cfg)
    caps = current_caps(cfg, ledger, prices, regime)
    if ledger.gross_exposure(prices) > caps.gross_cap + 0.05:
        alert(cfg, "RISK", f"Gross {ledger.gross_exposure(prices):.0%} above cap "
                           f"{caps.gross_cap:.0%} — deleverage queued")
        write_json(cfg.state_dir / "pending_deleverage.json",
                   {"created": asof, "target_gross": caps.gross_cap})
    sectors = factors.sector_table(scr)

    # 5. NAV history + shadow benchmark
    shadow_target = packmod.shadow_portfolio(scr, caps.gross_cap if not ledger.halted else 0, cfg)
    sh = update_shadow(cfg, shadow_target if rebalance_shadow else None, prices, asof)
    rec.update(sh)
    prev = _previous_nav_row(cfg, asof)
    rec["ew_level"] = round(factors.chained_ew_level(
        bars, prev.get("date"), prev.get("ew_level"), asof), 6)
    rec["regime"] = regime["regime"]
    rec["gross_cap"] = caps.gross_cap
    append_nav(cfg, rec)
    nav_hist = pd.read_csv(cfg.state_dir / "nav.csv")

    # 6. research pack
    macro_rows = macro_summary(macro) if macro is not None and len(macro) else []
    pk = packmod.build_pack(cfg, asof, scr, sectors, regime, macro_rows, ledger, prices,
                            caps, nav_hist)
    pk["data_freshness"] = fresh
    pk["rank_watch"] = {"threshold": cfg.rank_exit_threshold, "weeks": cfg.rank_exit_weeks,
                        "weak_weeks": watch, "ranks": held_ranks,
                        "exits_queued": [e["symbol"] for e in rank_exits]}
    md = packmod.pack_markdown(pk)
    if held_ranks:
        rows = [f"| {s} | {'–' if r is None else int(r)} | {watch.get(s, 0)} |"
                for s, r in sorted(held_ranks.items(), key=lambda kv: (kv[1] is None, kv[1] or 0))]
        md += (f"\n## Holdings: screen rank and rank-exit watch\n\n"
               f"Automatic exit when rank > {cfg.rank_exit_threshold} on {cfg.rank_exit_weeks} "
               f"consecutive weekly reviews. Queued this run: "
               f"{', '.join(e['symbol'] for e in rank_exits) or 'none'}.\n\n"
               f"| Symbol | Rank | Weak weeks |\n|---|---|---|\n" + "\n".join(rows) + "\n")
    # SEC filings for holdings + candidate pool (read from data/filings; fetched by daily_update)
    from fh import filings as filmod
    pool = list(scr.dropna(subset=["score"]).index[:cfg.candidate_pool]) if "score" in scr else []
    fsec, frows = filmod.pack_section(cfg, list(ledger.positions), pool, asof)
    md += fsec
    pk["filings"] = frows
    apath = cfg.state_dir / "filings_alerted.json"
    seen = set(read_json(apath, []) or [])
    for e in filmod.new_red_flags(cfg, list(ledger.positions), asof):
        key = f"{e['symbol']}|{e['filed']}|{e['form']}|{e['what']}"
        if key not in seen:
            alert(cfg, "RISK", f"Red-flag SEC filing on holding {e['symbol']}: {e['form']} "
                               f"{e['what']} (filed {e['filed']}) {e['url']} — for review, "
                               f"no automatic trade")
            seen.add(key)
    write_json(apath, sorted(seen))
    # mid-week review gate (read by the Tue-Fri 08:17 HKT "Mid-week Review" task)
    from fh import midweek
    g = midweek.gate(cfg, asof, nav_hist, regime, list(ledger.positions),
                     filmod.new_red_flags(cfg, list(ledger.positions), asof, days=1),
                     fresh["is_stale"])
    if g.get("calendar_warning"):
        alert(cfg, "WARN", g["calendar_warning"])
    md += midweek.gate_markdown(g)
    pk["midweek_gate"] = g
    if fresh["is_stale"]:
        md = (f"> **DATA STALE - DO NOT DECIDE ON THIS PACK.** {fresh['stale']}/{fresh['symbols']} "
              f"symbols ({fresh['stale_share']:.0%}) have no price bar for {asof}; median last "
              f"bar {fresh['median_last_bar']}. Prices, screen and regime are out of date. "
              f"Re-run scripts/daily_update.py (it falls back to Yahoo Finance) first.\n\n" + md)
        log(f"DATA STALE: {fresh['stale']}/{fresh['symbols']} symbols lack a {asof} bar "
            f"(median last bar {fresh['median_last_bar']})")
    out = cfg.research_dir / asof
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "pack.json", pk)
    (out / "pack.md").write_text(md)
    scr.to_csv(out / "screen_full.csv")
    write_json(cfg.state_dir / "latest.json", {"asof": asof, "pack": str(out / "pack.json"),
                                               "regime": regime["regime"],
                                               "nav": rec["nav"], "drawdown": dd,
                                               "data_stale": fresh["is_stale"]})
    log(f"after-close done {asof}: NAV {rec['nav']} dd {dd:.1%} regime {regime['regime']} "
        f"cap {caps.gross_cap:.0%} exits {len(exits)}")
    return pk
