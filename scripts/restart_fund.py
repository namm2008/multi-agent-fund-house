"""Restart the fund with new capital.

  python3 scripts/restart_fund.py --capital 200000            # queue: sell all at next US open, then reset
  python3 scripts/restart_fund.py --capital 200000 --now      # reset immediately (only if already flat)

The previous period (ledger, NAV history, alerts, audit, research folders) is moved
to archive/period-<old inception>-to-<date>/ — nothing is deleted.
"""
import _boot  # noqa: F401
import argparse

from fh.config import CONFIG as cfg
from fh.ledger import Ledger
from fh.restart import create_plan, plan_path, reset_fund
from fh.runtime import read_json

ap = argparse.ArgumentParser()
ap.add_argument("--capital", type=float, required=True)
ap.add_argument("--by", default="owner")
ap.add_argument("--now", action="store_true")
a = ap.parse_args()
plan = create_plan(cfg, a.capital, a.by)
held = Ledger.load(cfg).positions
if a.now:
    if held:
        raise SystemExit(f"Not flat ({sorted(held)}); run without --now to liquidate at the next open.")
    print("archived to", reset_fund(cfg, plan))
else:
    print(f"Queued. {len(held)} positions will be sold at the next US open: {sorted(held)}")
    print("Status:", read_json(plan_path(cfg))["status"])
