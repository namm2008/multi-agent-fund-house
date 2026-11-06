"""Multi-fund dashboard: every adapter renders with no data and with a proposal."""
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard"))
from fh import approval, registry  # noqa: E402
from adapters import ADAPTERS  # noqa: E402


def make(spec_id, tmp_path):
    spec = registry.get(spec_id)
    c = registry.load_config(spec)
    c = replace(c, data_dir=tmp_path / spec_id / "data", state_dir=tmp_path / spec_id / "state",
                research_dir=tmp_path / spec_id / "research", logs_dir=tmp_path / spec_id / "logs",
                kill_switch=tmp_path / spec_id / "STOP")
    c.ensure_dirs()
    return ADAPTERS[spec.kind](spec, c, approval)


@pytest.mark.parametrize("fid", [f.id for f in registry.FUNDS])
def test_empty_fund_renders(fid, tmp_path):
    a = make(fid, tmp_path)
    o = a.overview()
    assert o["currency"] == a.currency and o["marks"]["nav"] == pytest.approx(a.cfg.starting_capital)
    assert o["pending_approval"] is None and o["holdings"]["rows"] == []
    assert a.market()["blocks"] and a.weeks() == [] and a.audit()["events"] == []
    s = a.summary()
    assert s["id"] == fid and s["since"] == pytest.approx(0)


def test_registry_ids_unique_and_refs_distinct():
    ids = [f.id for f in registry.FUNDS]
    refs = [f.order_ref for f in registry.FUNDS]
    assert len(set(ids)) == len(ids)
    assert not any(a != b and b.startswith(a) for a in refs for b in refs)
