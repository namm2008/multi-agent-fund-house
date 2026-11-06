"""Refresh SEC EDGAR filings (8-K, 10-Q/10-K, late-filing notices) on demand.

  python3 scripts/update_filings.py              # holdings + watch list (top-60 + target)
  python3 scripts/update_filings.py --all        # whole universe (~500 requests, ~2 min)
  python3 scripts/update_filings.py --symbols LLY,NVDA
daily_update.py runs this automatically (whole universe on Saturdays).
"""
import argparse
import _boot  # noqa: F401

from fh.config import CONFIG as cfg
from fh import data, filings
from fh.ledger import Ledger


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--symbols", default="")
    a = ap.parse_args()
    cfg.ensure_dirs()
    uni = data.load_universe(cfg)
    held = list(Ledger.load(cfg).positions)
    watch = filings.watch_symbols(cfg, held)
    syms = ([s.strip().upper() for s in a.symbols.split(",") if s.strip()] or
            (uni["ib_symbol"].tolist() if a.all else watch))
    st = filings.update_filings(cfg, uni, syms, set(watch) | set(syms if a.symbols else []), print)
    f = filings.recent(cfg, held, filings.pd.Timestamp.today().strftime("%Y-%m-%d"), days=30)
    print(f"\nHoldings: {len(f)} filings in the last 30 days")
    for _, r in f.iterrows():
        print(f"  {r['filed']} {r['symbol']:6s} {r['form']:7s} {r['severity']:5s} "
              f"{filings._labels(r['form'], r['items'])}")
    if st["failed"]:
        print(f"\nFailed: {st['failed'][:20]}{' ...' if len(st['failed']) > 20 else ''}")


if __name__ == "__main__":
    main()
