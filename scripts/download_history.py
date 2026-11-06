"""Pull long daily history for the backtest (IB Gateway must be running).

  python3 scripts/download_history.py --years 12                 # everything
  python3 scripts/download_history.py --years 12 --resume        # only names not yet complete
  python3 scripts/download_history.py --years 12 --only CSX,PEP  # specific names

Survives a Gateway restart: it reconnects and carries on. A name is only
overwritten when the new download succeeded. Ends with a coverage report and
then refreshes SEC fundamentals. ~20-40 minutes for ~500 names (IBKR pacing).
"""
import _boot  # noqa: F401
import argparse
import time

import pandas as pd

from fh.config import CONFIG as cfg
from fh import data
from fh.ib import IBError, connect, stock

ap = argparse.ArgumentParser()
ap.add_argument("--years", type=int, default=12)
ap.add_argument("--only", default="", help="comma-separated IBKR symbols")
ap.add_argument("--resume", action="store_true",
                help="skip names whose cached history already starts within ~1 month of the target")
ap.add_argument("--skip-fundamentals", action="store_true")
a = ap.parse_args()
cfg.ensure_dirs()
uni = data.load_universe(cfg, refresh=not a.only)
syms = [s.strip() for s in a.only.split(",") if s.strip()] or uni["ib_symbol"].tolist()
target_start = pd.Timestamp.now() - pd.DateOffset(years=a.years)

if a.resume:
    todo = []
    for s in syms:
        old = data.load_bars(cfg, s)
        if old is None or old.index[0] > target_start + pd.Timedelta(days=31):
            todo.append(s)
    print(f"--resume: {len(syms) - len(todo)} already complete, {len(todo)} to fetch")
    syms = todo


def get_ib():
    for attempt in range(30):                       # up to ~15 minutes of waiting
        try:
            return connect(cfg, cfg.ib_client_id_data)
        except IBError as e:
            print(f"  Gateway not reachable ({attempt + 1}/30), retrying in 30s: {str(e).splitlines()[0]}")
            time.sleep(30)
    raise SystemExit("Gave up: IB Gateway did not come back.")


ok, failed = [], {}
ib = get_ib()
try:
    for i, s in enumerate(syms):
        for attempt in range(3):
            try:
                if not ib.isConnected():
                    print("  connection lost — reconnecting")
                    ib = get_ib()
                c = stock(s)
                ib.qualifyContracts(c)
                bars = ib.reqHistoricalData(c, "", f"{a.years} Y", "1 day", "ADJUSTED_LAST", True)
                if not bars:
                    raise RuntimeError("no bars returned")
                df = pd.DataFrame([{"date": pd.Timestamp(b.date), "open": b.open, "high": b.high,
                                    "low": b.low, "close": b.close, "volume": b.volume}
                                   for b in bars]).set_index("date")
                df.to_parquet(data.bars_path(cfg, s))
                ok.append(s)
                failed.pop(s, None)
                break
            except Exception as e:  # noqa: BLE001
                failed[s] = f"{type(e).__name__}: {e}"
                time.sleep(5)
        if i % 25 == 24:
            print(f"{i + 1}/{len(syms)}  (failed so far: {len(failed)})")
        ib.sleep(2.0)
finally:
    try:
        ib.disconnect()
    except Exception:  # noqa: BLE001
        pass

print(f"\nDownloaded {len(ok)}/{len(syms)}. Failed: {len(failed)}")
for s, e in failed.items():
    print(f"  {s}: {e[:120]}")
if failed:
    print("Re-run with:  python3 scripts/download_history.py --years "
          f"{a.years} --only {','.join(failed)}")
if not a.skip_fundamentals and not a.only:
    print("fundamentals (SEC EDGAR)…")
    data.update_fundamentals(cfg, uni)
print("Check coverage/quality with:  python3 scripts/check_data.py")
