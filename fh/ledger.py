"""The fund's own book: cash, positions, NAV history, high-water mark.

The fund is a $10k *sleeve* whatever the paper account's balance is. Every fill
updates the ledger; NAV = cash + sum(qty x last price). The IBKR account is
reconciled against it (see reconcile()), never the other way round.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd


@dataclass
class Position:
    qty: float
    avg_cost: float
    entry_date: str
    peak_close: float
    sector: str = ""


@dataclass
class Ledger:
    cash: float
    positions: Dict[str, Position] = field(default_factory=dict)
    hwm: float = 0.0
    hwm_date: str = ""
    risk_period_start: str = ""     # reset only by a human (scripts/reset_hwm.py)
    halted: bool = False
    halt_reason: str = ""
    buys_frozen_until: str = ""     # ISO date; sentinel shock freeze
    fills: List[dict] = field(default_factory=list)

    # ---------------------------------------------------------------- io
    @classmethod
    def load(cls, cfg) -> "Ledger":
        p = cfg.state_dir / "ledger.json"
        if not p.exists():
            return cls(cash=cfg.starting_capital, hwm=cfg.starting_capital,
                       hwm_date=cfg.inception_date,
                       risk_period_start=cfg.inception_date)
        raw = json.loads(p.read_text())
        raw["positions"] = {k: Position(**v) for k, v in raw.get("positions", {}).items()}
        return cls(**raw)

    def save(self, cfg) -> None:
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        d = asdict(self)
        tmp = cfg.state_dir / "ledger.json.tmp"
        tmp.write_text(json.dumps(d, indent=2, default=str))
        tmp.replace(cfg.state_dir / "ledger.json")    # atomic

    # ---------------------------------------------------------------- book
    def apply_fill(self, symbol: str, side: str, qty: float, price: float,
                   commission: float, sector: str = "", when: Optional[str] = None,
                   reason: str = "") -> None:
        when = when or datetime.now().isoformat(timespec="seconds")
        signed = qty if side == "BUY" else -qty
        self.cash -= signed * price + commission
        pos = self.positions.get(symbol)
        if side == "BUY":
            if pos:
                new_qty = pos.qty + qty
                pos.avg_cost = (pos.avg_cost * pos.qty + price * qty) / new_qty
                pos.qty = new_qty
            else:
                self.positions[symbol] = Position(qty, price, when[:10], price, sector)
        else:
            if not pos or pos.qty < qty - 1e-9:
                raise ValueError(f"Sell {qty} {symbol} exceeds fund position")
            pos.qty -= qty
            if pos.qty <= 1e-9:
                del self.positions[symbol]
        self.fills.append(dict(time=when, symbol=symbol, side=side, qty=qty,
                               price=price, commission=commission, reason=reason))

    def market_value(self, prices: Dict[str, float]) -> float:
        return sum(p.qty * prices.get(s, p.avg_cost) for s, p in self.positions.items())

    def nav(self, prices: Dict[str, float]) -> float:
        return self.cash + self.market_value(prices)

    def gross_exposure(self, prices: Dict[str, float]) -> float:
        n = self.nav(prices)
        return self.market_value(prices) / n if n > 0 else 0.0

    def weights(self, prices: Dict[str, float]) -> Dict[str, float]:
        n = self.nav(prices)
        return {s: p.qty * prices.get(s, p.avg_cost) / n for s, p in self.positions.items()}

    def mark(self, prices: Dict[str, float], asof: str) -> dict:
        """Update HWM/peaks with closing prices. Returns the NAV record."""
        n = self.nav(prices)
        if n > self.hwm:
            self.hwm, self.hwm_date = n, asof
        for s, p in self.positions.items():
            if s in prices:
                p.peak_close = max(p.peak_close, prices[s])
        return {"date": asof, "nav": round(n, 2), "cash": round(self.cash, 2),
                "gross": round(self.gross_exposure(prices), 4),
                "hwm": round(self.hwm, 2), "drawdown": round(n / self.hwm - 1, 4),
                "positions": len(self.positions)}

    def drawdown(self, prices: Dict[str, float]) -> float:
        return self.nav(prices) / self.hwm - 1 if self.hwm > 0 else 0.0


def append_nav(cfg, rec: dict) -> None:
    p = cfg.state_dir / "nav.csv"
    df = pd.DataFrame([rec])
    if p.exists():
        old = pd.read_csv(p)
        old = old[old["date"] != rec["date"]]
        df = pd.concat([old, df])
    df.to_csv(p, index=False)


def drawdown_cap(dd: float, ladder) -> float:
    """Max gross exposure permitted at drawdown `dd` (negative number)."""
    cap = 1.0
    for thr, c in sorted(ladder, key=lambda x: -x[0]):   # -0.08, -0.12, -0.15
        if dd <= thr:
            cap = c
    return cap


def reconcile(ledger: Ledger, ib_positions: Dict[str, float], tol: float = 1e-6) -> List[str]:
    """Ledger vs IBKR account, both directions: a fund position missing from the account,
    or an account position the fund does not know about (e.g. a fill lost in a disconnect)."""
    issues = []
    for s, p in ledger.positions.items():
        held = ib_positions.get(s, 0.0)
        if abs(held - p.qty) > tol:
            issues.append(f"{s}: ledger {p.qty:g} vs account {held:g}")
    for s, q in ib_positions.items():
        if s not in ledger.positions and abs(q) > tol:
            issues.append(f"{s}: account holds {q:g} that the fund ledger does not know about")
    return issues
