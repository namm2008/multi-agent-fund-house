import numpy as np
import pandas as pd

from fh import data


def _bars(start, n, px=100.0):
    idx = pd.bdate_range(start, periods=n)
    c = px * np.cumprod(1 + np.random.default_rng(0).normal(0, 0.01, n))
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1e6},
                        index=pd.Index(idx, name="date"))


def test_merge_rescales_old_history_without_seam():
    old = _bars("2025-01-01", 300)
    new = old.tail(10).copy() * 0.98            # other feed, 2% lower adjustment base
    new.loc[new.index[-1] + pd.offsets.BDay(1)] = new.iloc[-1] * 1.01
    m = data._merge_external(old, new)
    r_old = old["close"].pct_change().iloc[1:-10]
    r_m = m["close"].pct_change().loc[r_old.index]
    assert np.allclose(r_old, r_m)               # returns unchanged
    assert len(m) == 301 and m.index.is_unique


def test_merge_refuses_unadjusted_split():
    old = _bars("2025-01-01", 300)
    new = old.tail(5).copy() / 4
    assert data._merge_external(old, new) is None


def test_freshness():
    b = {"A": _bars("2026-01-01", 200), "B": _bars("2026-01-01", 210)}
    f = data.bars_freshness(b, str(b["B"].index[-1].date()))
    assert f["stale"] == 1 and f["is_stale"]


def test_dead_ibkr_switches_to_fallback(monkeypatch, tmp_path):
    class Cfg:
        data_dir = tmp_path
    (tmp_path / "bars").mkdir()

    class DeadIB:
        calls = 0
        def qualifyContracts(self, c): pass
        def reqHistoricalData(self, *a, **k):
            DeadIB.calls += 1
            return []
        def sleep(self, s): pass

    got = {}
    monkeypatch.setattr(data, "yahoo_update_bars",
                        lambda cfg, syms, log=print: got.setdefault("s", syms) and {s: "ok" for s in syms})
    monkeypatch.setattr("fh.ib.stock", lambda s: s, raising=False)
    syms = [f"S{i}" for i in range(50)]
    st = data.update_bars(DeadIB(), Cfg, syms, log=lambda m: None, dead_after=6)
    assert DeadIB.calls == 6                      # gave up quickly, not 50 x 60 s
    assert got["s"] == syms and all(v == "ok" for v in st.values())
