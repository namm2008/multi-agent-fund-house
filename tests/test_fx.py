"""FX for the all-funds total: account rates first, then IBKR forex midpoints, then a USD cross."""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import account_snapshot as snap  # noqa: E402


class FakeIB:
    def __init__(self, mids):
        self.mids = mids

    def qualifyContracts(self, c):
        return [c] if c.pair() in self.mids else []

    def reqHistoricalData(self, c, *a, **k):
        return [SimpleNamespace(close=self.mids[c.pair()])]


def test_fx_direct_inverse_and_cross():
    ib = FakeIB({"GBPUSD": 1.30, "USDHKD": 7.80})          # no GBP.HKD / HKD.GBP pair listed
    r, s = snap.fx_rates(ib, "GBP", {"USD", "HKD", "GBP"}, {}, log=lambda *_: None)
    assert r["GBP"] == 1 and abs(r["USD"] - 1 / 1.30) < 1e-12
    assert abs(r["HKD"] - 1 / (1.30 * 7.80)) < 1e-12 and "x" in s["HKD"]
    r2, s2 = snap.fx_rates(FakeIB({"HKDGBP": 0.0985}), "GBP", {"HKD"}, {"USD": 0.77}, log=lambda *_: None)
    assert r2["HKD"] == 0.0985 and r2["USD"] == 0.77 and s2["USD"] == "IBKR account"
