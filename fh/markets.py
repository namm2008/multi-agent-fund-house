"""Market specifications: everything that differs between markets (US now; HK spec kept for a future fund).

The rest of the code asks the market for contracts, lot sizes, trading costs and
session times instead of assuming US conventions.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import time
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Market:
    code: str                       # "US" | "HK"
    exchange: str                   # IBKR routing exchange
    currency: str
    tz: str                         # exchange time zone
    sessions: Tuple[Tuple[time, time], ...]
    # costs per side, as fractions of trade value (+ minimum commission in local ccy)
    commission_rate: float
    commission_min: float
    commission_per_share: float = 0.0
    stamp_duty: float = 0.0         # charged on BOTH buy and sell in HK
    exchange_fees: float = 0.0
    lot_based: bool = False         # True -> orders must be whole board lots
    yahoo_suffix: str = ""
    vol_index: str = "VIX"

    def contract(self, symbol: str):
        from ib_async import Stock
        return Stock(symbol, "SMART" if self.code == "US" else self.exchange, self.currency,
                     primaryExchange=None if self.code == "US" else self.exchange)

    def cost(self, value: float, shares: float = 0.0) -> float:
        """Estimated cost of ONE side of a trade, in local currency."""
        if value <= 0:
            return 0.0
        if self.commission_per_share:
            comm = min(max(self.commission_min, shares * self.commission_per_share), 0.01 * value)
        else:
            comm = max(self.commission_min, self.commission_rate * value)
        stamp = math.ceil(self.stamp_duty * value) if self.stamp_duty else 0.0   # HK rounds up to $1
        return comm + stamp + self.exchange_fees * value

    def round_qty(self, qty: float, lot: int = 1, down: bool = True) -> float:
        """Round a share quantity to a whole number of board lots."""
        lot = max(int(lot or 1), 1)
        n = qty / lot
        n = math.floor(n + 1e-9) if down else math.ceil(n - 1e-9)
        return float(max(n, 0) * lot)

    def ib_symbol(self, code: str) -> str:
        """HK stock code '0700' / '700.HK' -> IBKR symbol '700'."""
        if self.code == "HK":
            digits = "".join(ch for ch in str(code) if ch.isdigit())
            return str(int(digits)) if digits else str(code)
        return str(code).replace(".", " ")

    def yahoo_symbol(self, ib_sym: str) -> str:
        if self.code == "HK":
            return f"{int(ib_sym):04d}.HK"
        return ib_sym.replace(" ", "-")


US = Market(code="US", exchange="SMART", currency="USD", tz="America/New_York",
            sessions=((time(9, 30), time(16, 0)),),
            commission_rate=0.0, commission_min=1.0, commission_per_share=0.005,
            exchange_fees=0.0000278, yahoo_suffix="", vol_index="VIX")

# HK: IBKR fixed 0.08% (min HK$18); stamp duty 0.1% each side; HKEX trading fee
# 0.00565%, SFC levy 0.0027%, AFRC levy 0.00015%, CCASS ~0.0042%  => ~0.013% fees.
HK = Market(code="HK", exchange="SEHK", currency="HKD", tz="Asia/Hong_Kong",
            sessions=((time(9, 30), time(12, 0)), (time(13, 0), time(16, 0))),
            commission_rate=0.0008, commission_min=18.0, stamp_duty=0.001,
            exchange_fees=0.000128, lot_based=True, yahoo_suffix=".HK", vol_index="VHSI")

MARKETS: Dict[str, Market] = {"US": US, "HK": HK}


def round_trip_cost_pct(m: Market, value: float, shares: float = 0.0) -> float:
    return 2 * m.cost(value, shares) / value if value > 0 else float("nan")
