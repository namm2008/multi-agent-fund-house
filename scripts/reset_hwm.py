"""Start a new risk period after a halt: un-halts and resets the high-water mark
to today's NAV. This is a deliberate human decision, logged in the ledger."""
import _boot  # noqa: F401
from datetime import date
from fh.config import CONFIG as cfg
from fh.ledger import Ledger
from fh.runtime import read_json, alert

ledger = Ledger.load(cfg)
latest = read_json(cfg.state_dir / "latest.json", {})
nav = latest.get("nav", ledger.cash)
print(f"Halted: {ledger.halted} ({ledger.halt_reason}); HWM {ledger.hwm:.2f}; NAV {nav}")
if cfg.kill_switch.exists():
    print("Remove the STOP_TRADING file first."); raise SystemExit(1)
if input("Type RESET to start a new risk period at current NAV: ").strip() == "RESET":
    ledger.halted, ledger.halt_reason = False, ""
    ledger.hwm, ledger.hwm_date = float(nav), date.today().isoformat()
    ledger.risk_period_start = date.today().isoformat()
    ledger.save(cfg)
    alert(cfg, "INFO", f"New risk period started by human at NAV {nav}")
