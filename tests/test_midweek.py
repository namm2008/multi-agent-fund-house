import json
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fh.config import Config  # noqa: E402
from fh import midweek  # noqa: E402


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state",
                research_dir=tmp_path / "research", logs_dir=tmp_path / "logs")
    c.ensure_dirs()
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "event_calendar.json").write_text(json.dumps({
        "valid_until": "2026-12-31",
        "events": [{"date": "2026-10-28", "name": "FOMC decision"},
                   {"date": "2026-11-06", "name": "Jobs report (Oct)"}]}))
    wk = c.research_dir / "2026-10-23"
    (wk / "reports").mkdir(parents=True)
    (wk / "target_portfolio.json").write_text(json.dumps({
        "type": "weekly", "status": "proposed",
        "holdings": [{"symbol": "AAA", "weight": 0.05}, {"symbol": "BBB", "weight": 0.05}],
        "next_steps": {"add_candidates": ["CCC"]}}))
    (wk / "reports" / "fundamental.md").write_text(
        'text\n```json\n{"role":"fundamental","ratings":[{"symbol":"AAA","next_earnings":"2026-10-29"},'
        '{"symbol":"BBB","next_earnings":"unverified"}]}\n```\n')
    return c


def nav(rows):
    return pd.DataFrame(rows, columns=["date", "regime", "ew_level"])


def test_gate_quiet_event_earnings_regime_move_vix(cfg):
    base = [("2026-10-26", "risk_on", 1.00), ("2026-10-27", "risk_on", 1.01)]
    g = midweek.gate(cfg, "2026-10-27", nav(base), {"vix": 18.0}, ["AAA", "BBB"], [], False)
    assert g["triggered"] is False and g["upcoming_events"][0]["name"] == "FOMC decision"
    g = midweek.gate(cfg, "2026-10-28", nav(base + [("2026-10-28", "risk_on", 1.012)]),
                     {"vix": 19.0}, ["AAA"], [], False)
    assert g["triggered"] and any("FOMC" in r for r in g["reasons"])
    g = midweek.gate(cfg, "2026-10-29", nav(base + [("2026-10-28", "risk_on", 1.012),
                                                    ("2026-10-29", "neutral", 0.975)]),
                     {"vix": 26.0}, ["AAA"], [], False)
    txt = " | ".join(g["reasons"])
    assert "earnings: holding AAA" in txt and "regime changed" in txt
    assert "market move" in txt and "VIX crossed 25" in txt
    g = midweek.gate(cfg, "2026-10-30", nav(base), {"vix": 26.0}, ["AAA"],
                     [{"symbol": "AAA", "form": "8-K", "what": "4.02", "filed": "2026-10-30"}], True)
    assert g["triggered"] is False and g["data_stale"]          # stale data never convenes
    assert "TRIGGERED" not in midweek.gate_markdown(g)


def test_calendar_warning(cfg):
    g = midweek.gate(cfg, "2026-12-20", nav([("2026-12-18", "risk_on", 1), ("2026-12-20", "risk_on", 1)]),
                     {}, [], [], False)
    assert "event calendar ends 2026-12-31" in g["calendar_warning"]


def test_midweek_violations(cfg):
    f = cfg.research_dir / "2026-10-28"
    f.mkdir()
    prop = {"type": "midweek", "holdings": []}
    cur = {"AAA": 0.05, "BBB": 0.05}
    assert midweek.midweek_violations(cfg, f, prop, cur, {"AAA": 0.07, "BBB": 0.05, "CCC": 0.05}) == []
    v = midweek.midweek_violations(cfg, f, prop, cur, {"AAA": 0.05, "ZZZ": 0.05})
    assert len(v) == 1 and "ZZZ" in v[0]
    assert midweek.midweek_violations(cfg, f, {"type": "weekly"}, cur, {"ZZZ": 0.1}) == []
    # an executed exposure-increasing mid-week proposal blocks a second increase, not a cut
    e = cfg.research_dir / "2026-10-27"
    e.mkdir()
    (e / "target_portfolio.json").write_text(json.dumps({"type": "midweek"}))
    (e / "approval.json").write_text(json.dumps({"executed": "2026-10-28",
                                                 "orders_preview": [{"side": "BUY"}]}))
    v = midweek.midweek_violations(cfg, f, prop, cur, {"AAA": 0.08, "BBB": 0.05})
    assert len(v) == 1 and "already executed" in v[0]
    assert midweek.midweek_violations(cfg, f, prop, cur, {"AAA": 0.03, "BBB": 0.05}) == []
