"""Human approval of the committee's proposal (terminal version; the dashboard
does the same thing with buttons).

  python scripts/approve.py            # latest proposal
  python scripts/approve.py 2026-10-09 # a specific week
"""
import _boot  # noqa: F401
import sys
from pathlib import Path

from fh.approval import evaluate, latest_proposal, record_decision, trigger_execution
from fh.config import CONFIG as cfg


def review(folder: Path, interactive=True):
    ev = evaluate(cfg, folder)
    prop = ev["proposal"]
    c = ev["caps"]
    print(f"\n=== Proposal {folder.name}: {prop.get('decision', '')} ===")
    print(prop.get("summary", "")[:1500])
    print(f"\nRisk check — gross cap {c['gross_cap']:.0%} (regime {c['regime_cap']:.0%}, "
          f"ladder {c['ladder_cap']:.0%})")
    for x in ev["violations"]: print("  VIOLATION:", x)
    for x in ev["changes"]: print("  changed:  ", x)
    for x in ev["warnings"]: print("  warning:  ", x)
    print("\nFinal target weights:")
    for s, w in sorted(ev["final_target"].items(), key=lambda kv: -kv[1]):
        print(f"  {s:6s} {w:6.1%}")
    print(f"  cash   {1 - sum(ev['final_target'].values()):6.1%}")
    print("\nOrder preview (at last close; re-sized at execution):")
    for o in ev["orders"]:
        print(f"  {o['side']:4s} {o['qty']:>8g} {o['symbol']:6s} ≈ ${o['value']:>9,.2f}  ({o['reason']})")
    for x in ev["fat_finger"]: print("  FAT-FINGER:", x)
    if not interactive:
        return ev
    if not ev["approvable"]:
        print("\nNot approvable (see issues above, or already decided/executed)."); return ev
    ans = input("\nType APPROVE to authorise, REJECT to reject, anything else to skip: ").strip()
    if ans in ("APPROVE", "REJECT"):
        note = input("Optional note for the audit trail: ").strip()
        record_decision(cfg, folder, ans == "APPROVE", note, via="cli")
        if ans == "APPROVE":
            print("Approved. " + trigger_execution(cfg, folder)["message"])
        else:
            print("Rejected. Nothing will trade.")
    return ev


if __name__ == "__main__":
    f = cfg.research_dir / sys.argv[1] if len(sys.argv) > 1 else latest_proposal(cfg)
    if not f:
        sys.exit("No proposal found (research/<date>/target_portfolio.json).")
    review(f)
