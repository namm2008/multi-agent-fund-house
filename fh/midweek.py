"""Mid-week review: an event-triggered short committee between the Saturday meetings.

The after-close pipeline evaluates a deterministic GATE every day and writes
state/midweek_trigger.json. The Claude scheduled task "Mid-week Review" (Tue-Fri 08:17 HKT)
reads it: no trigger -> stops in one line; trigger -> macro + risk + PM review that checks the
latest Saturday memo's pre-committed conditions (agents/PIPELINE.md section C).

Rules enforced in code for a proposal with "type": "midweek" (see midweek_violations):
  * new names only from the latest weekly target's holdings or next_steps.add_candidates;
  * at most ONE exposure-increasing mid-week proposal executed between two weekly committees
    (risk reductions are always allowed).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from fh.runtime import read_json, write_json

MARKET_MOVE = 0.03          # equal-weight S&P day move that triggers a review (either way)
VIX_LEVEL = 25.0            # VIX crossing this level (either way) triggers a review


def calendar_path(cfg) -> Path:
    return cfg.data_dir.parent / "agents" / "event_calendar.json"


def load_calendar(cfg) -> dict:
    return read_json(calendar_path(cfg), {"events": [], "valid_until": None}) or {"events": []}


def _sessions(nav: pd.DataFrame, asof: str) -> List[str]:
    """[previous session, asof] from nav history dates (falls back to business days)."""
    d = sorted(x for x in nav["date"].astype(str).unique() if x <= asof) if nav is not None and len(nav) else []
    if len(d) >= 2 and d[-1] == asof:
        return [d[-2], asof]
    return [str((pd.Timestamp(asof) - pd.tseries.offsets.BDay(1)).date()), asof]


def weekly_folders(cfg) -> List[Path]:
    """Research folders holding a WEEKLY committee proposal (not a mid-week one)."""
    out = []
    for d in sorted(p for p in cfg.research_dir.glob("20*") if p.is_dir()):
        t = read_json(d / "target_portfolio.json")
        if t and t.get("type", "weekly") != "midweek":
            out.append(d)
    return out


def earnings_dates(cfg, symbols) -> Dict[str, str]:
    """Next-earnings dates for `symbols` from the latest weekly fundamental report's JSON."""
    for d in reversed(weekly_folders(cfg)):
        p = d / "reports" / "fundamental.md"
        if not p.exists():
            continue
        txt = p.read_text()
        blocks = re.findall(r"```json\s*(\{.*?\})\s*```", txt, flags=re.S)
        for b in reversed(blocks):
            try:
                js = json.loads(b)
            except Exception:  # noqa: BLE001
                continue
            out = {r["symbol"]: r.get("next_earnings", "") for r in js.get("ratings", [])
                   if r.get("symbol") in set(symbols)}
            return {s: v for s, v in out.items() if re.match(r"\d{4}-\d{2}-\d{2}$", str(v))}
    return {}


def gate(cfg, asof: str, nav: pd.DataFrame, regime: dict, holdings: List[str],
         red_filings: List[dict], data_stale: bool) -> dict:
    """Decide whether the next morning's mid-week review should convene."""
    prev, cur = _sessions(nav, asof)
    reasons = []
    cal = load_calendar(cfg)
    for e in cal.get("events", []):
        if e.get("date") == cur:
            reasons.append(f"event: {e.get('name')} ({cur})")
    for s, d in sorted(earnings_dates(cfg, holdings).items()):
        if d in (prev, cur):
            reasons.append(f"earnings: holding {s} reported ({d})")
    old = read_json(cfg.state_dir / "midweek_trigger.json", {}) or {}
    if nav is not None and len(nav) >= 2 and "regime" in nav:
        h = nav[nav["date"].astype(str) <= cur].tail(2)
        if len(h) == 2 and h["regime"].iloc[0] != h["regime"].iloc[1]:
            reasons.append(f"regime changed: {h['regime'].iloc[0]} -> {h['regime'].iloc[1]}")
        if "ew_level" in h and h["ew_level"].notna().all() and len(h) == 2:
            mv = h["ew_level"].iloc[1] / h["ew_level"].iloc[0] - 1
            if abs(mv) >= MARKET_MOVE:
                reasons.append(f"market move: equal-weight S&P {mv:+.1%} on {cur}")
    vix, pvix = regime.get("vix"), old.get("vix")
    if vix is not None and pvix is not None and (vix >= VIX_LEVEL) != (pvix >= VIX_LEVEL):
        reasons.append(f"VIX crossed {VIX_LEVEL:g}: {pvix:.1f} -> {vix:.1f}")
    for f in red_filings:
        reasons.append(f"red filing: {f['symbol']} {f['form']} {f['what']} ({f['filed']})")
    warn = None
    vu = cal.get("valid_until")
    if not vu or vu < cur:
        warn = "event calendar expired: update agents/event_calendar.json"
    elif (pd.Timestamp(vu) - pd.Timestamp(cur)).days < 21:
        warn = f"event calendar ends {vu}: add the next dates to agents/event_calendar.json"
    upcoming = [e for e in cal.get("events", []) if cur < e.get("date", "") <=
                str((pd.Timestamp(cur) + pd.Timedelta(days=10)).date())]
    rec = {"asof": cur, "triggered": bool(reasons) and not data_stale, "reasons": reasons,
           "data_stale": data_stale, "vix": vix, "calendar_warning": warn,
           "upcoming_events": upcoming}
    write_json(cfg.state_dir / "midweek_trigger.json", rec)
    return rec


def gate_markdown(g: dict) -> str:
    s = ("\n## Mid-week review gate\n\n" +
         (f"**TRIGGERED** for the next morning's review: " + "; ".join(g["reasons"]) + "."
          if g["triggered"] else "Not triggered." + (" (data stale)" if g["data_stale"] else "")))
    if g.get("upcoming_events"):
        s += "\n\nNext 10 days: " + "; ".join(f"{e['date']} {e['name']}" for e in g["upcoming_events"]) + "."
    if g.get("calendar_warning"):
        s += f"\n\n> {g['calendar_warning']}"
    return s + "\n"


# --------------------------------------------------------------------------- approval rules
def _executed_increase_since(cfg, since_folder: str) -> List[str]:
    out = []
    for d in sorted(p for p in cfg.research_dir.glob("20*") if p.is_dir() and p.name > since_folder):
        t, a = read_json(d / "target_portfolio.json"), read_json(d / "approval.json")
        if t and t.get("type") == "midweek" and a and a.get("executed") and \
                any(o.get("side") == "BUY" for o in a.get("orders_preview", [])):
            out.append(d.name)
    return out


def midweek_violations(cfg, folder: Path, prop: dict, current: Dict[str, float],
                       target: Dict[str, float]) -> List[str]:
    if prop.get("type") != "midweek":
        return []
    ref = [d for d in weekly_folders(cfg) if d.name < folder.name]
    if not ref:
        return ["mid-week proposal without a preceding weekly committee proposal"]
    wk = read_json(ref[-1] / "target_portfolio.json", {}) or {}
    allowed = {h.get("symbol") for h in wk.get("holdings", [])} | \
              set((wk.get("next_steps") or {}).get("add_candidates", [])) | set(current)
    v = [f"mid-week: {s} is not a holding, not in the {ref[-1].name} target and not one of its "
         f"add_candidates" for s in target if s not in allowed]
    increases = any(w > current.get(s, 0) + 0.02 for s, w in target.items())
    if increases:
        done = _executed_increase_since(cfg, ref[-1].name)
        done = [d for d in done if d != folder.name]
        if done:
            v.append(f"mid-week: an exposure-increasing mid-week proposal already executed "
                     f"since the {ref[-1].name} committee ({', '.join(done)}); only reductions "
                     f"are allowed until the next weekly committee")
    return v
