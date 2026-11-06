"""Crash sentinel — runs all the time (launchd KeepAlive), acts only in US hours.

Every `sentinel_interval_sec` during the regular session:
  1. Marks the fund with live (or 15-min delayed) prices -> intraday drawdown.
  2. Market proxy = equal-weight move of a mega-cap basket vs prior close
     (SPX/VIX indices are used as well if your data subscriptions allow).
  3. Applies, AUTOMATICALLY (no approval needed):
       shock  -> freeze new buys for 3 sessions + alert
       crash  -> cut gross exposure to `crash_exposure` now (pro-rata sells)
       ladder -> if fund drawdown crosses a ladder step, cut gross to that step
  4. Kill switch: create a file named STOP_TRADING in the repo root to halt.

No LLM is in this loop on purpose: it must be fast, cheap and predictable.
"""
import _boot  # noqa: F401
import time
from datetime import timedelta

from fh.config import CONFIG as cfg
from fh.ledger import Ledger, drawdown_cap
from fh.risk import crash_level, deleverage_plan
from fh.runtime import Log, alert, market_open_now, now_et, read_json, write_json

log = Log(cfg, "sentinel")


def snapshot_prices(ib, symbols):
    from fh.ib import stock
    cons = [stock(s) for s in symbols]
    ib.qualifyContracts(*cons)
    tickers = ib.reqTickers(*cons)
    last, prev = {}, {}
    for s, t in zip(symbols, tickers):
        px = t.last if (t.last and t.last > 0) else t.marketPrice()
        if px and px == px and px > 0:
            last[s] = float(px)
        if t.close and t.close > 0:
            prev[s] = float(t.close)
    return last, prev


def index_quotes(ib):
    """SPX and VIX if subscribed; (None, None) otherwise."""
    try:
        from ib_async import Index
        spx, vix = Index("SPX", "CBOE"), Index("VIX", "CBOE")
        ib.qualifyContracts(spx, vix)
        ts = ib.reqTickers(spx, vix)
        spx_chg = (ts[0].last / ts[0].close - 1) if ts[0].last and ts[0].close else None
        vix_px = ts[1].last if ts[1].last and ts[1].last > 0 else None
        return spx_chg, vix_px
    except Exception:  # noqa: BLE001
        return None, None


def execute_sells(ib, ledger, plan, prices, reason):
    from fh.orders import Order, send_orders, refresh_server_stops
    orders = [Order(s, "SELL", q, prices.get(s, ledger.positions[s].avg_cost), reason)
              for s, q in plan.items()]
    if not orders:
        return
    res = send_orders(ib, orders, ledger, cfg, {}, log)
    alert(cfg, "RISK", f"{reason}: sold {[(r['symbol'], r['filled']) for r in res]}")
    refresh_server_stops(ib, ledger, cfg, log)


def tick(ib, state):
    lock = cfg.state_dir / "execute.lock"
    if lock.exists() and time.time() - lock.stat().st_mtime < 900:
        return                      # execute.py is trading; avoid two writers on the ledger
    ledger = Ledger.load(cfg)
    if cfg.kill_switch.exists() and not ledger.halted:
        ledger.halted, ledger.halt_reason = True, "kill switch file"
        ledger.save(cfg)
        alert(cfg, "CRITICAL", "Kill switch found: fund halted (existing positions kept; "
                               "run scripts/liquidate.py to go to cash)")
    basket = list(dict.fromkeys(cfg.market_proxy + list(ledger.positions)))
    last, prev = snapshot_prices(ib, basket)
    proxy = [last[s] / prev[s] - 1 for s in cfg.market_proxy if s in last and s in prev]
    proxy_chg = sum(proxy) / len(proxy) if len(proxy) >= 8 else None
    spx_chg, vix = index_quotes(ib)
    mkt_chg = spx_chg if spx_chg is not None else proxy_chg

    # 3-day proxy move: compare with closes saved by earlier ticks
    hist = state.setdefault("proxy_close_hist", {})
    today = now_et().date().isoformat()
    three_day = None
    days = sorted(hist)
    if len(days) >= 3 and mkt_chg is not None:
        base = hist[days[-3]]
        three_day = (1 + mkt_chg) * hist[days[-1]] / base - 1 if base else None

    prices = {s: last.get(s, p.avg_cost) for s, p in ledger.positions.items()}
    nav, dd = ledger.nav(prices), ledger.drawdown(prices)
    gross = ledger.gross_exposure(prices)
    level = crash_level(mkt_chg, three_day, vix, cfg)
    # blind: no index data and fewer than 8 proxy names priced (e.g. IBKR error 10197,
    # "no market data during competing live session" while the live account is logged in)
    if mkt_chg is None:
        state["blind_ticks"] = state.get("blind_ticks", 0) + 1
        if state["blind_ticks"] >= 3 and state.get("blind_alerted") != today:
            alert(cfg, "WARN", f"Crash sentinel is blind: only {len(proxy)} of {len(cfg.market_proxy)} proxy "
                               "stocks have live prices, so shock/crash rules cannot fire. Usual cause: you are "
                               "logged in to the LIVE IBKR account (mobile app, TWS or Client Portal), which takes "
                               "the market data from the paper session (IBKR error 10197).")
            state["blind_alerted"] = today
    else:
        state["blind_ticks"] = 0
    state["last"] = {"time_et": now_et().isoformat(timespec="seconds"), "nav": round(nav, 2),
                     "drawdown": round(dd, 4), "gross": round(gross, 4),
                     "market_chg": mkt_chg, "three_day": three_day, "vix": vix,
                     "level": level if mkt_chg is not None else "blind", "proxy_n": len(proxy)}

    # --- rules ---
    if level in ("shock", "crash") and state.get("alerted_level_" + today) != level:
        freeze = (now_et() + timedelta(days=4)).date().isoformat()
        ledger.buys_frozen_until = max(ledger.buys_frozen_until or "", freeze)
        ledger.save(cfg)
        alert(cfg, "RISK", f"Market {level}: proxy {mkt_chg:+.1%}, 3-day {'n/a' if three_day is None else format(three_day, '+.1%')}, "
                           f"VIX {vix if vix is not None else 'n/a'}. "
                           f"New buys frozen until {freeze}.")
        state["alerted_level_" + today] = level

    target = None
    if level == "crash" and gross > cfg.crash_exposure + 0.02:
        target, why = cfg.crash_exposure, f"CRASH rule (market {mkt_chg:+.1%})"
    ladder = drawdown_cap(dd, cfg.dd_ladder)
    if gross > ladder + 0.02 and (target is None or ladder < target):
        target, why = ladder, f"DRAWDOWN LADDER ({dd:.1%} -> max gross {ladder:.0%})"
    if ladder == 0.0 and not ledger.halted:
        ledger.halted, ledger.halt_reason = True, f"hard floor intraday: dd {dd:.1%}"
        ledger.save(cfg)
        alert(cfg, "CRITICAL", f"Hard floor hit intraday ({dd:.1%}). Fund HALTED, going to cash.")
    if target is not None:
        plan = deleverage_plan(ledger, prices, target)
        log(f"{why}: selling {plan}")
        execute_sells(ib, ledger, plan, prices, why)

    if mkt_chg is not None and now_et().hour >= 15 and now_et().minute >= 50:
        # remember an approximate close level of the proxy for the 3-day rule
        lvl = hist.get(days[-1], 1.0) * (1 + mkt_chg) if days else 1.0
        hist[today] = lvl
        for d in sorted(hist)[:-5]:
            del hist[d]
    write_json(cfg.state_dir / "sentinel_state.json", state)


def main():
    from fh.ib import connect, IBError
    cfg.ensure_dirs()
    state = read_json(cfg.state_dir / "sentinel_state.json", {})
    ib = None
    log("sentinel started")
    while True:
        try:
            if not market_open_now():
                if ib is not None:
                    ib.disconnect()
                    ib = None
                time.sleep(60)
                continue
            if ib is None or not ib.isConnected():
                ib = connect(cfg, cfg.ib_client_id_sentinel, readonly=False)
                state.pop("conn_alerted", None)
            tick(ib, state)
            ib.sleep(cfg.sentinel_interval_sec)
        except IBError as e:
            if not state.get("conn_alerted"):
                alert(cfg, "ERROR", f"Sentinel cannot reach IB Gateway during market hours: {e}")
                state["conn_alerted"] = True
            ib = None
            time.sleep(120)
        except KeyboardInterrupt:
            break
        except Exception as e:  # noqa: BLE001
            log(f"tick error {type(e).__name__}: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
