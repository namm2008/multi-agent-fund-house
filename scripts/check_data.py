"""Audit the cached daily price history (no IBKR connection needed).

  python3 scripts/check_data.py           # report only
  python3 scripts/check_data.py --fix     # also repair obvious bad ticks / unadjusted splits

Reports: missing files, history start/end, gaps inside a name's history, and
one-day moves > 40%. A move is called a DATA ERROR (and fixed with --fix) only when
  * it fully reverses the next day (single bad print, e.g. CMCSA 2017-02-17 at half price), or
  * it is a split-like step (ratio ~2, 3/2, 3, 4, 5, 10 or their inverses) on ordinary volume
    that never reverses (history before an unadjusted split, e.g. RJF 2021-09-22).
Real events (earnings gaps, buyouts, crashes) are listed but never touched.
Fixed files are backed up to data/bars_backup/ first.
"""
import _boot  # noqa: F401
import argparse
import json
import shutil

import numpy as np
import pandas as pd

from fh.config import CONFIG as cfg
from fh import data

SPLIT_RATIOS = [2, 1.5, 3, 4, 5, 10, 1 / 2, 2 / 3, 1 / 3, 1 / 4, 1 / 5, 1 / 10]


def classify(df: pd.DataFrame):
    c, v = df["close"], df["volume"]
    r = c.pct_change()
    vol_ratio = v / v.rolling(20, min_periods=5).median().shift(1)
    out, skip = [], set()
    for i in np.where(r.abs() > 0.40)[0]:
        if i in skip:            # the reversal of a bad tick is not a separate event
            continue
        d = r.index[i]
        nxt = r.iloc[i + 1] if i + 1 < len(r) else np.nan
        ratio = c.iloc[i] / c.iloc[i - 1]
        vr = vol_ratio.iloc[i]
        if not np.isnan(nxt) and abs((1 + r.iloc[i]) * (1 + nxt) - 1) < 0.05:
            kind = "bad_tick"
            skip.add(i + 1)
        elif (min(abs(ratio / k - 1) for k in SPLIT_RATIOS) < 0.02
              and not np.isnan(vr) and vr < 2.5):   # needs a volume baseline: no IPO-week calls
            kind = "unadjusted_split"
        else:
            kind = "real_move"
        out.append({"date": str(d.date()), "move": round(float(r.iloc[i]), 3),
                    "volume_x_normal": None if np.isnan(vr) else round(float(vr), 1), "kind": kind,
                    "ratio": round(float(ratio), 3)})
    return out


def repair(df: pd.DataFrame, events) -> pd.DataFrame:
    df = df.copy()
    px = ["open", "high", "low", "close"]
    for e in events:
        d = pd.Timestamp(e["date"])
        if e["kind"] == "bad_tick":
            i = df.index.get_loc(d)
            df.iloc[i, [df.columns.get_loc(k) for k in px]] = (
                (df.iloc[i - 1][px].values + df.iloc[i + 1][px].values) / 2)
        elif e["kind"] == "unadjusted_split":
            k = min(SPLIT_RATIOS, key=lambda s: abs(e["ratio"] / s - 1))
            df.loc[df.index < d, px] = df.loc[df.index < d, px] * k
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--years", type=int, default=12)
    a = ap.parse_args()
    uni = data.load_universe(cfg)
    syms = uni["ib_symbol"].tolist()
    target = pd.Timestamp.now() - pd.DateOffset(years=a.years) + pd.Timedelta(days=31)
    bars, missing = {}, []
    for s in syms:
        d = data.load_bars(cfg, s)
        (missing.append(s) if d is None or d.empty else bars.__setitem__(s, d))
    cnt = pd.concat([pd.Series(1, index=d.index) for d in bars.values()]).groupby(level=0).size()
    cal = cnt[cnt >= 0.9 * cnt.max()].index
    last = max(d.index[-1] for d in bars.values())

    short = {s: str(d.index[0].date()) for s, d in bars.items() if d.index[0] > target}
    stale = {s: str(d.index[-1].date()) for s, d in bars.items() if d.index[-1] < last}
    gaps = {}
    for s, d in bars.items():
        span = cal[(cal >= d.index[0]) & (cal <= d.index[-1])]
        m = span.difference(d.index)
        if len(m):
            gaps[s] = {"missing_sessions": len(m), "first": str(m[0].date()), "last": str(m[-1].date())}
    events = {s: ev for s, d in bars.items() if (ev := classify(d))}
    errors = {s: [e for e in ev if e["kind"] != "real_move"] for s, ev in events.items()}
    errors = {s: ev for s, ev in errors.items() if ev}

    print(f"Universe {len(syms)} | files {len(bars)} | missing {len(missing)} {missing}")
    print(f"Latest bar: {last.date()} | names not up to date: {len(stale)} {stale}")
    print(f"Full {a.years}-year history: {len(bars) - len(short)} | shorter: {len(short)} "
          "(mostly IPOs / spin-offs / ticker changes; see report)")
    print(f"Names with gaps inside their history: {len(gaps)}")
    for s, g in sorted(gaps.items(), key=lambda kv: -kv[1]["missing_sessions"])[:10]:
        print(f"  {s}: {g}")
    print(f"One-day moves > 40%: {sum(len(v) for v in events.values())} in {len(events)} names; "
          f"classified as data errors: {sum(len(v) for v in errors.values())}")
    for s, ev in errors.items():
        for e in ev:
            print(f"  DATA ERROR {s} {e}")
    rep = {"latest_bar": str(last.date()), "missing_files": missing, "stale": stale,
           "short_history": short, "gaps": gaps, "big_moves": events, "data_errors": errors}
    out = cfg.data_dir / "data_quality_report.json"
    out.write_text(json.dumps(rep, indent=2))
    print(f"\nFull report: {out}")

    if a.fix and errors:
        bdir = cfg.data_dir / "bars_backup"
        bdir.mkdir(exist_ok=True)
        for s, ev in errors.items():
            p = data.bars_path(cfg, s)
            shutil.copyfile(p, bdir / p.name)
            fixed = repair(bars[s], ev)
            fixed.to_parquet(p)
            left = [e for e in classify(fixed) if e["kind"] != "real_move"]
            print(f"  fixed {s}: {[e['date'] for e in ev]} -> remaining errors {left}")


if __name__ == "__main__":
    main()
