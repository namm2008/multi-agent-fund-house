"""Shared helpers for scripts: clock, logging, alerts, file locations."""
from __future__ import annotations

import json
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HKT = ZoneInfo("Asia/Hong_Kong")


def now_et() -> datetime:
    return datetime.now(ET)


def market_open_now(t: datetime | None = None) -> bool:
    t = t or now_et()
    return t.weekday() < 5 and dtime(9, 30) <= t.time() < dtime(16, 0)


def execution_window(t: datetime | None = None) -> tuple:
    """Can an order run start now? (True, "") or (False, reason).
    Used for runs triggered by an approval; the scheduled 21:40/22:40 HKT runs keep
    their own simpler rule."""
    t = t or now_et()
    if not market_open_now(t):
        return False, "the US market is closed"
    if t.time() < dtime(9, 40):
        return False, "the opening auction is still settling (before 09:40 New York time)"
    if t.time() >= dtime(15, 40):
        return False, "it is too close to the US close (after 15:40 New York time)"
    return True, ""


def last_session_date(t: datetime | None = None) -> str:
    """Most recent US session date (ET) whose close has passed."""
    import pandas as pd
    t = t or now_et()
    d = pd.Timestamp(t.date())
    if t.time() < dtime(16, 15):
        d -= pd.Timedelta(days=1)
    while d.weekday() >= 5:
        d -= pd.Timedelta(days=1)
    return str(d.date())


class Log:
    def __init__(self, cfg, name: str):
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        self.path = cfg.logs_dir / f"{name}.log"

    def __call__(self, msg: str) -> None:
        line = f"{datetime.now(HKT):%Y-%m-%d %H:%M:%S} HKT | {msg}"
        print(line, flush=True)
        with open(self.path, "a") as f:
            f.write(line + "\n")


def alert(cfg, level: str, msg: str, **extra) -> None:
    """Append to state/alerts.jsonl — read by the daily risk brief and status."""
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    rec = {"time_hkt": datetime.now(HKT).isoformat(timespec="seconds"),
           "level": level, "msg": msg, **extra}
    with open(cfg.state_dir / "alerts.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"[ALERT {level}] {msg}", file=sys.stderr, flush=True)
    _mac_notify(level, msg)


def _mac_notify(level: str, msg: str) -> None:
    """Desktop notification on macOS; silently ignored elsewhere."""
    import platform
    import subprocess
    if platform.system() != "Darwin":
        return
    safe = msg.replace('"', "'")[:200]
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{safe}" with title "Fund House {level}"'],
                       timeout=5, check=False)
    except Exception:  # noqa: BLE001
        pass


def read_json(p: Path, default=None):
    return json.loads(p.read_text()) if p.exists() else default


def write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    tmp.replace(p)
