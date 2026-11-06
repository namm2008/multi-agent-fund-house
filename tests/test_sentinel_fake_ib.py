"""Sentinel + order path against a fake IB (no network)."""
import sys, types
import pytest
from dataclasses import replace
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from fh.config import Config
from fh.ledger import Ledger


class T:  # ticker
    def __init__(s, last, close): s.last, s.close, s.bid, s.ask = last, close, last - .01, last + .01
    def marketPrice(s): return s.last


class FakeIB:
    def __init__(s, moves): s.moves, s.placed = moves, []
    def qualifyContracts(s, *c): return c
    def reqTickers(s, *cons):
        out = []
        for c in cons:
            if type(c).__name__ == "Index": raise RuntimeError("no index data")
            out.append(T(100 * (1 + s.moves.get(c.symbol, s.moves["*"])), 100))
        return out
    def reqMktData(s, c, *a): return T(100 * (1 + s.moves.get(c.symbol, s.moves["*"])), 100)
    def sleep(s, x): pass
    def placeOrder(s, c, o):
        s.placed.append((c.symbol, o.action, o.totalQuantity,
                         o.lmtPrice if o.orderType == "LMT" else o.orderType, o.orderRef))
        s.n = getattr(s, "n", 0) + 1
        mkt = 100 * (1 + s.moves.get(c.symbol, s.moves["*"]))
        px = o.lmtPrice if o.orderType == "LMT" else mkt
        ex = types.SimpleNamespace(shares=o.totalQuantity, price=px,
                                   execId=f"E{s.n}", orderRef=o.orderRef, side=o.action)
        fill = types.SimpleNamespace(execution=ex, commissionReport=types.SimpleNamespace(commission=1.0),
                                     time=None, contract=c)
        return types.SimpleNamespace(fills=[fill], isDone=lambda: True, order=o,
                                     orderStatus=types.SimpleNamespace(status="Filled"))
    def openTrades(s): return []
    def reqAllOpenOrders(s): return []
    def cancelOrder(s, o): pass


def test_crash_deleverages(tmp_path, monkeypatch):
    import sentinel
    cfg = replace(Config(), starting_capital=10_000.0, max_order_value=2_000.0, state_dir=tmp_path, logs_dir=tmp_path, kill_switch=tmp_path / "K")
    cfg.ensure_dirs()
    monkeypatch.setattr(sentinel, "cfg", cfg)
    monkeypatch.setattr(sentinel, "log", lambda *a: None)   # never touch the real log
    import fh.orders as fo
    L = Ledger.load(cfg)
    for s in ("AAA", "BBB"):
        L.apply_fill(s, "BUY", 45, 100, 0)          # 90% invested
    L.save(cfg)
    ib = FakeIB({"*": -0.06})                       # market -6% intraday -> crash
    sentinel.tick(ib, {})
    L2 = Ledger.load(cfg)
    prices = {s: 94.0 for s in L2.positions}
    sells = [p for p in ib.placed if p[1] == "SELL" and not p[4].startswith("FH-STOP")]
    assert sells, ib.placed
    assert L2.gross_exposure(prices) <= cfg.crash_exposure + 0.03
    assert L2.buys_frozen_until
    stops = [p for p in ib.placed if p[4].startswith("FH-STOP")]
    assert {p[0] for p in stops} == set(L2.positions)


def test_normal_day_no_trades(tmp_path, monkeypatch):
    import sentinel
    cfg = replace(Config(), starting_capital=10_000.0, max_order_value=2_000.0, state_dir=tmp_path, logs_dir=tmp_path, kill_switch=tmp_path / "K")
    monkeypatch.setattr(sentinel, "cfg", cfg)
    monkeypatch.setattr(sentinel, "log", lambda *a: None)
    L = Ledger.load(cfg); L.apply_fill("AAA", "BUY", 45, 100, 0); L.save(cfg)
    ib = FakeIB({"*": -0.005})
    st = {}
    sentinel.tick(ib, st)
    assert ib.placed == [] and st["last"]["level"] == "normal"


class SlowIB(FakeIB):
    """Market trades above our first limit: nothing fills until the order is re-priced."""
    def __init__(s, market):
        super().__init__({"*": 0.0}); s.market = market; s.trades = []
    def reqMktData(s, c, *a):
        t = T(s.market, s.market); return t
    def placeOrder(s, c, o):
        s.placed.append((c.symbol, o.action, o.totalQuantity,
                         o.lmtPrice if o.orderType == "LMT" else (o.orderType, o.algoStrategy), o.orderRef))
        if o.orderType != "LMT":
            filled = True
        elif o.action == "BUY":
            filled = o.lmtPrice >= s.market
        else:
            filled = o.lmtPrice <= s.market
        s.n = getattr(s, "n", 0) + 1
        ex = types.SimpleNamespace(shares=o.totalQuantity, price=s.market, execId=f"X{s.n}",
                                   orderRef=o.orderRef, side=o.action)
        fills = [types.SimpleNamespace(execution=ex, commissionReport=types.SimpleNamespace(commission=1.0),
                                       time=None, contract=c)] if filled else []
        return types.SimpleNamespace(fills=fills, isDone=lambda: filled, order=o,
                                     orderStatus=types.SimpleNamespace(status="Filled" if filled else "Submitted"))


def _cfg(tmp_path, style="limit"):
    cfg = replace(Config(), starting_capital=10_000.0, max_order_value=2_000.0, state_dir=tmp_path, logs_dir=tmp_path, kill_switch=tmp_path / "K",
                  order_reprice_after_sec=5, order_wait_sec=10, order_style=style)
    cfg.ensure_dirs()
    return cfg


def test_limit_is_marketable_with_cap(tmp_path):
    from fh.orders import Order, send_orders
    cfg = _cfg(tmp_path)
    L = Ledger.load(cfg)
    ib = SlowIB(market=226.0)
    res = send_orders(ib, [Order("NVDA", "BUY", 2, 224.0, "rebalance")], L, cfg, {}, lambda *a: None)
    # limit = max(ask, last, close) x 1.005 -> above the market -> fills
    assert res[0]["filled"] == 2 and res[0]["limit"] >= 226.0
    assert L.positions["NVDA"].qty == 2


def test_unfilled_order_reported_not_hidden(tmp_path):
    from fh.orders import Order, send_orders
    cfg = _cfg(tmp_path)
    L = Ledger.load(cfg)
    ib = SlowIB(market=226.0)
    ib.reqMktData = lambda c, *a: T(200.0, 200.0)      # stale quote far below the market
    res = send_orders(ib, [Order("NVDA", "BUY", 2, 200.0, "rebalance")], L, cfg, {}, lambda *a: None)
    assert res[0]["status"] == "unfilled" and res[0]["filled"] == 0
    assert "NVDA" not in L.positions


def test_sync_recovers_missed_fill_once(tmp_path):
    from fh.orders import sync_fh_fills
    cfg = _cfg(tmp_path)
    L = Ledger.load(cfg)
    ex = types.SimpleNamespace(shares=1.0, price=253.03, execId="AME1", orderRef="FH-rebalance", side="BOT")
    f = types.SimpleNamespace(execution=ex, commissionReport=types.SimpleNamespace(commission=1.0),
                              time=None, contract=types.SimpleNamespace(symbol="AME"))
    ib = types.SimpleNamespace(reqExecutions=lambda *a: [f])
    assert len(sync_fh_fills(ib, L, cfg, lambda *a: None)) == 1
    assert L.positions["AME"].qty == 1 and L.cash == pytest.approx(10_000 - 254.03)
    assert sync_fh_fills(ib, L, cfg, lambda *a: None) == []          # never booked twice


def test_reconcile_both_directions():
    from fh.ledger import reconcile
    L = Ledger(cash=0); L.apply_fill("ABBV", "BUY", 2, 260, 0)
    issues = reconcile(L, {"ABBV": 2.0, "AME": 1.0})
    assert len(issues) == 1 and "AME" in issues[0]


def test_price_protection_rejection_is_resubmitted(tmp_path):
    """15 Oct: limit from the previous close (221.30) was rejected; market was 216.01."""
    from fh.orders import Order, send_orders
    cfg = _cfg(tmp_path)
    L = Ledger.load(cfg)
    ib = SlowIB(market=216.01)
    quote = T(float("nan"), 221.30); quote.bid = quote.ask = float("nan")   # only prev close known
    ib.reqMktData = lambda c, *a: quote
    real_place = ib.placeOrder
    def place(c, o):
        if o.lmtPrice > 216.01 * 1.029:              # IBKR price protection
            msg = ("We cannot accept an order at a limit price at or more aggressive than 222.36. "
                   "Please submit your order using a limit price that is closer to the current market price of 216.01.")
            return types.SimpleNamespace(fills=[], isDone=lambda: True, order=o,
                                         orderStatus=types.SimpleNamespace(status="Cancelled"),
                                         log=[types.SimpleNamespace(message=msg)])
        return real_place(c, o)
    ib.placeOrder = place
    res = send_orders(ib, [Order("NVDA", "BUY", 2, 221.3, "rebalance")], L, cfg, {}, lambda *a: None)
    assert res[0]["filled"] == 2 and 216.01 <= res[0]["limit"] <= 216.01 * 1.006


def test_restart_liquidates_archives_and_resets(tmp_path):
    from fh.restart import create_plan, reset_fund
    from fh.orders import Order, send_orders
    import json as _json
    root = tmp_path / "fund"
    (root / "fh").mkdir(parents=True)
    cfgfile = root / "fh" / "config.py"
    cfgfile.write_text('class C:\n    starting_capital: float = 10_000.0\n    inception_date: str = "2026-10-05"\n'
                       '    max_order_value: float = 2_000.0          # fat-finger guard per order\n')
    cfg = replace(Config(), starting_capital=10_000.0, max_order_value=2_000.0, state_dir=root / "state", research_dir=root / "research",
                  logs_dir=root / "logs", data_dir=root / "data", kill_switch=root / "K",
                  order_reprice_after_sec=5, order_wait_sec=10)
    cfg.ensure_dirs()
    (cfg.research_dir / "2026-10-12").mkdir()
    L = Ledger.load(cfg); L.apply_fill("ABBV", "BUY", 2, 100, 1); L.save(cfg)
    (cfg.state_dir / "nav.csv").write_text("date,nav\n2026-10-14,10000\n")
    plan = create_plan(cfg, 200_000, "test")
    with pytest.raises(RuntimeError):                 # refuses while not flat
        reset_fund(cfg, plan, config_path=cfgfile, today="2026-10-16")
    ib = FakeIB({"*": 0.10})                          # market 110, sells fill at the limit
    send_orders(ib, [Order("ABBV", "SELL", 2, 110, "restart")], L, cfg, {}, lambda *a: None)
    L.save(cfg)
    arch = reset_fund(cfg, plan, config_path=cfgfile, today="2026-10-16")
    new = Ledger.load(cfg)
    assert new.cash == 200_000 and new.hwm == 200_000 and not new.positions
    assert (arch / "state" / "nav.csv").exists() and (arch / "research" / "2026-10-12").exists()
    assert not (cfg.state_dir / "nav.csv").exists()
    txt = cfgfile.read_text()
    assert "starting_capital: float = 200_000.0" in txt and 'inception_date: str = "2026-10-16"' in txt
    assert "max_order_value: float = 30_000.0" in txt and "# fat-finger" in txt
    summ = _json.loads((arch / "SUMMARY.json").read_text())
    assert summ["final_nav_cash"] == pytest.approx(L.cash, abs=0.01) and summ["final_nav_cash"] > 10_000


def test_default_style_is_adaptive_market_and_fills_on_stale_quote(tmp_path):
    """16 Oct: delayed quote 213.50 vs market 240.16 — a market-type order still fills."""
    from fh.orders import Order, send_orders
    cfg = _cfg(tmp_path, style="adaptive")
    assert Config().order_style == "adaptive"
    L = Ledger.load(cfg); L.apply_fill("DGX", "BUY", 2, 230, 0)
    ib = SlowIB(market=240.16)
    ib.reqMktData = lambda c, *a: T(213.50, 213.50)
    res = send_orders(ib, [Order("DGX", "SELL", 2, 213.5, "restart")], L, cfg, {}, lambda *a: None)
    assert res[0]["filled"] == 2 and res[0]["avg_price"] == pytest.approx(240.16)
    assert ib.placed[0][3] == ("MKT", "Adaptive") and "DGX" not in L.positions


def test_rejected_algo_falls_back_to_plain_market(tmp_path):
    from fh.orders import Order, send_orders
    cfg = _cfg(tmp_path, style="adaptive")
    L = Ledger.load(cfg)
    ib = SlowIB(market=100.0)
    real = ib.placeOrder
    def place(c, o):
        if o.algoStrategy == "Adaptive":
            return types.SimpleNamespace(fills=[], isDone=lambda: True, order=o, log=[],
                                         orderStatus=types.SimpleNamespace(status="Cancelled"))
        return real(c, o)
    ib.placeOrder = place
    res = send_orders(ib, [Order("AME", "BUY", 1, 100, "rebalance")], L, cfg, {}, lambda *a: None)
    assert res[0]["filled"] == 1 and res[0]["limit"] == "MKT"
