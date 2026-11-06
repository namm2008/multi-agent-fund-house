"""Pre-flight: python packages, IB Gateway (paper), SEC EDGAR, FRED, universe."""
import _boot  # noqa: F401
import importlib

from fh.config import CONFIG as cfg

ok = True
for m in ("pandas", "numpy", "pyarrow", "requests", "ib_async"):
    try:
        importlib.import_module(m); print(f"[ok] {m}")
    except ImportError:
        ok = False; print(f"[!!] {m} missing -> pip install -r requirements.txt")

import requests
for name, url, hdr in [
    ("SEC EDGAR", "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
     {"User-Agent": cfg.sec_user_agent}),
    ("FRED", "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10", {}),
    ("S&P 500 list", cfg.universe_source, {})]:
    try:
        r = requests.get(url, headers=hdr, timeout=20)
        print(f"[{'ok' if r.ok else '!!'}] {name} HTTP {r.status_code}")
        ok &= r.ok
    except Exception as e:  # noqa: BLE001
        ok = False; print(f"[!!] {name}: {e}")

try:
    from fh.ib import connect
    ib = connect(cfg, 39)
    acct = ib.managedAccounts()
    summ = {v.tag: v.value for v in ib.accountSummary() if v.tag in ("NetLiquidation", "TotalCashValue")}
    print(f"[ok] IB Gateway paper {acct} {summ}")
    from fh.ib import stock
    c = stock("AAPL"); ib.qualifyContracts(c)
    t = ib.reqTickers(c)[0]
    print(f"[ok] AAPL quote last={t.last} close={t.close} (delayed is fine)")
    bars = ib.reqHistoricalData(c, "", "1 M", "1 day", "ADJUSTED_LAST", True)
    print(f"[ok] historical daily bars: {len(bars)}")
    ib.disconnect()
except Exception as e:  # noqa: BLE001
    ok = False; print(f"[!!] IBKR: {e}")
print("\nALL GOOD" if ok else "\nFix the [!!] items above.")
