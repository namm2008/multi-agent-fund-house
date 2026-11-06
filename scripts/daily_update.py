"""After-close job (launchd: Tue–Sat 06:30 HKT).

Downloads bars/fundamentals/macro, books any server-side stop fills, marks the
fund, checks stops and the drawdown ladder, builds the research pack.

  python scripts/daily_update.py            # normal
  python scripts/daily_update.py --no-ib    # rebuild from cached data only
  python scripts/daily_update.py --source yahoo   # prices from Yahoo (IBKR history down)
"""
import argparse
import _boot  # noqa: F401
from datetime import date

import pandas as pd

from fh.config import CONFIG as cfg
from fh import data
from fh.ledger import Ledger
from fh.pipeline import run_after_close
from fh.runtime import Log, alert, last_session_date

log = Log(cfg, "daily_update")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ib", action="store_true")
    ap.add_argument("--refresh-universe", action="store_true")
    ap.add_argument("--source", choices=["auto", "ibkr", "yahoo"], default="auto",
                    help="auto = IBKR, Yahoo for whatever IBKR cannot deliver")
    a = ap.parse_args()
    cfg.ensure_dirs()
    asof = last_session_date()
    is_weekend_run = date.today().weekday() == 5      # Saturday HKT = after Friday close

    uni = data.load_universe(cfg, refresh=a.refresh_universe or is_weekend_run)
    symbols = uni["ib_symbol"].tolist()
    ledger = Ledger.load(cfg)
    symbols += [s for s in ledger.positions if s not in symbols]

    ib = None
    if not a.no_ib:
        from fh.ib import connect
        try:
            ib = connect(cfg, cfg.ib_client_id_data, readonly=False)
        except Exception as e:  # noqa: BLE001
            if a.source != "yahoo":
                raise
            log(f"IB Gateway not reachable ({str(e).splitlines()[0]}); prices from Yahoo, "
                f"stop fills and reconcile skipped")
    if ib is not None:
        from fh.orders import sync_server_stop_fills
        try:
            sync_server_stop_fills(ib, ledger, cfg, log)
            if a.source == "yahoo":
                st = data.yahoo_update_bars(cfg, symbols, log)
            else:
                st = data.update_bars(ib, cfg, symbols, log, fallback=a.source == "auto")
            bad = {s: v for s, v in st.items() if v != "ok"}
            if bad:
                log(f"bars: {len(bad)} failures e.g. {list(bad.items())[:5]}")
            pos = {p.contract.symbol.replace(".", " "): float(p.position)
                   for p in ib.positions() if p.contract.secType == "STK"
                   and p.contract.currency == "USD"}      # HK fund positions live in the same account
            from fh.ledger import reconcile
            issues = reconcile(ledger, pos)
            if issues:
                alert(cfg, "WARN", "Ledger vs IBKR mismatch: " + "; ".join(issues))
        finally:
            ib.disconnect()
    elif a.source == "yahoo":
        st = data.yahoo_update_bars(cfg, symbols, log)
        bad = {s: v for s, v in st.items() if v != "ok"}
        if bad:
            log(f"bars: {len(bad)} failures e.g. {list(bad.items())[:5]}")

    fpath = cfg.data_dir / "fundamentals.parquet"
    if is_weekend_run or not fpath.exists():
        log("refreshing fundamentals from SEC EDGAR")
        fund = data.update_fundamentals(cfg, uni, log)
    else:
        fund = pd.read_parquet(fpath)
    try:   # SEC filings: whole universe on Saturdays, holdings + watch list on other days
        from fh import filings
        watch = filings.watch_symbols(cfg, ledger.positions)
        st = filings.update_filings(cfg, uni, uni["ib_symbol"] if is_weekend_run else watch,
                                    watch, log)
        if st["failed"] and len(st["failed"]) > 20:
            alert(cfg, "WARN", f"SEC filings: {len(st['failed'])} companies failed to refresh "
                  f"(EDGAR unreachable or rate-limited); the pack's filings section may be stale")
    except Exception as e:  # noqa: BLE001
        log(f"filings refresh failed ({e})")
        alert(cfg, "WARN", f"SEC filings refresh failed: {str(e)[:300]}")
    try:
        macro = data.update_macro(cfg, log)
        if macro.attrs.get("fred_failed"):
            alert(cfg, "WARN", f"FRED: {len(macro.attrs['fred_failed'])} series failed after "
                  f"retries, using cached values: {macro.attrs['fred_failed']} "
                  f"(see logs/daily_update.log for what FRED returned)")
    except Exception as e:  # noqa: BLE001
        log(f"macro refresh failed ({e}); using cache")
        alert(cfg, "WARN", f"Macro (FRED) refresh failed, pack macro table and regime VIX/HY "
              f"inputs are from cache: {str(e)[:300]}")
        mp = cfg.data_dir / "macro.parquet"
        macro = pd.read_parquet(mp) if mp.exists() else None

    bars = data.load_all_bars(cfg, symbols)
    fresh = data.bars_freshness(bars, asof)
    if fresh["is_stale"]:
        alert(cfg, "ERROR", f"Price data stale: {fresh['stale']}/{fresh['symbols']} symbols have "
              f"no bar for {asof} (median last bar {fresh['median_last_bar']}). The research "
              f"pack is flagged and the committee will block. Fix: check IB Gateway, then "
              f"python3 scripts/daily_update.py --source yahoo")
    run_after_close(cfg, asof, uni, bars, fund, macro, ledger,
                    rebalance_shadow=is_weekend_run, log=log)
    if ib is not None:
        import subprocess, sys
        from pathlib import Path
        subprocess.run([sys.executable, str(Path(__file__).parent / "account_snapshot.py")],
                       timeout=60, check=False)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        alert(cfg, "ERROR", f"daily_update failed: {type(e).__name__}: {e}")
        raise
