"""Turn target weights into orders, and send them to IBKR."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

from fh.ledger import Ledger


@dataclass
class Order:
    symbol: str
    side: str          # BUY / SELL
    qty: float
    ref_price: float
    reason: str

    @property
    def value(self) -> float:
        return self.qty * self.ref_price


def plan_orders(target: Dict[str, float], ledger: Ledger, prices: Dict[str, float],
                cfg, reason: str = "rebalance") -> List[Order]:
    nav = ledger.nav(prices)
    cur_w = ledger.weights(prices)
    orders: List[Order] = []
    for s in sorted(set(target) | set(cur_w)):
        px = prices.get(s)
        if not px:
            continue
        tw, cw = target.get(s, 0.0), cur_w.get(s, 0.0)
        full_exit = tw == 0 and cw > 0
        if not full_exit and abs(tw - cw) < cfg.rebalance_band:
            continue
        held = ledger.positions[s].qty if s in ledger.positions else 0.0
        want = tw * nav / px
        delta = want - held
        if cfg.allow_fractional:
            q = round(abs(delta), 4)
        else:
            q = float(math.floor(abs(delta))) if delta > 0 else float(math.ceil(abs(delta) - 1e-9))
        if full_exit:
            q = held
        q = min(q, held) if delta < 0 else q
        if q <= 0:
            continue
        orders.append(Order(s, "BUY" if delta > 0 else "SELL", q, px,
                            "exit" if full_exit else reason))
    # sells first so buys are funded; buys limited by available cash
    orders.sort(key=lambda o: 0 if o.side == "SELL" else 1)
    cash = ledger.cash + sum(o.value for o in orders if o.side == "SELL")
    cash -= cfg.cash_buffer * nav
    final = []
    for o in orders:
        if o.side == "BUY":
            if o.value > cash:
                affordable = math.floor(cash / o.ref_price) if not cfg.allow_fractional \
                    else round(cash / o.ref_price, 4)
                if affordable <= 0:
                    continue
                o.qty = float(affordable)
            cash -= o.value
        final.append(o)
    return final


def check_fat_finger(orders: List[Order], cfg) -> List[str]:
    return [f"{o.side} {o.qty} {o.symbol} ≈ ${o.value:,.0f} exceeds per-order cap "
            f"${cfg.max_order_value:,.0f}" for o in orders if o.value > cfg.max_order_value]


def orders_to_dicts(orders: List[Order]) -> List[dict]:
    return [dict(asdict(o), value=round(o.value, 2)) for o in orders]


# --------------------------------------------------------------------------- #
# IBKR execution
# --------------------------------------------------------------------------- #
def _ref_price(ib, c, side: str, fallback: float):
    """Best available price right now. Paper accounts often only get 15-min delayed
    quotes, so take the HIGHER of last/ask (buys) or LOWER of last/bid (sells)."""
    t = ib.reqMktData(c, "", True, False)       # snapshot
    ib.sleep(2)
    ok = lambda x: x is not None and x == x and x > 0  # noqa: E731
    # Today's prices only. The previous close is NOT a candidate: on 15 Oct NVDA's prior
    # close (221.30) sat 2.4% above the market (216.01) and IBKR's price-protection rejected
    # the limit as "too aggressive".
    cands = [x for x in ((t.ask, t.last) if side == "BUY" else (t.bid, t.last)) if ok(x)]
    if cands:
        return (max(cands) if side == "BUY" else min(cands)), "quote"
    if ok(t.close):
        return t.close, "prev close"
    return fallback, "fallback"


def _rejection_price(trade):
    """IBKR price-protection rejections quote the current market price; use it."""
    import re
    for e in reversed(getattr(trade, "log", []) or []):
        m = re.search(r"current market price of ([0-9]+(?:\.[0-9]+)?)", e.message or "")
        if m:
            return float(m.group(1))
    return None


def _limit(px: float, side: str, cfg) -> float:
    cap = cfg.limit_cap_bps / 1e4
    return round(px * (1 + cap) if side == "BUY" else px * (1 - cap), 2)


def _make_order(o: "Order", cfg, lmt: Optional[float], style: str):
    """'adaptive' = IBKR Adaptive algo market order (IBKR's own suggestion when it rejects a
    limit as too aggressive); 'market' = plain market order; 'limit' = capped limit."""
    from ib_async import LimitOrder, MarketOrder, TagValue
    ref = f"FH-{o.reason}"[:40]
    if style == "limit":
        return LimitOrder(o.side, o.qty, lmt, tif=cfg.order_tif, orderRef=ref)
    m = MarketOrder(o.side, o.qty, tif=cfg.order_tif, orderRef=ref)
    if style == "adaptive":
        m.algoStrategy = "Adaptive"
        m.algoParams = [TagValue("adaptivePriority", "Normal")]
    return m


def send_orders(ib, orders: List[Order], ledger: Ledger, cfg, sectors: Dict[str, str],
                log=print, wait_sec: Optional[int] = None) -> List[dict]:
    """Send orders, wait for fills, book every fill into the ledger by IBKR execution id
    (never twice; fills lost in a disconnect are recovered by sync_fh_fills).

    Default style (cfg.order_style = "adaptive"): IBKR Adaptive market orders, sent only
    during regular hours by execute.py/sentinel. History of why:
      * 2 & 13 Oct: limits priced from 15-min-delayed quotes never filled (NVDA);
      * 15 Oct: a limit built from the previous close was rejected as "too aggressive";
      * 16 Oct: delayed quotes were up to 12% stale on a news day (DGX 213.50 vs 240.16),
        so SELL limits were rejected and FTNT's limit sat above the market.
    Delayed quotes cannot price a limit reliably, so the fund lets IBKR's algo work the
    order; the fat-finger guard still checks size against the reference price.
    With cfg.order_style = "limit" (needs live quotes) the old capped-limit path is used."""
    from fh.ib import stock
    wait_sec = wait_sec or cfg.order_wait_sec
    style = getattr(cfg, "order_style", "adaptive")
    trades = []
    for o in orders:
        c = stock(o.symbol)
        ib.qualifyContracts(c)
        lmt = None
        if style == "limit":
            px, src = _ref_price(ib, c, o.side, o.ref_price)
            lmt = _limit(px, o.side, cfg)
            log(f"  sent {o.side} {o.qty} {o.symbol} LMT {lmt} (ref {px:.2f} from {src})")
        else:
            log(f"  sent {o.side} {o.qty} {o.symbol} {style.upper()} MKT (ref {o.ref_price:.2f})")
        trade = ib.placeOrder(c, _make_order(o, cfg, lmt, style))
        trades.append([o, trade, lmt if lmt is not None else style.upper(), c])

    ib.sleep(5)                                        # rejected at once? re-submit once
    for row in trades:
        o, t, lmt, c = row
        if t.isDone() and not t.fills and t.orderStatus.status in ("Cancelled", "ApiCancelled", "Inactive"):
            if style == "limit":
                mkt = _rejection_price(t)
                px, src = (mkt, "IBKR market price") if mkt else _ref_price(ib, c, o.side, o.ref_price)
                new_lmt, new_style = _limit(px, o.side, cfg), "limit"
                note = f"LMT {new_lmt} (ref {px:.2f} from {src})"
            else:                                      # algo not accepted -> plain market order
                new_lmt, new_style, note = None, "market", "plain MKT"
            row[1] = ib.placeOrder(c, _make_order(o, cfg, new_lmt, new_style))
            row[2] = new_lmt if new_lmt is not None else "MKT"
            log(f"  {o.symbol} {style} order rejected (status {t.orderStatus.status}); re-submitted {note}")

    def wait(seconds):
        waited = 0
        while waited < seconds and any(not t.isDone() for _, t, _, _ in trades):
            ib.sleep(5)
            waited += 5

    wait(cfg.order_reprice_after_sec)
    if style == "limit":
        for row in trades:                               # one re-price for stragglers
            o, t, lmt, c = row
            if not t.isDone() and isinstance(lmt, float):
                px, src = _ref_price(ib, c, o.side, o.ref_price)
                new = _limit(px, o.side, cfg)
                if (o.side == "BUY" and new > lmt) or (o.side == "SELL" and new < lmt):
                    t.order.lmtPrice = new
                    ib.placeOrder(c, t.order)
                    row[2] = new
                    log(f"  re-priced {o.side} {o.symbol} LMT {lmt} -> {new}")
    wait(max(0, wait_sec - cfg.order_reprice_after_sec))

    results = []
    for o, t, lmt, c in trades:
        if not t.isDone():
            ib.cancelOrder(t.order)
            ib.sleep(1)
        filled = 0.0
        for f in t.fills:
            filled += f.execution.shares
            _book(ledger, o.symbol, o.side, f, sectors.get(o.symbol, ""), o.reason)
        status = t.orderStatus.status if filled >= o.qty - 1e-9 else (
            "partial" if filled > 0 else "unfilled")
        avg = (sum(f.execution.shares * f.execution.price for f in t.fills) / filled) if filled else None
        results.append(dict(symbol=o.symbol, side=o.side, qty=o.qty, limit=lmt,
                            filled=filled, avg_price=avg, status=status))
        if filled:
            log(f"  filled {o.side} {filled:g} {o.symbol} @ {avg:.2f}")
        ledger.save(cfg)
    return results


def _booked_ids(ledger: Ledger) -> set:
    return {f.get("exec_id") for f in ledger.fills if f.get("exec_id")}


def _book(ledger: Ledger, symbol, side, fill, sector, reason) -> bool:
    eid = fill.execution.execId
    if eid in _booked_ids(ledger):
        return False
    comm = (fill.commissionReport.commission or 0.0) if fill.commissionReport else 0.0
    if comm != comm or comm > 1e6:          # IBKR sends 1.7e308 until the report arrives
        comm = 0.0
    qty = fill.execution.shares
    if side == "SELL" and symbol in ledger.positions:
        qty = min(qty, ledger.positions[symbol].qty)
    ledger.apply_fill(symbol, side, qty, fill.execution.price, comm, sector,
                      when=fill.time.astimezone().isoformat(timespec="seconds")
                      if getattr(fill, "time", None) else None, reason=reason)
    ledger.fills[-1]["exec_id"] = eid
    return True


def sync_fh_fills(ib, ledger: Ledger, cfg, log=print) -> List[dict]:
    """Book any fund (orderRef FH-*) execution the ledger has not seen yet:
    server-side stops that fired while the Mac was off, and fills that arrived
    while the API connection was down (the AME fill on 2 Oct was lost this way).
    IBKR returns executions of roughly the last 24h-7 days, so run it daily."""
    from ib_async import ExecutionFilter
    booked = []
    for f in ib.reqExecutions(ExecutionFilter()):
        ref = f.execution.orderRef or ""
        if not ref.startswith("FH-") or f.execution.execId in _booked_ids(ledger):
            continue
        side = "BUY" if f.execution.side.upper().startswith("B") else "SELL"
        sym = f.contract.symbol
        if side == "SELL" and sym not in ledger.positions:
            continue
        reason = "server_stop" if ref.startswith("FH-STOP-") else ref[3:] + " (recovered)"
        if _book(ledger, sym, side, f, "", reason):
            booked.append({"symbol": sym, "side": side, "qty": f.execution.shares,
                           "price": f.execution.price, "ref": ref})
            log(f"  recovered fill {side} {f.execution.shares} {sym} @ {f.execution.price} ({ref})")
    if booked:
        ledger.save(cfg)
    return booked


def sync_server_stop_fills(ib, ledger: Ledger, cfg, log=print) -> List[dict]:
    """Backwards-compatible name."""
    return sync_fh_fills(ib, ledger, cfg, log)


def refresh_server_stops(ib, ledger: Ledger, cfg, log=print) -> None:
    """Keep one GTC stop per holding parked on IBKR's servers at
    avg_cost x (1 - server_side_stop). It protects the fund if the Mac is off."""
    from ib_async import StopOrder
    from fh.ib import stock
    ib.reqAllOpenOrders()
    ib.sleep(1)
    open_trades = [t for t in ib.openTrades() if (t.order.orderRef or "").startswith("FH-STOP-")]
    by_sym = {t.order.orderRef[8:]: t for t in open_trades}
    for s, p in ledger.positions.items():
        stop_px = round(p.avg_cost * (1 - cfg.server_side_stop), 2)
        t = by_sym.pop(s, None)
        if t and abs(t.order.totalQuantity - p.qty) < 1e-9 and abs(t.order.auxPrice - stop_px) < 0.01:
            continue
        if t:
            ib.cancelOrder(t.order)
        c = stock(s)
        ib.qualifyContracts(c)
        ib.placeOrder(c, StopOrder("SELL", p.qty, stop_px, tif="GTC",
                                   orderRef=f"FH-STOP-{s}", outsideRth=False))
        log(f"  server stop {s}: SELL {p.qty} @ STP {stop_px}")
    for s, t in by_sym.items():          # stops for positions we no longer hold
        ib.cancelOrder(t.order)
        log(f"  cancelled stale stop {s}")

