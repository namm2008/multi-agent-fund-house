"""Execution job (launchd: 21:40 and 22:40 HKT Mon–Fri; acts once, after the US open).

Order of business:
  1. Risk exits queued by daily_update (stops / halt)        -> automatic
  2. Deleverage queued by daily_update (gross above cap)      -> automatic
  3. Human-approved committee target (state/approved_target.json) -> once
  4. Refresh the GTC server-side stops.
  --dry-run prints what would be sent.
  --on-approval is started by the dashboard / approve.py right after an approval during
  US hours: it runs even if today's scheduled run already happened, but only if there is
  an approved target not yet attempted today.
"""
import _boot  # noqa: F401
import argparse
import sys
import time
from datetime import date

import pandas as pd

from fh.config import CONFIG as cfg
from fh.ledger import Ledger
from fh.orders import (Order, check_fat_finger, plan_orders, refresh_server_stops, send_orders,
                       sync_fh_fills)
from fh.risk import current_caps, deleverage_plan, validate_target
from fh.approval import needs_execution
from fh.runtime import (Log, alert, execution_window, market_open_now, now_et, read_json,
                        write_json)

log = Log(cfg, "execute")
LOCK_STALE_SEC = 30 * 60      # an older execute.lock is left over from a crash


def live_prices(ib, symbols):
    from fh.ib import stock
    cons = [stock(s) for s in symbols]
    ib.qualifyContracts(*cons)
    out = {}
    for s, t in zip(symbols, ib.reqTickers(*cons)):
        px = t.last if (t.last and t.last > 0) else t.marketPrice()
        if px and px == px and px > 0:
            out[s] = float(px)
    return out


def deferred_buys(prop, screen, ledger, prices, caps, orders, approved) -> list:
    """Symbols whose approved buys are held back only because of the sentinel's buy freeze."""
    if not caps.buys_frozen:
        return []
    from dataclasses import replace
    v_open = validate_target(prop, screen, ledger, prices, replace(caps, buys_frozen=False), cfg)
    want = plan_orders(v_open.target, ledger, prices, cfg)
    if approved.get("only"):
        want = [o for o in want if o.symbol in set(approved["only"])]
    now = {(o.symbol, o.side) for o in orders}
    return sorted(o.symbol for o in want if o.side == "BUY" and (o.symbol, "BUY") not in now)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="run even if already ran today")
    ap.add_argument("--on-approval", action="store_true",
                    help="started right after an approval (see module doc)")
    a = ap.parse_args()
    today = now_et().date().isoformat()
    marker = cfg.state_dir / "last_execute.txt"
    lock = cfg.state_dir / "execute.lock"      # sentinel pauses while this exists

    if a.on_approval:
        ok, why = execution_window()
        if not ok:
            log(f"on-approval run skipped: {why}; the scheduled run will trade")
            return
        # another run in progress: wait for it (it read the target before the approval)
        t0 = time.time()
        while lock.exists() and time.time() - lock.stat().st_mtime < LOCK_STALE_SEC \
                and time.time() - t0 < 20 * 60:
            time.sleep(15)
        if not needs_execution(read_json(cfg.state_dir / "approved_target.json", {}) or {},
                               today):
            log("on-approval run: nothing to do (target already executed or attempted today)")
            return
        log("on-approval run: approved target found, trading now")
    else:
        if not a.force and marker.exists() and marker.read_text().strip() == today:
            return
        if not market_open_now() and not a.dry_run:
            return
        if now_et().hour == 9 and now_et().minute < 40 and not a.dry_run:
            return   # let the opening auction settle
        if lock.exists() and time.time() - lock.stat().st_mtime < LOCK_STALE_SEC \
                and not a.dry_run:
            log("another execute run is in progress; skipping this one")
            return

    from fh.ib import connect
    lock.write_text(today)
    ib = None
    try:
        ib = connect(cfg, cfg.ib_client_id_exec, readonly=a.dry_run)
        ledger = Ledger.load(cfg)
        if not a.dry_run:
            sync_fh_fills(ib, ledger, cfg, log)     # recover fills missed in a disconnect
        latest = read_json(cfg.state_dir / "latest.json", {})
        pack = read_json(__import__("pathlib").Path(latest["pack"]), {}) if latest else {}
        screen_path = cfg.research_dir / latest.get("asof", "x") / "screen_full.csv"
        screen = pd.read_csv(screen_path, index_col=0) if screen_path.exists() else pd.DataFrame()
        sectors = screen["sector"].to_dict() if "sector" in screen else {}
        approved = read_json(cfg.state_dir / "approved_target.json", {}) or {}
        wanted = set(ledger.positions) | set(approved.get("target", {}))
        prices = live_prices(ib, sorted(wanted)) if wanted else {}
        sent = []

        # 0. fund restart in progress: sell everything, then reset the books
        from fh.restart import plan_path, reset_fund
        plan = read_json(plan_path(cfg), {}) or {}
        if plan.get("status") == "liquidating":
            orders = [Order(s, "SELL", p.qty, prices.get(s, p.avg_cost), "restart")
                      for s, p in ledger.positions.items()]
            log(f"RESTART: liquidating {[(o.symbol, o.qty) for o in orders]}")
            if a.dry_run:
                return
            if orders:
                sent += send_orders(ib, orders, ledger, cfg, sectors, log)
            refresh_server_stops(ib, ledger, cfg, log)       # cancels stops of sold names
            ledger.save(cfg)
            if ledger.positions:
                alert(cfg, "WARN", f"Restart: still holding {sorted(ledger.positions)}; "
                                   "will retry at the next US open")
                marker.write_text(today)
            else:
                arch = reset_fund(cfg, plan)
                log(f"RESTART complete: new capital ${plan['new_capital']:,.0f}, archive {arch}")
            return

        # 1. risk exits
        ex = read_json(cfg.state_dir / "pending_risk_exits.json", {}) or {}
        orders = [Order(e["symbol"], "SELL", ledger.positions[e["symbol"]].qty,
                        prices.get(e["symbol"], ledger.positions[e["symbol"]].avg_cost),
                        f"risk:{e['rule']}")
                  for e in ex.get("exits", []) if e["symbol"] in ledger.positions]
        # 2. deleverage
        dl = read_json(cfg.state_dir / "pending_deleverage.json", {}) or {}
        if dl and not orders:
            plan = deleverage_plan(ledger, prices, dl["target_gross"])
            orders = [Order(s, "SELL", q, prices[s], "risk:deleverage") for s, q in plan.items()]
        if orders:
            log(f"risk orders: {[(o.symbol, o.qty, o.reason) for o in orders]}")
            if not a.dry_run:
                sent += send_orders(ib, orders, ledger, cfg, sectors, log)
                write_json(cfg.state_dir / "pending_risk_exits.json", {"exits": []})
                write_json(cfg.state_dir / "pending_deleverage.json", {})

        # 3. approved committee rebalance (re-validated against CURRENT caps)
        if approved.get("decision") and not approved.get("executed"):
            prop = {"holdings": [{"symbol": s, "weight": w} for s, w in approved["target"].items()]}
            caps = current_caps(cfg, ledger, prices, pack.get("regime", {"exposure_cap": 0}))
            v = validate_target(prop, screen, ledger, prices, caps, cfg)
            for c in v.changes: log(f"  re-check changed: {c}")
            orders = plan_orders(v.target, ledger, prices, cfg)
            if approved.get("only"):        # a re-queued subset of an approved target
                orders = [o for o in orders if o.symbol in set(approved["only"])]
            held_back = deferred_buys(prop, screen, ledger, prices, caps, orders, approved)
            ff = check_fat_finger(orders, cfg)
            if ff:
                alert(cfg, "ERROR", "Rebalance blocked by fat-finger guard: " + "; ".join(ff))
            elif held_back:
                # sentinel freeze: send the sells now, keep the target open for the buys
                log(f"buys frozen until {ledger.buys_frozen_until}: holding back {held_back}; "
                    f"sending {[(o.side, o.qty, o.symbol) for o in orders]}")
                if not a.dry_run:
                    res = send_orders(ib, orders, ledger, cfg, sectors, log) if orders else []
                    sent += res
                    first = not approved.get("deferred")
                    approved.setdefault("deferred", []).append({"date": today, "held_back": held_back,
                                                                "frozen_until": ledger.buys_frozen_until,
                                                                "results": res})
                    if first:
                        alert(cfg, "INFO", f"Approved target {approved['folder']}: buys of {', '.join(held_back)} "
                                           f"wait until the crash-sentinel freeze ends ({ledger.buys_frozen_until}); "
                                           "the target stays open and trades at the first run after that")
                    write_json(cfg.state_dir / "approved_target.json", approved)
                    write_json(cfg.research_dir / approved["folder"] / "execution.json", approved)
            else:
                log(f"rebalance orders: {[(o.side, o.qty, o.symbol) for o in orders]}")
                if not a.dry_run:
                    res = send_orders(ib, orders, ledger, cfg, sectors, log) if orders else []
                    sent += res
                    approved.setdefault("attempts", []).append({"date": today, "results": res})
                    approved["results"] = [r for at in approved["attempts"] for r in at["results"]]
                    open_ = [r for r in res if r["filled"] < r["qty"] - 1e-9]
                    n = len(approved["attempts"])
                    ap = read_json(cfg.research_dir / approved["folder"] / "approval.json", {}) or {}
                    if open_ and n < cfg.rebalance_retry_days:
                        approved["outstanding"] = open_
                        alert(cfg, "WARN", f"{len(open_)} rebalance order(s) not (fully) filled: "
                              + ", ".join(f"{r['side']} {r['symbol']} {r['filled']:g}/{r['qty']:g}" for r in open_)
                              + f" — will retry at the next US open (attempt {n}/{cfg.rebalance_retry_days})")
                    else:
                        approved["executed"] = today
                        ap["executed"] = today
                        if open_:
                            approved["outstanding"] = open_
                            alert(cfg, "ERROR", f"Gave up after {n} sessions on: "
                                  + ", ".join(f"{r['side']} {r['symbol']}" for r in open_)
                                  + " — the committee will see the gap next week")
                        else:
                            approved.pop("outstanding", None)
                    ap["attempts"] = n
                    ap["outstanding"] = approved.get("outstanding", [])
                    write_json(cfg.state_dir / "approved_target.json", approved)
                    write_json(cfg.research_dir / approved["folder"] / "execution.json", approved)
                    write_json(cfg.research_dir / approved["folder"] / "approval.json", ap)

        if not a.dry_run:
            refresh_server_stops(ib, ledger, cfg, log)
            ledger.save(cfg)
            marker.write_text(today)
        if sent:
            alert(cfg, "INFO", f"Executed {len(sent)} orders: "
                               + ", ".join(f"{r['side']} {r['filled']}/{r['qty']} {r['symbol']}"
                                           for r in sent))
    finally:
        if ib is not None:
            ib.disconnect()
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        alert(cfg, "ERROR", f"execute failed: {type(e).__name__}: {e}")
        raise
