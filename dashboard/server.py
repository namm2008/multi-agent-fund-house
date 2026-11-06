"""Fund House dashboard — local web app for every fund in fh/registry.py (standard library only).

  python3 dashboard/server.py            # then open http://127.0.0.1:8765
  python3 dashboard/server.py --demo     # sample data, to see what it looks like

Cover page with all funds, then per fund: overview, approval, committee, market, audit.
Each fund's files are read through its adapter (dashboard/adapters.py). Approvals write the
same record as the fund's CLI (scripts/approve.py). Binds to 127.0.0.1 only.
"""
from __future__ import annotations

import argparse
import json
import math
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import importlib  # noqa: E402

from fh.config import CONFIG  # noqa: E402
from fh import registry  # noqa: E402
from fh.runtime import HKT, read_json  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from adapters import ADAPTERS  # noqa: E402

TOKEN = secrets.token_urlsafe(24)       # blocks cross-site POSTs to localhost
cfg = CONFIG


# --------------------------------------------------------------------------- #
def clean(o):
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


# --------------------------------------------------------------------------- #
# Funds: one adapter per fund in fh/registry.py (see dashboard/adapters.py)
FUNDS: dict = {}


def load_funds(demo_cfg=None):
    FUNDS.clear()
    for spec in registry.FUNDS:
        fcfg = demo_cfg if (demo_cfg is not None and spec.id == "us") else registry.load_config(spec)
        appr_mod = importlib.import_module("fh.approval")
        FUNDS[spec.id] = ADAPTERS[spec.kind](spec, fcfg, appr_mod)


def fund(fid: str):
    if fid not in FUNDS:
        raise FileNotFoundError(f"fund {fid}")
    return FUNDS[fid]


def api_funds() -> dict:
    """Cover page: one summary per fund, the shared IBKR account and a combined total."""
    cards = []
    for a in FUNDS.values():
        try:
            cards.append(a.summary())
        except Exception as e:  # noqa: BLE001  one broken fund never blanks the cover
            cards.append({"id": a.id, "label": a.label, "currency": a.currency, "error": f"{type(e).__name__}: {e}",
                          "marks": {}, "capital": a.cfg.starting_capital, "alerts": [], "events": []})
    acct = read_json(cfg.state_dir / "account.json", {}) or {}
    fx = dict(acct.get("fx_to_base") or {})
    base = acct.get("currency")
    total, fx_missing = None, []
    if base and base != "BASE":
        fx_missing = sorted({c["currency"] for c in cards} - set(fx) - {base})
        fx.setdefault(base, 1.0)
        if all(c["currency"] in fx and c["marks"].get("nav") is not None for c in cards):
            total = {"currency": base, "nav": sum(c["marks"]["nav"] * fx[c["currency"]] for c in cards),
                     "capital": sum(c["capital"] * fx[c["currency"]] for c in cards),
                     "fx": {c["currency"]: fx[c["currency"]] for c in cards}}
    alerts = sorted(({**a, "fund": c["id"], "fund_label": c["label"]} for c in cards for a in c["alerts"]),
                    key=lambda a: str(a.get("time_hkt") or ""), reverse=True)[:8]
    events = sorted(({**e, "fund": c["id"], "fund_label": c["label"]} for c in cards for e in c["events"]),
                    key=lambda e: e["date"])[:12]
    return {"now_hkt": datetime.now(HKT).isoformat(timespec="seconds"), "funds": cards,
            "account": {k: acct.get(k) for k in ("account", "time_hkt", "currency", "net_liquidation", "cash",
                                                  "unrealized_pnl", "connected")},
            "total": total, "fx_missing": fx_missing, "fx_source": acct.get("fx_source") or {},
            "alerts": alerts, "events": events}


def safe_path(rel: str) -> Path:
    p = (ROOT / rel).resolve()
    allowed = [d for a in FUNDS.values() for d in a.allowed_dirs()]
    if not any(str(p).startswith(str(a) + "/") or p == a for a in allowed) or not p.is_file():
        raise FileNotFoundError(rel)
    return p


# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "FundHouse/1"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(clean(obj), default=str).encode(), "application/json")

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                html = (Path(__file__).parent / "index.html").read_text()
                html = html.replace("__FH_TOKEN__", TOKEN)
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if u.path in ("/how", "/how.html", "/how-it-works") or u.path.startswith("/how/"):
                fid = u.path.split("/")[2] if u.path.startswith("/how/") else "us"
                page = fund(fid).HOW_PAGE
                if not page:
                    raise FileNotFoundError(f"no 'How it works' page for {fid}")
                html = (Path(__file__).parent / page).read_text()
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if u.path == "/api/mechanics":
                return self._json(fund(parse_qs(u.query).get("fund", ["us"])[0]).mechanics())
            if u.path.startswith("/static/"):
                name = u.path.rsplit("/", 1)[1]
                f = Path(__file__).parent / "static" / name
                if f.is_file() and name.endswith(".js"):
                    return self._send(200, f.read_bytes(), "application/javascript")
                return self._json({"error": "not found"}, 404)
            if u.path == "/api/funds":
                return self._json(api_funds())
            if u.path == "/api/file":
                p = safe_path(parse_qs(u.query).get("path", [""])[0])
                ctype = "application/json" if p.suffix == ".json" else "text/plain; charset=utf-8"
                return self._send(200, p.read_bytes(), ctype)
            parts = u.path.strip("/").split("/")              # api/f/<fund>/<what>
            if len(parts) == 4 and parts[:2] == ["api", "f"]:
                a, what = fund(parts[2]), parts[3]
                if what == "overview":
                    return self._json({**a.overview(), "exec_note": a.EXEC_NOTE, "flow": a.FLOW,
                                       "strategy": a.STRATEGY})
                if what == "market":
                    return self._json(a.market())
                if what == "weeks":
                    return self._json(a.weeks())
                if what == "week":
                    return self._json(a.week(parse_qs(u.query).get("d", [""])[0]))
                if what == "audit":
                    return self._json(a.audit())
            self._json({"error": "not found"}, 404)
        except FileNotFoundError as e:
            self._json({"error": f"not found: {e}"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        if self.headers.get("X-FH-Token") != TOKEN:
            return self._json({"error": "bad token — reload the page"}, 403)
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)
        try:
            parts = u.path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "f"] and parts[3] == "decision":
                decision = body.get("decision")
                if decision == "approve" and body.get("confirm") != "APPROVE":
                    return self._json({"error": "type APPROVE to confirm"}, 400)
                if decision not in ("approve", "reject"):
                    return self._json({"error": "decision must be approve|reject"}, 400)
                return self._json(fund(parts[2]).decide(str(body.get("folder", "")), decision == "approve",
                                                        str(body.get("note", ""))[:1000]))
            if u.path in ("/api/refresh-account", "/api/refresh-portfolio"):
                ok, msg = refresh_account()
                return self._json({"ok": ok, "message": msg})
            self._json({"error": "not found"}, 404)
        except FileNotFoundError as e:
            self._json({"error": f"not found: {e}"}, 404)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


# --------------------------------------------------------------------------- #
REFRESH_LOCK = threading.Lock()


def refresh_account():
    """Run the read-only IBKR snapshot (account + fund live prices). Serialised so the
    manual button and the 2-minute background refresh never use the same client id at once."""
    from fh.ib import port_open
    if not port_open(cfg.ib_host, cfg.ib_port, 0.5):
        return False, "IB Gateway is not reachable — showing the last snapshot and last close prices."
    with REFRESH_LOCK:
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "account_snapshot.py")],
                           capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout)[-400:] or "snapshot failed"
    return True, "Portfolio refreshed from IBKR."


def code_watcher():
    """When run by launchd (parent pid 1), exit if the dashboard code changes on disk so
    launchd restarts it with the new version. Manual runs are left alone."""
    import os
    files = [Path(__file__), Path(__file__).parent / "adapters.py", *(ROOT / "fh").glob("*.py")]
    stamp = {f: f.stat().st_mtime for f in files if f.exists()}
    while True:
        time.sleep(10)
        if any(f.exists() and f.stat().st_mtime != m for f, m in stamp.items()):
            print("code changed on disk — exiting so launchd restarts the dashboard", flush=True)
            os._exit(3)


def account_loop(every: int):
    while True:
        try:
            refresh_account()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(every)


def main():
    global cfg
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--demo", action="store_true", help="serve generated sample data")
    ap.add_argument("--no-account-refresh", action="store_true")
    a = ap.parse_args()
    if a.demo:
        from demo_data import build_demo
        demo_root = ROOT / "dashboard" / "_demo"
        cfg = replace(CONFIG, data_dir=demo_root / "data", state_dir=demo_root / "state",
                      research_dir=demo_root / "research", logs_dir=demo_root / "logs",
                      kill_switch=demo_root / "STOP_TRADING", inception_date="2026-07-06")
        build_demo(cfg)
        load_funds(demo_cfg=cfg)
        print("DEMO DATA")
    else:
        load_funds()
    if not a.demo and not a.no_account_refresh:
        threading.Thread(target=account_loop, args=(120,), daemon=True).start()
    import os
    if os.getppid() == 1:
        threading.Thread(target=code_watcher, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"Fund House dashboard: http://127.0.0.1:{a.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    main()
