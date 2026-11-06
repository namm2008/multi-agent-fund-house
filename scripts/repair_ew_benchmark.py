"""Rebuild the equal-weight benchmark column (ew_level) in state/nav.csv by chaining
daily equal-weight returns from 1.0 on the first recorded date. Keeps a backup copy
(state/nav.csv.bak-<timestamp>). Safe to re-run."""
import _boot  # noqa: F401
import shutil
import time

import pandas as pd

from fh.config import CONFIG as cfg
from fh import data, factors

p = cfg.state_dir / "nav.csv"
nav = pd.read_csv(p)
shutil.copyfile(p, cfg.state_dir / f"nav.csv.bak-{time.strftime('%Y%m%d-%H%M%S')}")
uni = data.load_universe(cfg)
bars = data.load_all_bars(cfg, uni["ib_symbol"].tolist())
r = factors.ew_daily_returns(bars)
old = nav["ew_level"].copy()
level, prev = 1.0, None
new = []
for d in nav["date"]:
    if prev is not None:
        win = r[(r.index > pd.Timestamp(prev)) & (r.index <= pd.Timestamp(d))]
        level *= float((1 + win).prod())
    new.append(round(level, 6))
    prev = d
nav["ew_level"] = new
nav.to_csv(p, index=False)
print(pd.DataFrame({"date": nav["date"], "old": old, "new": nav["ew_level"],
                    "index_100": (nav["ew_level"] * 100).round(2)}).to_string(index=False))
