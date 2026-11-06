"""Every fund the house runs. Add a fund here (plus its folder/config) and the dashboard,
the account snapshot and the cover page pick it up.

    id          short id used in URLs (/#/us/overview)
    label       name on the fund switcher
    config      (folder relative to the repo root, module name) of the fund's Config object
    kind        dashboard adapter: "us" (US quant + committee);
                a new strategy either reuses one or adds an adapter in dashboard/adapters.py
    currency    fund currency (ledger, NAV, orders at IBKR)
    exchange    IBKR exchange for live prices
    order_ref   orderRef prefix the fund's orders carry at IBKR
"""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class FundSpec:
    id: str
    label: str
    config: tuple
    kind: str
    currency: str
    exchange: str
    order_ref: str


FUNDS = [
    FundSpec("us", "US Equity", ("", "fh.config"), "us", "USD", "SMART", "FH-"),
]


def get(fid: str) -> FundSpec:
    for f in FUNDS:
        if f.id == fid:
            return f
    raise KeyError(fid)


def load_config(spec: FundSpec):
    folder, module = spec.config
    if folder:
        p = str(ROOT / folder)
        if p not in sys.path:
            sys.path.insert(0, p)
    return importlib.import_module(module).CONFIG
