"""Read-only IBKR snapshot for the dashboard. Never sends orders.

Writes
  state/account.json         whole paper account (balances, positions, open orders, FX)
  <fund state>/live_portfolio.json   for EVERY fund in fh/registry.py: the fund sleeve marked
                             to the latest prices: NAV, day P&L, drawdown vs HWM, per holding
"""
import _boot  # noqa: F401
from datetime import datetime

from fh.config import CONFIG as cfg
from fh.runtime import HKT, write_json

TAGS = {"NetLiquidation": "net_liquidation", "TotalCashValue": "cash",
        "UnrealizedPnL": "unrealized_pnl", "RealizedPnL": "realized_pnl",
        "BuyingPower": "buying_power", "GrossPositionValue": "gross_position_value",
        "AvailableFunds": "available_funds"}


def contract_for(spec, sym):
    from ib_async import Stock
    if spec.exchange == "SEHK":
        return Stock(str(int(sym)), "SEHK", "HKD")
    return Stock(sym.replace(" ", "."), spec.exchange, spec.currency)


def live_prices(ib, symbols, acct_prices, spec=None):
    """Latest price + previous close per symbol (delayed data is fine)."""
    from fh.ib import stock
    out = {}
    if not symbols:
        return out
    cons = [contract_for(spec, s) if spec else stock(s) for s in symbols]
    ib.qualifyContracts(*cons)
    for s, t in zip(symbols, ib.reqTickers(*cons)):
        ok = lambda x: x is not None and x == x and x > 0  # noqa: E731
        last = t.last if ok(t.last) else (t.marketPrice() if ok(t.marketPrice()) else None)
        src = "market data"
        if s in acct_prices and ok(acct_prices[s]):
            last, src = acct_prices[s], "IBKR portfolio"
        prev = t.close if ok(t.close) else None
        out[s] = {"price": last, "prev_close": prev, "source": src,
                  "bid": t.bid if ok(t.bid) else None, "ask": t.ask if ok(t.ask) else None,
                  "day_chg": (last / prev - 1) if (last and prev) else None}
    return out


def fund_live(ledger, quotes, cfg=cfg):
    pos, mv, day_pnl = {}, 0.0, 0.0
    for s, p in ledger.positions.items():
        q = quotes.get(s, {})
        px = q.get("price") or p.avg_cost
        val = p.qty * px
        mv += val
        if q.get("prev_close"):
            day_pnl += p.qty * (px - q["prev_close"])
        pos[s] = {**q, "price": px, "qty": p.qty, "avg_cost": p.avg_cost,
                  "market_value": round(val, 2),
                  "unrealized_pnl": round(p.qty * (px - p.avg_cost), 2),
                  "unrealized_pct": px / p.avg_cost - 1,
                  "from_peak": px / max(p.peak_close, px) - 1}
    nav = ledger.cash + mv
    for s in pos:
        pos[s]["weight"] = pos[s]["market_value"] / nav if nav else 0
    return {"nav": round(nav, 2), "cash": round(ledger.cash, 2), "market_value": round(mv, 2),
            "gross": mv / nav if nav else 0, "day_pnl": round(day_pnl, 2),
            "day_pct": day_pnl / (nav - day_pnl) if nav - day_pnl else None,
            "since_inception": nav / cfg.starting_capital - 1,
            "hwm": ledger.hwm, "drawdown": min(0.0, nav / ledger.hwm - 1) if ledger.hwm else 0.0,
            "positions": pos}


def fx_rates(ib, base: str, need, known: dict, log=print) -> tuple:
    """Units of `base` per 1 unit of each currency in `need`.
    IBKR only reports ExchangeRate for currencies the account holds, so missing pairs come from
    IBKR forex midpoints (IDEALPRO, free historical data), then Yahoo as a last resort."""
    from ib_async import Forex
    rates, src = {base: 1.0}, {base: "base"}
    for c, v in known.items():
        if c != "BASE" and v and v > 0:
            rates[c], src[c] = v, "IBKR account"

    def ib_mid(pair):
        try:
            c = Forex(pair)
            if not ib.qualifyContracts(c):
                return None
            bars = ib.reqHistoricalData(c, "", "3 D", "1 hour", "MIDPOINT", False, timeout=15)
            return float(bars[-1].close) if bars else None
        except Exception as e:  # noqa: BLE001
            log(f"fx {pair}: {type(e).__name__}: {e}")
            return None

    for c in sorted(set(need) - set(rates)):
        x = ib_mid(base + c)                      # BASE.CCY = CCY per 1 BASE
        if x:
            rates[c], src[c] = 1 / x, f"IBKR {base}.{c}"
            continue
        x = ib_mid(c + base)                      # CCY.BASE = BASE per 1 CCY
        if x:
            rates[c], src[c] = x, f"IBKR {c}.{base}"
            continue
        a, b = ib_mid(base + "USD"), (ib_mid("USD" + c) if c != "USD" else 1.0)
        if a and b:                               # cross via USD
            rates[c], src[c] = 1 / (a * b), f"IBKR {base}.USD x USD.{c}"
            continue
        try:
            import yfinance as yf
            h = yf.Ticker(f"{c}{base}=X").history(period="5d")
            if len(h):
                rates[c], src[c] = float(h["Close"].iloc[-1]), "Yahoo"
        except Exception as e:  # noqa: BLE001
            log(f"fx {c}: Yahoo failed: {e}")
    return rates, src


def hk_open_now() -> bool:
    n = datetime.now(HKT)
    m = n.hour * 60 + n.minute
    return n.weekday() < 5 and (570 <= m < 720 or 780 <= m < 960)


def main():
    from fh.ib import connect
    ib = connect(cfg, 34, readonly=True)
    try:
        acct = ib.managedAccounts()[0]
        # Values come per currency (base currency may be EUR/HKD for a non-US entity).
        by_ccy = {}
        for v in ib.accountSummary(acct):
            if v.tag in TAGS:
                try:
                    by_ccy.setdefault(v.currency or "BASE", {})[TAGS[v.tag]] = float(v.value)
                except ValueError:
                    pass
        fx = {}                                    # 1 unit of currency = fx[ccy] base currency
        for v in ib.accountValues(acct):
            if v.tag == "ExchangeRate" and v.currency:
                try:
                    fx[v.currency] = float(v.value)
                except ValueError:
                    pass
        ccy = next((c for c in ("USD", "BASE") if by_ccy.get(c, {}).get("net_liquidation") is not None),
                   next((c for c, d in by_ccy.items() if "net_liquidation" in d), None))
        vals = dict(by_ccy.get(ccy, {}))
        from fh import registry
        base = ccy if ccy and ccy != "BASE" else "USD"
        held_ccy = {p.contract.currency for p in ib.portfolio(acct) if p.contract.currency}
        fx, fx_src = fx_rates(ib, base, {f.currency for f in registry.FUNDS} | held_ccy, fx)
        vals["currency"] = ccy
        vals["all_currencies"] = by_ccy
        pos = []
        for p in ib.portfolio(acct):
            if p.contract.secType != "STK":
                continue
            pos.append({"symbol": p.contract.symbol, "currency": p.contract.currency,
                        "exchange": p.contract.primaryExchange or p.contract.exchange, "qty": float(p.position),
                        "avg_cost": round(p.averageCost, 4),
                        "market_price": round(p.marketPrice, 4),
                        "market_value": round(p.marketValue, 2),
                        "unrealized_pnl": round(p.unrealizedPNL, 2)})
        ib.reqAllOpenOrders()
        ib.sleep(1)
        orders = [{"symbol": t.contract.symbol, "currency": t.contract.currency, "action": t.order.action,
                   "qty": float(t.order.totalQuantity), "type": t.order.orderType,
                   "price": t.order.lmtPrice or t.order.auxPrice, "tif": t.order.tif,
                   "ref": t.order.orderRef, "status": t.orderStatus.status}
                  for t in ib.openTrades()]
        now = datetime.now(HKT).isoformat(timespec="seconds")
        write_json(cfg.state_dir / "account.json", {
            "time_hkt": now, "account": acct, "connected": True, **vals, "fx_to_base": fx, "fx_source": fx_src,
            "positions": pos, "open_orders": orders})

        from fh.ledger import Ledger
        from fh.runtime import market_open_now
        for spec in registry.FUNDS:
            try:
                fcfg = registry.load_config(spec)
                if spec.id != "us" and not (fcfg.state_dir / "ledger.json").exists():
                    continue                          # fund not trading yet
                ledger = Ledger.load(fcfg)
                acct_px = {p["symbol"]: p["market_price"] for p in pos if p["currency"] == spec.currency}
                quotes = live_prices(ib, sorted(ledger.positions), acct_px, spec)
                write_json(fcfg.state_dir / "live_portfolio.json", {
                    "time_hkt": now, "currency": spec.currency,
                    "market_open": market_open_now() if spec.id == "us" else hk_open_now(),
                    **fund_live(ledger, quotes, fcfg)})
            except Exception as e:  # noqa: BLE001  one fund failing never blocks the others
                print(f"{spec.id}: live portfolio failed: {type(e).__name__}: {e}")
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
