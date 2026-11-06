"""Restart the fund with new capital: liquidate, archive the old period, start a clean ledger.

Phase 1 (scripts/restart_fund.py): writes state/restart_plan.json and cancels any
pending rebalance. Phase 2 (execute.py, next US open): sells every fund position;
once the ledger is flat it calls reset_fund() automatically, before the next
after-close job and committee run.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from fh.ledger import Ledger
from fh.runtime import HKT, alert, read_json, write_json

PLAN = "restart_plan.json"
# Per-period state that is archived and started fresh. account.json (the IBKR account
# itself) and data/ (prices, fundamentals) are kept.
PERIOD_STATE = ["ledger.json", "nav.csv", "shadow.json", "alerts.jsonl", "audit.jsonl",
                "approved_target.json", "pending_risk_exits.json", "pending_deleverage.json", "rank_watch.json",
                "sentinel_state.json", "live_portfolio.json", "latest.json", "last_execute.txt"]


def plan_path(cfg) -> Path:
    return cfg.state_dir / PLAN


def create_plan(cfg, new_capital: float, requested_by: str) -> dict:
    now = datetime.now(HKT).isoformat(timespec="seconds")
    plan = {"status": "liquidating", "new_capital": float(new_capital), "created_hkt": now,
            "requested_by": requested_by,
            "old_capital": cfg.starting_capital, "old_inception": cfg.inception_date}
    write_json(plan_path(cfg), plan)
    # stop any pending/retrying rebalance — nothing should be bought while liquidating
    ap = read_json(cfg.state_dir / "approved_target.json", {}) or {}
    if ap and not ap.get("executed"):
        ap["executed"] = f"cancelled {now[:10]}: fund restart"
        ap["only"] = []
        write_json(cfg.state_dir / "approved_target.json", ap)
    with open(cfg.state_dir / "audit.jsonl", "a") as f:
        f.write(json.dumps({"time_hkt": now, "event": "RESTART_REQUESTED", "folder": None,
                            "via": requested_by,
                            "note": f"Liquidate all and restart with ${new_capital:,.0f}"}) + "\n")
    alert(cfg, "INFO", f"Fund restart requested: all positions will be sold at the next US open, "
                       f"then the fund restarts with ${new_capital:,.0f}")
    return plan


def _set_config_value(text: str, name: str, value: str) -> str:
    new, n = re.subn(rf"^(\s*{name}:\s*[^=]+=\s*)([^#\n]+?)(\s*(#.*)?)$",
                     lambda m: f"{m.group(1)}{value}{m.group(3)}", text, count=1, flags=re.M)
    if n != 1:
        raise ValueError(f"could not update {name} in config.py")
    return new


def reset_fund(cfg, plan: dict, config_path: Path | None = None, today: str | None = None) -> Path:
    """Archive the finished period and start a clean ledger with plan['new_capital'].
    Refuses unless the ledger is flat (no positions)."""
    ledger = Ledger.load(cfg)
    if ledger.positions:
        raise RuntimeError(f"cannot reset: fund still holds {sorted(ledger.positions)}")
    today = today or datetime.now(HKT).date().isoformat()
    cap = float(plan["new_capital"])
    old_nav = ledger.cash

    root = cfg.state_dir.parent
    arch = root / "archive" / f"period-{plan.get('old_inception', 'start')}-to-{today}"
    (arch / "state").mkdir(parents=True, exist_ok=True)
    (arch / "research").mkdir(parents=True, exist_ok=True)
    for name in PERIOD_STATE:
        p = cfg.state_dir / name
        if p.exists():
            shutil.move(str(p), str(arch / "state" / name))
    if cfg.research_dir.exists():
        for d in sorted(cfg.research_dir.iterdir()):
            if d.is_dir():
                shutil.move(str(d), str(arch / "research" / d.name))
    (arch / "SUMMARY.json").write_text(json.dumps({
        "period": [plan.get("old_inception"), today], "starting_capital": plan.get("old_capital"),
        "final_nav_cash": round(old_nav, 2),
        "return": round(old_nav / plan["old_capital"] - 1, 4) if plan.get("old_capital") else None,
        "reason": f"restart with ${cap:,.0f} requested by {plan.get('requested_by')}"}, indent=2))

    # fund config: capital, inception date, per-order fat-finger cap (15% of capital)
    config_path = config_path or (root / "fh" / "config.py")
    txt = config_path.read_text()
    txt = _set_config_value(txt, "starting_capital", f"{cap:_.1f}")
    txt = _set_config_value(txt, "inception_date", f'"{today}"')
    txt = _set_config_value(txt, "max_order_value", f"{cap * 0.15:_.1f}")
    config_path.write_text(txt)
    cfg.starting_capital, cfg.inception_date, cfg.max_order_value = cap, today, cap * 0.15

    new = Ledger(cash=cap, hwm=cap, hwm_date=today, risk_period_start=today)
    new.save(cfg)
    plan.update(status="done", reset_hkt=datetime.now(HKT).isoformat(timespec="seconds"),
                archive=str(arch), old_final_nav=round(old_nav, 2))
    write_json(cfg.state_dir / PLAN, plan)
    with open(cfg.state_dir / "audit.jsonl", "a") as f:
        f.write(json.dumps({"time_hkt": plan["reset_hkt"], "event": "FUND_RESTARTED", "folder": None,
                            "via": "execute.py",
                            "note": f"New period from {today} with ${cap:,.0f}. Previous period "
                                    f"archived to {arch.name} (final ${old_nav:,.2f})"}) + "\n")
    alert(cfg, "INFO", f"Fund restarted with ${cap:,.0f} (inception {today}). Previous period "
                       f"archived: {arch.name}, final ${old_nav:,.2f}")
    return arch
