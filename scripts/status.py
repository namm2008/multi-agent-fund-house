"""One-screen status: NAV, drawdown, caps, holdings, recent alerts, pending actions."""
import _boot  # noqa: F401
import json
from fh.config import CONFIG as cfg
from fh.runtime import read_json

latest = read_json(cfg.state_dir / "latest.json", {})
print("latest:", latest)
led = read_json(cfg.state_dir / "ledger.json", {})
print("halted:", led.get("halted"), led.get("halt_reason"), "| buys frozen until:",
      led.get("buys_frozen_until"))
print("positions:", {k: v["qty"] for k, v in led.get("positions", {}).items()})
print("sentinel:", (read_json(cfg.state_dir / "sentinel_state.json", {}) or {}).get("last"))
print("pending exits:", read_json(cfg.state_dir / "pending_risk_exits.json", {}))
print("approved target:", {k: v for k, v in (read_json(cfg.state_dir / "approved_target.json", {}) or {}).items() if k in ("folder", "executed")})
p = cfg.state_dir / "alerts.jsonl"
if p.exists():
    print("\nlast alerts:")
    for line in p.read_text().splitlines()[-10:]:
        a = json.loads(line); print(f"  {a['time_hkt']} {a['level']:8s} {a['msg']}")
