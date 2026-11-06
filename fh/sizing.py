"""Capital sizing: how well can a fund of a given size hit its target weights when it
must buy whole shares (US) or whole board lots (HK)?"""
from __future__ import annotations

from typing import Iterable, List

import numpy as np
import pandas as pd

from fh.markets import Market, round_trip_cost_pct


def sizing_table(prices: pd.Series, lots: pd.Series, capitals: Iterable[float], market: Market,
                 target_weight: float = 0.08, band: float = 0.02) -> pd.DataFrame:
    """One row per capital level.

    prices, lots: indexed by symbol (the candidate pool). lot = 1 for US stocks.
    """
    p = prices.astype(float)
    lot = lots.reindex(p.index).fillna(1).clip(lower=1).astype(float)
    lot_value = p * lot
    rows: List[dict] = []
    for cap in capitals:
        alloc = cap * target_weight
        n_lots = np.floor(alloc / lot_value)
        held = n_lots > 0
        real_w = n_lots * lot_value / cap
        miss = (real_w - target_weight).abs() / target_weight
        step = lot_value / cap                         # weight of the smallest possible trade
        rt = [round_trip_cost_pct(market, v, s) for v, s in
              zip(np.maximum(n_lots, 1) * lot_value, np.maximum(n_lots, 1) * lot)]
        rows.append({
            "capital": cap,
            "per_name": alloc,
            "unbuyable": int((~held).sum()),
            "unbuyable_names": ", ".join(p.index[~held][:8]),
            "median_weight_miss": float(miss[held].median()) if held.any() else np.nan,
            "p90_weight_miss": float(miss[held].quantile(0.9)) if held.any() else np.nan,
            "median_min_trade_weight": float(step.median()),
            "names_min_trade_gt_band": int((step > band).sum()),
            "median_round_trip_cost": float(np.median(rt)),
        })
    return pd.DataFrame(rows)


def recommend(table: pd.DataFrame, max_unbuyable: int = 0, max_median_miss: float = 0.05,
              max_band_breaches: int = 2) -> float | None:
    """Smallest capital meeting all three tests, or None."""
    ok = table[(table.unbuyable <= max_unbuyable) & (table.median_weight_miss <= max_median_miss)
               & (table.names_min_trade_gt_band <= max_band_breaches)]
    return float(ok.capital.min()) if len(ok) else None


def to_markdown(table: pd.DataFrame, ccy: str, band: float = 0.02) -> str:
    L = [f"| Capital ({ccy}) | Per name (8%) | Can't buy 1 lot | Median weight miss | 90th pct miss "
         f"| Median smallest trade | Names whose 1 lot > {band:.0%} band | Round-trip cost |",
         "|---|---|---|---|---|---|---|---|"]
    for r in table.itertuples():
        L.append(f"| {r.capital:,.0f} | {r.per_name:,.0f} | {r.unbuyable}"
                 f"{' (' + r.unbuyable_names + ')' if r.unbuyable else ''} | "
                 f"{r.median_weight_miss:.1%} | {r.p90_weight_miss:.1%} | "
                 f"{r.median_min_trade_weight:.2%} | {r.names_min_trade_gt_band} | "
                 f"{r.median_round_trip_cost:.2%} |")
    return "\n".join(L)
