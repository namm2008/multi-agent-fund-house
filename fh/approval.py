"""Proposal evaluation and the approval record — shared by the CLI
(scripts/approve.py) and the dashboard, so both leave the same audit trail."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from fh.ledger import Ledger
from fh.orders import check_fat_finger, orders_to_dicts, plan_orders
from fh.risk import current_caps, validate_target
from fh.runtime import HKT, execution_window, now_et, read_json, write_json


def proposal_folders(cfg) -> list:
    if not cfg.research_dir.exists():
        return []
    return sorted(d for d in cfg.research_dir.iterdir()
                  if d.is_dir() and (d / "target_portfolio.json").exists())


def latest_proposal(cfg) -> Optional[Path]:
    f = proposal_folders(cfg)
    return f[-1] if f else None


def evaluate(cfg, folder: Path) -> dict:
    """Re-run the code risk check on a proposal against the CURRENT ledger."""
    prop = read_json(folder / "target_portfolio.json", {})
    pack = read_json(folder / "pack.json", {})
    screen = pd.read_csv(folder / "screen_full.csv", index_col=0)
    ledger = Ledger.load(cfg)
    prices = {c["symbol"]: c["price"] for c in pack.get("candidates", [])}
    prices.update({h["symbol"]: h["price"] for h in pack.get("fund", {}).get("holdings", [])})
    for s in list(ledger.positions) + [h.get("symbol") for h in prop.get("holdings", [])]:
        if s and s not in prices and s in screen.index:
            prices[s] = float(screen.loc[s, "price"])
    caps = current_caps(cfg, ledger, prices, pack.get("regime", {"exposure_cap": 0}))
    frozen = caps.buys_frozen
    if frozen:
        # A sentinel buy-freeze is temporary: approve the intended target and let execution
        # hold the buys back until the freeze ends (sells still go at the next run).
        from dataclasses import replace as _replace
        caps = _replace(caps, buys_frozen=False)
    v = validate_target(prop, screen, ledger, prices, caps, cfg)
    if frozen:
        v.warnings.append(f"Buys are frozen by the crash sentinel until {ledger.buys_frozen_until}: "
                          "if approved, sells go at the next run and the buys wait until the freeze ends")
    from fh.midweek import midweek_violations
    mv = midweek_violations(cfg, folder, prop, ledger.weights(prices), v.target)
    if mv:
        v.violations.extend(mv)
        v.ok = False
    orders = plan_orders(v.target, ledger, prices, cfg)
    ff = check_fat_finger(orders, cfg)
    current = {s: round(w, 4) for s, w in ledger.weights(prices).items()}
    approval = read_json(folder / "approval.json")
    return {
        "folder": folder.name, "proposal": prop,
        "caps": {"gross_cap": caps.gross_cap, "regime_cap": caps.regime_cap,
                 "ladder_cap": caps.ladder_cap, "halted": caps.halted,
                 "buys_frozen": frozen, "buys_frozen_until": ledger.buys_frozen_until or ""},
        "violations": v.violations, "changes": v.changes, "warnings": v.warnings,
        "fat_finger": ff, "final_target": v.target, "current_weights": current,
        "sectors": {s: screen.loc[s, "sector"] for s in set(v.target) | set(current)
                    if s in screen.index and "sector" in screen},
        "orders": orders_to_dicts(orders),
        "approvable": bool(v.ok and not ff and prop.get("status") == "proposed"
                           and not (approval or {}).get("executed")),
        "approval": approval,
    }


def record_decision(cfg, folder: Path, approve: bool, note: str = "",
                    via: str = "cli") -> dict:
    ev = evaluate(cfg, folder)
    if approve and not ev["approvable"]:
        raise ValueError("Proposal is not approvable: " + "; ".join(
            ev["violations"] + ev["fat_finger"] or ["status is not 'proposed' or already executed"]))
    prev = read_json(folder / "approval.json") or {}
    if prev.get("executed"):
        raise ValueError("Already executed on " + str(prev["executed"]))
    rec = {"folder": folder.name, "target": ev["final_target"], "decision": approve,
           "orders_preview": ev["orders"], "changes": ev["changes"],
           "warnings": ev["warnings"], "executed": False, "note": note, "via": via,
           "decided_at_hkt": datetime.now(HKT).isoformat(timespec="seconds"),
           "history": prev.get("history", []) + ([{k: prev.get(k) for k in
                                                   ("decision", "decided_at_hkt", "via", "note")}]
                                                 if prev else [])}
    write_json(folder / "approval.json", rec)
    write_json(cfg.state_dir / "approved_target.json", rec if approve else {})
    with open(cfg.state_dir / "audit.jsonl", "a") as f:
        f.write(json.dumps({"time_hkt": rec["decided_at_hkt"], "event":
                            "APPROVED" if approve else "REJECTED", "folder": folder.name,
                            "via": via, "note": note,
                            "target": ev["final_target"]}) + "\n")
    return rec


def needs_execution(approved: dict, today: str) -> bool:
    """An approved target that has not been executed and was not already attempted today."""
    if not approved or not approved.get("decision") or approved.get("executed"):
        return False
    return all(a.get("date") != today for a in approved.get("attempts", []))


def trigger_execution(cfg, folder: Path, popen=subprocess.Popen) -> dict:
    """Called right after an approval. If the US market is open (09:40-15:40 New York
    time) start scripts/execute.py --on-approval in the background so the orders go now
    instead of at the next scheduled run (21:40 HKT). Outside those hours, or if
    `cfg.execute_on_approval` is off, nothing starts and the scheduled run trades.
    The result is stored in approval.json["trigger"] and the audit trail."""
    ok, why = execution_window()
    if not getattr(cfg, "execute_on_approval", True):
        ok, why = False, "instant execution is switched off (config: execute_on_approval)"
    now = datetime.now(HKT).isoformat(timespec="seconds")
    if ok:
        root = Path(__file__).resolve().parents[1]
        logs = cfg.logs_dir
        logs.mkdir(parents=True, exist_ok=True)
        out = open(logs / "launchd_com.fundhouse.execute.out", "a")
        err = open(logs / "launchd_com.fundhouse.execute.err", "a")
        try:
            popen([sys.executable, str(root / "scripts" / "execute.py"), "--on-approval"],
                  cwd=str(root), stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                  start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1"})
            res = {"started": True, "at_hkt": now,
                   "message": "Orders are being sent now (US market open). "
                              "Fills normally appear within a few minutes."}
        except Exception as e:  # noqa: BLE001
            res = {"started": False, "at_hkt": now,
                   "message": f"Could not start the order run ({type(e).__name__}: {e}); "
                              "the scheduled run at 21:40 HKT will trade instead."}
        finally:
            out.close()
            err.close()
    else:
        res = {"started": False, "at_hkt": now,
               "message": f"Orders will go at the next scheduled run (21:40 HKT on the next "
                          f"US trading day): {why}."}
    ap = read_json(folder / "approval.json", {}) or {}
    if ap:
        ap["trigger"] = res
        write_json(folder / "approval.json", ap)
    with open(cfg.state_dir / "audit.jsonl", "a") as f:
        f.write(json.dumps({"time_hkt": now, "event": "EXECUTION_TRIGGERED" if res["started"]
                            else "EXECUTION_QUEUED", "folder": folder.name, "via": "approval",
                            "note": res["message"]}) + "\n")
    return res
