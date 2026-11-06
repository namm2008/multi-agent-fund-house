"""Offline tests: no IBKR, no network. Run: python -m pytest -q"""
import json
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fh.config import Config  # noqa: E402
from fh.ledger import Ledger, drawdown_cap, reconcile  # noqa: E402
from fh.orders import plan_orders  # noqa: E402
from fh.risk import RiskCaps, crash_level, deleverage_plan, stop_hits, validate_target  # noqa: E402
from fh import data as fdata  # noqa: E402

SECTORS = ["Information Technology", "Health Care", "Financials", "Energy", "Industrials"]


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c = replace(c, starting_capital=10_000.0, max_order_value=2_000.0, data_dir=tmp_path / "data", state_dir=tmp_path / "state",
                research_dir=tmp_path / "research", logs_dir=tmp_path / "logs",
                kill_switch=tmp_path / "STOP_TRADING", min_dollar_volume=1e6)
    c.ensure_dirs()
    return c


def synth(n=60, days=320, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2025-06-02", periods=days)
    bars, rows = {}, []
    for i in range(n):
        s = f"S{i:02d}"
        drift = rng.normal(0.0004, 0.0006)
        r = rng.normal(drift, 0.018, days)
        c = 50 * np.exp(np.cumsum(r))
        bars[s] = pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
                                "volume": 2e6}, index=idx)
        rows.append({"symbol": s, "ib_symbol": s, "name": s, "sector": SECTORS[i % 5],
                     "industry": "x", "CIK": f"{i:010d}"})
    uni = pd.DataFrame(rows)
    fund = pd.DataFrame({"roe": rng.normal(0.15, 0.1, n), "operating_margin": rng.normal(.2, .1, n),
                         "fcf_ttm": rng.normal(1e8, 5e7, n), "debt_to_equity": rng.uniform(0, 2, n),
                         "share_change_1y": rng.normal(0, .02, n),
                         "revenue_growth": rng.normal(.08, .1, n),
                         "eps_growth_proxy": rng.normal(.1, .2, n), "net_income_ttm": 1e8,
                         "shares_out": 1e8}, index=uni["ib_symbol"])
    return uni, bars, fund


def test_drawdown_ladder():
    lad = [(-0.08, 0.75), (-0.12, 0.5), (-0.15, 0.0)]
    assert drawdown_cap(-0.02, lad) == 1.0
    assert drawdown_cap(-0.08, lad) == 0.75
    assert drawdown_cap(-0.13, lad) == 0.5
    assert drawdown_cap(-0.30, lad) == 0.0


def test_ledger_nav_and_fills(cfg):
    L = Ledger.load(cfg)
    L.apply_fill("AAA", "BUY", 10, 100, 1.0)
    assert L.cash == pytest.approx(10_000 - 1001)
    assert L.nav({"AAA": 110}) == pytest.approx(10_000 - 1001 + 1100)
    L.apply_fill("AAA", "SELL", 10, 110, 1.0)
    assert "AAA" not in L.positions
    with pytest.raises(ValueError):
        L.apply_fill("BBB", "SELL", 1, 10, 0)
    L.save(cfg)
    assert Ledger.load(cfg).cash == pytest.approx(L.cash)


def test_reconcile():
    L = Ledger(cash=0)
    L.apply_fill("AAA", "BUY", 5, 10, 0)
    assert reconcile(L, {"AAA": 5}) == []
    assert reconcile(L, {"AAA": 3})


def test_stops(cfg):
    L = Ledger.load(cfg)
    L.apply_fill("AAA", "BUY", 10, 100, 0)
    L.apply_fill("BBB", "BUY", 10, 100, 0)
    L.positions["BBB"].peak_close = 200
    hits = {h["symbol"]: h["rule"] for h in stop_hits(L, {"AAA": 84, "BBB": 149}, cfg)}
    assert hits == {"AAA": "stop_from_entry", "BBB": "trailing_stop"}


def test_crash_level(cfg):
    assert crash_level(-0.01, -0.02, 18, cfg) == "normal"
    assert crash_level(-0.035, None, 18, cfg) == "shock"
    assert crash_level(-0.06, None, None, cfg) == "crash"
    assert crash_level(-0.01, -0.09, None, cfg) == "crash"
    assert crash_level(None, None, 45, cfg) == "crash"


def test_deleverage(cfg):
    L = Ledger(cash=2000)
    L.apply_fill("AAA", "BUY", 40, 100, 0)
    L.apply_fill("BBB", "BUY", 40, 100, 0)   # cash now -6000? use prices
    L.cash = 2000
    prices = {"AAA": 100, "BBB": 100}
    plan = deleverage_plan(L, prices, 0.5)
    # NAV 10000, MV 8000 -> keep 5000 -> sell 15 each (rounded up)
    assert plan == {"AAA": 15.0, "BBB": 15.0}


def test_plan_orders_whole_shares_and_cash(cfg):
    L = Ledger.load(cfg)
    orders = plan_orders({"AAA": 0.10, "BBB": 0.10}, L, {"AAA": 333.0, "BBB": 50.0}, cfg)
    q = {o.symbol: o.qty for o in orders}
    assert q == {"AAA": 3.0, "BBB": 20.0}
    assert all(o.side == "BUY" for o in orders)


def test_validate_target_rules(cfg):
    uni, bars, fund = synth()
    from fh import factors
    scr = factors.screen(bars, fund, uni, cfg)
    pool = list(scr.dropna(subset=["score"]).head(cfg.candidate_pool).index)
    outside = list(scr.dropna(subset=["score"]).index[cfg.candidate_pool:])[0]
    L = Ledger.load(cfg)
    prices = scr["price"].to_dict()
    prop = {"target_gross_exposure": 1.0,
            "holdings": [{"symbol": pool[0], "weight": 0.25},        # > max weight
                         {"symbol": outside, "weight": 0.08},        # not in pool
                         {"symbol": "SPY", "weight": 0.10}]          # ETF / unknown
            + [{"symbol": s, "weight": 0.08} for s in pool[1:12]]}
    caps = RiskCaps(regime_cap=0.7, ladder_cap=1.0, halted=False, buys_frozen=False)
    v = validate_target(prop, scr, L, prices, caps, cfg)
    assert pool[0] in v.target and v.target[pool[0]] <= cfg.max_weight + 1e-9
    assert outside not in v.target and "SPY" not in v.target
    assert sum(v.target.values()) <= 0.7 + 1e-6
    sec = scr["sector"].to_dict()
    by = {}
    for s, w in v.target.items():
        by[sec[s]] = by.get(sec[s], 0) + w
    assert max(by.values()) <= cfg.max_sector_weight + 1e-6
    # halted fund: nothing allowed
    v2 = validate_target(prop, scr, L, prices, replace(caps, halted=True), cfg)
    assert v2.target == {} and not v2.ok


def test_end_to_end_offline(cfg, capsys):
    from fh.pipeline import run_after_close
    uni, bars, fund = synth()
    L = Ledger.load(cfg)
    pk = run_after_close(cfg, "2026-08-21", uni, bars, fund, None, L, rebalance_shadow=True)
    assert len(pk["candidates"]) == cfg.candidate_pool
    assert pk["regime"]["regime"] in ("risk_on", "neutral", "risk_off")
    folder = cfg.research_dir / "2026-08-21"
    assert (folder / "pack.md").exists()
    # committee writes a proposal -> risk check -> orders
    top = [c["symbol"] for c in pk["candidates"][:12]]
    prop = {"decision": "initial deployment", "target_gross_exposure": 0.9,
            "holdings": [{"symbol": s, "weight": 0.075} for s in top]}
    (folder / "target_portfolio.json").write_text(json.dumps(prop))
    from fh.approval import evaluate, record_decision
    ev = evaluate(cfg, folder)
    assert ev["final_target"] and ev["orders"] and ev["approvable"] is False  # status not 'proposed'
    assert sum(o["value"] for o in ev["orders"] if o["side"] == "BUY") <= 10_000
    prop["status"] = "proposed"
    (folder / "target_portfolio.json").write_text(json.dumps(prop))
    assert evaluate(cfg, folder)["approvable"]
    rec = record_decision(cfg, folder, True, "ok", via="test")
    assert rec["decision"] and (cfg.state_dir / "approved_target.json").exists()
    assert "APPROVED" in (cfg.state_dir / "audit.jsonl").read_text()
    # an executed proposal cannot be re-approved
    ap = json.loads((folder / "approval.json").read_text()); ap["executed"] = "2026-08-24"
    (folder / "approval.json").write_text(json.dumps(ap))
    with pytest.raises(ValueError):
        record_decision(cfg, folder, True)


def test_edgar_ttm_parsing():
    facts = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        {"start": "2024-01-01", "end": "2024-12-31", "val": 100, "filed": "2025-02-10", "form": "10-K"},
        {"start": "2023-01-01", "end": "2023-12-31", "val": 80, "filed": "2024-02-10", "form": "10-K"},
        {"start": "2025-01-01", "end": "2025-03-31", "val": 30, "filed": "2025-05-01", "form": "10-Q"},
        {"start": "2025-04-01", "end": "2025-06-30", "val": 30, "filed": "2025-08-01", "form": "10-Q"},
        {"start": "2024-07-01", "end": "2024-09-30", "val": 25, "filed": "2024-11-01", "form": "10-Q"},
        {"start": "2024-10-01", "end": "2024-12-31", "val": 27, "filed": "2025-02-10", "form": "10-K"},
    ]}}}}}
    # as of 2025-03-01: only FY2024 known -> 100; growth vs FY2023 = 25%
    r = fdata.fundamentals_row(facts, pd.Timestamp("2025-03-01"))
    assert r["revenue_ttm"] == 100 and r["revenue_growth"] == pytest.approx(0.25)
    # as of 2025-09-01: last four quarters 25+27+30+30 = 112 (no look-ahead beyond filed)
    r2 = fdata.fundamentals_row(facts, pd.Timestamp("2025-09-01"))
    assert r2["revenue_ttm"] == 112


def test_edgar_ignores_proxy_statement_and_uses_ytd():
    rows = [
        {"start": "2025-01-01", "end": "2025-12-31", "val": 3_511_400_000, "filed": "2026-02-17", "form": "10-K"},
        {"start": "2025-01-01", "end": "2025-12-31", "val": 3511, "filed": "2026-04-16", "form": "DEF 14A"},
        {"start": "2025-01-01", "end": "2025-06-30", "val": 1_700_000_000, "filed": "2025-08-05", "form": "10-Q"},
        {"start": "2026-01-01", "end": "2026-06-30", "val": 2_235_800_000, "filed": "2026-08-05", "form": "10-Q"},
        {"start": "2026-04-01", "end": "2026-06-30", "val": 1_212_900_000, "filed": "2026-08-05", "form": "10-Q"},
    ]
    facts = {"facts": {"us-gaap": {"NetIncomeLoss": {"units": {"USD": rows}}}}}
    # before the H1-2026 10-Q: FY2025 from the 10-K, NOT the proxy-statement value
    r = fdata.fundamentals_row(facts, pd.Timestamp("2026-05-01"))
    assert r["net_income_ttm"] == 3_511_400_000
    # after it: FY2025 + H1-2026 - H1-2025
    r2 = fdata.fundamentals_row(facts, pd.Timestamp("2026-09-01"))
    assert r2["net_income_ttm"] == 3_511_400_000 + 2_235_800_000 - 1_700_000_000


def test_ew_benchmark_independent_of_cached_history(cfg):
    """Regression (2026-10-15): downloading more price history must not move the benchmark."""
    from fh import factors
    from fh.pipeline import run_after_close
    uni, bars, fund = synth(n=30, days=400, seed=5)
    d1, d2 = str(bars["S00"].index[-2].date()), str(bars["S00"].index[-1].date())
    short = {s: d.iloc[-250:] for s, d in bars.items()}
    assert factors.chained_ew_level(bars, d1, 1.0, d2) == pytest.approx(
        factors.chained_ew_level(short, d1, 1.0, d2))
    # through the pipeline: day 1 starts at 1.0; day 2 with 4x longer history only moves by that day's return
    run_after_close(cfg, d1, uni, {s: b.iloc[:-1].iloc[-250:] for s, b in bars.items()},
                    fund, None, Ledger.load(cfg), rebalance_shadow=True)
    run_after_close(cfg, d2, uni, bars, fund, None, Ledger.load(cfg), rebalance_shadow=False)
    nav = pd.read_csv(cfg.state_dir / "nav.csv")
    assert nav["ew_level"].iloc[0] == 1.0
    day_ret = factors.ew_daily_returns(bars).iloc[-1]
    assert nav["ew_level"].iloc[1] == pytest.approx(1 + day_ret, abs=1e-6)



def test_rank_watch_needs_two_weak_weekly_reviews(cfg):
    from fh.risk import rank_watch
    from fh.ledger import Position
    led = Ledger(cash=0.0)
    for s in ("AAA", "BBB", "CCC"):
        led.positions[s] = Position(10, 100.0, "2026-10-01", 100.0, "Tech")
    scr = pd.DataFrame({"score": [0.9, 0.5, np.nan], "rank": [5, 75, 400]},
                       index=["AAA", "BBB", "CCC"])          # CCC ineligible -> weak
    w, ex, ranks = rank_watch(led, scr, {}, cfg, review=True)
    assert w == {"AAA": 0, "BBB": 1, "CCC": 1} and ex == [] and ranks["CCC"] is None
    w2, ex2, _ = rank_watch(led, scr, w, cfg, review=False)   # weekday: no change
    assert w2 == w and ex2 == []
    scr.loc["BBB", "rank"] = 70
    w3, ex3, _ = rank_watch(led, scr, w, cfg, review=True)
    assert {e["symbol"] for e in ex3} == {"BBB", "CCC"}
    assert all(e["rule"] == "rank_exit" for e in ex3)
    scr.loc["BBB", "rank"] = 30                               # recovered -> reset
    w4, _, _ = rank_watch(led, scr, {"BBB": 1}, cfg, review=True)
    assert w4["BBB"] == 0


def test_series_merges_switched_revenue_tags_and_stays_point_in_time():
    """A company that switched revenue tags must get its CURRENT revenue (bug 18 Oct:
    0% growth for 155 names), and a later filing must not hide the figure known earlier."""
    from fh import data
    def row(start, end, val, filed):
        return {"start": start, "end": end, "val": val, "filed": filed, "form": "10-K", "fp": "FY"}
    facts = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [row("2016-10-01", "2017-09-30", 229.0, "2017-11-03"),
                                       row("2024-10-01", "2025-09-30", 999.0, "2026-11-01")]}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            row("2023-10-01", "2024-09-30", 391.0, "2024-11-01"),
            row("2024-10-01", "2025-09-30", 416.0, "2025-10-31")]}}}}}
    tags = data.GAAP_TAGS["revenue"]
    s = data._series(facts, tags, True)
    assert pd.Timestamp("2025-09-30") in set(s["end"])          # newer tag merged in
    # point in time: on 2026-01-15 the 2026-11-01 restated row was not yet filed
    assert data.ttm(s, pd.Timestamp("2026-01-15")) == 416.0


def test_execution_window_and_trigger(tmp_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from fh.runtime import execution_window
    from fh import approval
    ny = ZoneInfo("America/New_York")
    assert execution_window(datetime(2026, 10, 21, 10, 0, tzinfo=ny))[0] is True
    assert execution_window(datetime(2026, 10, 21, 9, 35, tzinfo=ny))[0] is False
    assert execution_window(datetime(2026, 10, 21, 15, 45, tzinfo=ny))[0] is False
    assert execution_window(datetime(2026, 10, 24, 11, 0, tzinfo=ny))[0] is False    # Saturday
    assert approval.needs_execution({"decision": "approve", "attempts": []}, "2026-10-21")
    assert not approval.needs_execution({"decision": "approve", "executed": True}, "2026-10-21")
    assert not approval.needs_execution({"decision": "approve",
                                         "attempts": [{"date": "2026-10-21"}]}, "2026-10-21")
    cfg = replace(Config(), state_dir=tmp_path, logs_dir=tmp_path / "logs")
    calls = []
    for open_ in (True, False):
        monkeypatch.setattr(approval, "execution_window",
                            lambda t=None, o=open_: (o, "" if o else "the US market is closed"))
        res = approval.trigger_execution(cfg, tmp_path, popen=lambda *a, **k: calls.append(a))
        assert res["started"] is open_
    assert len(calls) == 1 and "--on-approval" in calls[0][0]


def test_buy_freeze_defers_not_drops(cfg):
    """A sentinel freeze must not strip the buys out of an approved target for good."""
    from fh.pipeline import run_after_close
    from fh.approval import evaluate, record_decision
    import execute as ex
    uni, bars, fund = synth()
    L = Ledger.load(cfg)
    pk = run_after_close(cfg, "2026-08-21", uni, bars, fund, None, L, rebalance_shadow=True)
    folder = cfg.research_dir / "2026-08-21"
    top = [c["symbol"] for c in pk["candidates"][:12]]
    prop = {"decision": "initial deployment", "status": "proposed", "target_gross_exposure": 0.9,
            "holdings": [{"symbol": s, "weight": 0.075} for s in top]}
    (folder / "target_portfolio.json").write_text(json.dumps(prop))
    L = Ledger.load(cfg)
    L.buys_frozen_until = "2099-01-01"
    L.save(cfg)
    ev = evaluate(cfg, folder)
    assert ev["caps"]["buys_frozen"] and ev["approvable"]
    assert len(ev["final_target"]) == 12 and any("frozen" in w for w in ev["warnings"])
    rec = record_decision(cfg, folder, True, via="test")
    assert len(rec["target"]) == 12                                   # intended target kept
    # execution under the freeze: buys held back, nothing marked executed
    caps = RiskCaps(regime_cap=1.0, ladder_cap=1.0, halted=False, buys_frozen=True)
    screen = pd.read_csv(folder / "screen_full.csv", index_col=0)
    prices = {s: float(screen.loc[s, "price"]) for s in top}
    p2 = {"holdings": [{"symbol": s, "weight": w} for s, w in rec["target"].items()]}
    monkey_cfg = ex.cfg
    try:
        ex.cfg = cfg
        v = validate_target(p2, screen, L, prices, caps, cfg)
        orders = plan_orders(v.target, L, prices, cfg)
        held = ex.deferred_buys(p2, screen, L, prices, caps, orders, rec)
        assert not orders and held == sorted(top)
        assert ex.deferred_buys(p2, screen, L, prices, replace(caps, buys_frozen=False), orders, rec) == []
    finally:
        ex.cfg = monkey_cfg
