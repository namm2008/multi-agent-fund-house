"""Fund adapters for the multi-fund dashboard.

Every fund is shown with the same pages (overview, approval, committee, market, audit).
The browser only knows a generic contract; each adapter turns its fund's files into it:

  overview()  KPIs, holdings (+ column spec), events calendar, health, banners, limits
  market()    blocks: key-value cards and sortable tables (column spec)
  weeks()/week(id)  committee folders and agent reports
  evaluate()/decide()  proposal check and the approval record
  audit()     decisions, alerts, fills, proposals
  summary()   one card on the cover page

A new fund with the same strategy reuses an adapter (new FundSpec in fh/registry.py);
a new strategy subclasses FundAdapter and overrides the hooks it needs.
"""
from __future__ import annotations

import csv
import html
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from fh.runtime import HKT, read_json

ROOT = Path(__file__).resolve().parent.parent
E = html.escape


def read_text(p: Path, limit: int = 400_000):
    return p.read_text(errors="replace")[:limit] if p.exists() else None


def jsonl_tail(p: Path, n: int = 200) -> list:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(errors="replace").splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def last_json_block(md):
    """Agents end each report with a ```json block — parse the last one."""
    if not md or "```json" not in md:
        return None
    blk = md.rsplit("```json", 1)[1].split("```", 1)[0]
    try:
        return json.loads(blk)
    except json.JSONDecodeError:
        return None


def file_age_hours(p: Path):
    return round((time.time() - p.stat().st_mtime) / 3600, 1) if p.exists() else None


def safe(fn, *a, default=None):
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        return {**(default or {}), "error": f"{type(e).__name__}: {e}"}


def port_open(host, port, timeout=0.5) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# =========================================================================== #
class FundAdapter:
    ROLES: list = []                       # [(report file key, title)] in pipeline order
    FLOW = ""                              # committee pipeline, one line
    STRATEGY = ""
    EXEC_NOTE = ""
    MARKET_NAME = "market"
    HOW_PAGE = ""                          # dashboard/<file> served at /how/<fund id>

    def __init__(self, spec, cfg, appr):
        self.spec, self.cfg, self.appr = spec, cfg, appr
        self.id, self.label, self.currency, self.kind = spec.id, spec.label, spec.currency, spec.kind

    # ------------------------------------------------------------ files
    def rel(self, p: Path) -> str:
        try:
            return str(Path(p).resolve().relative_to(ROOT.resolve()))
        except ValueError:
            return str(p)

    def ledger(self) -> dict:
        return read_json(self.cfg.state_dir / "ledger.json", {}) or {}

    def latest(self) -> dict:
        return read_json(self.cfg.state_dir / "latest.json", {}) or {}

    def pack(self, latest=None) -> dict:
        latest = latest if latest is not None else self.latest()
        if not latest.get("asof"):
            return {}
        return read_json(self.cfg.research_dir / latest["asof"] / "pack.json", {}) or {}

    def live(self) -> dict:
        return read_json(self.cfg.state_dir / "live_portfolio.json", {}) or {}

    def nav_history(self) -> list:
        p = self.cfg.state_dir / "nav.csv"
        if not p.exists():
            return []
        rows = list(csv.DictReader(p.open()))
        for r in rows:
            for k, v in list(r.items()):
                if k not in ("date", "regime"):
                    try:
                        r[k] = float(v) if v not in ("", None) else None
                    except ValueError:
                        pass
        return rows

    def alerts(self, n=50) -> list:
        return jsonl_tail(self.cfg.state_dir / "alerts.jsonl", n)[::-1]

    def allowed_dirs(self) -> list:
        return [self.cfg.research_dir.resolve(), self.cfg.state_dir.resolve(), self.cfg.logs_dir.resolve()]

    # ------------------------------------------------------------ proposals
    def folders(self) -> list:
        """Research folders: <date>/ and nested proposal folders (e.g. <date>/trial)."""
        rd = self.cfg.research_dir
        if not rd.exists():
            return []
        out = []
        for d in rd.iterdir():
            if not d.is_dir():
                continue
            out.append(d)
            out += [s for s in d.iterdir() if s.is_dir() and (s / "target_portfolio.json").exists()]
        return sorted(out, key=lambda d: str(d.relative_to(rd)), reverse=True)

    def folder(self, fid: str) -> Path:
        d = (self.cfg.research_dir / fid).resolve()
        if ".." in fid or not str(d).startswith(str(self.cfg.research_dir.resolve())) or not d.is_dir():
            raise FileNotFoundError(fid)
        return d

    def fid(self, d: Path) -> str:
        return str(Path(d).relative_to(self.cfg.research_dir))

    def pending(self):
        for d in self.folders():
            prop = read_json(d / "target_portfolio.json", {}) or {}
            if prop.get("status") == "proposed" and not read_json(d / "approval.json"):
                return {"folder": self.fid(d), "decision": prop.get("decision"),
                        "summary": (prop.get("summary") or "")[:400]}
            if prop:                                    # only the newest proposal can be pending
                return None
        return None

    def evaluate(self, d: Path) -> dict:
        return self.appr.evaluate(self.cfg, d)

    def decide(self, fid: str, approve: bool, note: str) -> dict:
        d = self.folder(fid)
        if not (d / "target_portfolio.json").exists():
            raise FileNotFoundError(fid)
        rec = self.appr.record_decision(self.cfg, d, approve, note, via="dashboard")
        trig = self.after_approve(d) if approve else None
        return {"ok": True, "approval": rec, "trigger": trig}

    def after_approve(self, d: Path):
        return None

    def weeks(self) -> list:
        out = []
        for d in self.folders():
            prop = read_json(d / "target_portfolio.json", {}) or {}
            ap = read_json(d / "approval.json", {}) or {}
            out.append({
                "date": self.fid(d), "has_pack": (d / "pack.json").exists(),
                "n_reports": len(list((d / "reports").glob("*.md"))) if (d / "reports").exists() else 0,
                "has_memo": (d / "committee_memo.md").exists(),
                "has_brief": any((d / f).exists() for f in ("daily_brief.md", "midweek_review.md", "event_review.md")),
                "blocked": (d / "committee_blocked.md").exists(),
                "decision": prop.get("decision"), "status": prop.get("status"), "type": prop.get("type"),
                "approval": None if not ap else ("approved" if ap.get("decision") else "rejected"),
                "executed": ap.get("executed") or None,
            })
        return out

    def week(self, fid: str) -> dict:
        d = self.folder(fid)
        reports = {}
        for key, title in self.ROLES:
            m = read_text(d / "reports" / f"{key}.md")
            if m is not None:
                reports[key] = {"title": title, "markdown": m, "json": last_json_block(m)}
        ev = None
        if (d / "target_portfolio.json").exists():
            try:
                ev = self.evaluate(d)
            except Exception as e:  # noqa: BLE001
                ev = {"error": f"{type(e).__name__}: {e}"}
        pack = read_json(d / "pack.json", {}) or {}
        brief = "\n\n".join(t for t in (read_text(d / f) for f in ("daily_brief.md", "midweek_review.md",
                                                                       "event_review.md")) if t) or None
        return {
            "date": fid, "reports": reports, "order": [k for k, _ in self.ROLES],
            "memo": read_text(d / "committee_memo.md"), "blocked": read_text(d / "committee_blocked.md"),
            "daily_brief": brief, "proposal": read_json(d / "target_portfolio.json"),
            "evaluation": ev, "approval": read_json(d / "approval.json"),
            "execution": read_json(d / "execution.json"), "pack_md": read_text(d / "pack.md"),
            "pack_fund": pack.get("fund"), "base": self.rel(d),
            "files": sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()),
        }

    # ------------------------------------------------------------ audit
    def audit(self) -> dict:
        ledger = self.ledger()
        events = []
        for a in jsonl_tail(self.cfg.state_dir / "audit.jsonl", 1000):
            if a.get("event") in ("APPROVED", "REJECTED"):
                txt = f"{a.get('event')} proposal {a.get('folder')} via {a.get('via')}" + \
                      (f" — “{a.get('note')}”" if a.get("note") else "")
            else:
                txt = f"{a.get('event')}: " + json.dumps({k: v for k, v in a.items()
                                                         if k not in ("event", "time_hkt")}, default=str)[:300]
            events.append({"time": a.get("time_hkt"), "kind": "decision", "level": a.get("event"),
                           "text": txt, "folder": a.get("folder")})
        for a in jsonl_tail(self.cfg.state_dir / "alerts.jsonl", 2000):
            events.append({"time": a.get("time_hkt"), "kind": "alert", "level": a.get("level"), "text": a.get("msg")})
        for f in ledger.get("fills", []):
            events.append({"time": f.get("time"), "kind": "fill", "level": f.get("side"),
                           "text": f"{f.get('side')} {f.get('qty'):g} {f.get('symbol')} @ {f.get('price'):.3f} "
                                   f"(comm {f.get('commission', 0):.2f}) — {f.get('reason', '')}"})
        for w in self.weeks():
            if w["decision"]:
                events.append({"time": w["date"][:10], "kind": "committee", "level": w["decision"],
                               "text": f"Committee proposal {w['date']}: {w['decision']} ({w['status']})",
                               "folder": w["date"]})
        events.sort(key=lambda e: str(e.get("time") or ""), reverse=True)
        st = self.rel(self.cfg.state_dir)
        return {"events": events, "files": [f"{st}/{f}" for f in ("nav.csv", "audit.jsonl", "alerts.jsonl", "ledger.json")]}

    # ------------------------------------------------------------ marks
    def marks(self, latest, ledger, rows) -> dict:
        lv = self.live()
        pack_nav = None
        if lv.get("nav") is not None and lv.get("time_hkt"):
            return {"live": True, "nav": lv["nav"], "cash": lv["cash"], "gross": lv["gross"],
                    "drawdown": lv.get("drawdown"), "hwm": lv.get("hwm"), "day_pnl": lv.get("day_pnl"),
                    "day_pct": lv.get("day_pct"), "time_hkt": lv["time_hkt"], "market_open": lv.get("market_open")}
        cash = ledger.get("cash", self.cfg.starting_capital)
        mv = sum(r.get("market_value") or 0 for r in rows)
        nav = pack_nav or cash + mv
        hwm = ledger.get("hwm") or self.cfg.starting_capital
        navh = self.nav_history()
        prev = navh[-2]["nav"] if len(navh) > 1 else None
        return {"live": False, "nav": nav, "cash": cash, "gross": mv / nav if nav else 0,
                "drawdown": min(0.0, nav / hwm - 1) if hwm else 0, "hwm": hwm,
                "day_pnl": (navh[-1]["nav"] - prev) if prev else None,
                "day_pct": (navh[-1]["nav"] / prev - 1) if prev else None}

    def open_orders(self, account) -> list:
        return [o for o in account.get("open_orders", []) or []
                if o.get("currency", "USD") == self.currency or str(o.get("ref") or "").startswith(self.spec.order_ref)]

    # ------------------------------------------------------------ cover card
    def summary(self) -> dict:
        o = self.overview()
        m, navh = o["marks"], o["nav_history"]
        base = navh[0]["nav"] if navh and navh[0].get("nav") else None
        b0 = (o.get("benchmarks") or [{}])[0]
        bret = None
        if navh and b0.get("key") and navh[0].get(b0["key"]) and navh[-1].get(b0["key"]):
            bret = navh[-1][b0["key"]] / navh[0][b0["key"]] - 1
        ev = o.get("events") or {}
        today = ev.get("today") or datetime.now(HKT).date().isoformat()
        return {
            "id": self.id, "label": self.label, "kind": self.kind, "currency": self.currency,
            "fund_name": o["fund_name"], "strategy": self.STRATEGY, "capital": o["starting_capital"],
            "inception": o["inception_date"], "asof": o["latest"].get("asof"),
            "stale": bool(o["latest"].get("data_stale")), "marks": m,
            "since": m["nav"] / o["starting_capital"] - 1 if m.get("nav") else None,
            "n_pos": len(o["holdings"].get("rows", [])), "pending": o["pending_approval"],
            "queued": bool(o["approved_target"].get("decision") and not o["approved_target"].get("executed")),
            "halted": bool(o["ledger"].get("halted")), "mandate": o["risk"]["mandate"],
            "pills": o["pills"], "bench": {"label": b0.get("label"), "ret": bret},
            "nav_index": [{"date": r["date"], "v": r["nav"] / base * 100} for r in navh if base and r.get("nav")],
            "alerts": [a for a in o["alerts"][:20]
                       if a.get("level") in ("WARN", "ERROR", "CRITICAL", "RISK")][:5],
            "events": [e for e in ev.get("events", []) if e["date"] >= today][:6],
            "top": sorted(o["holdings"].get("rows", []), key=lambda r: -(r.get("weight") or 0))[:5],
            "extra": (o.get("kpis_extra") or [None])[0],
        }

    # ------------------------------------------------------------ hooks
    MECH_KEYS = ["fund_name", "starting_capital", "inception_date", "max_drawdown_mandate", "dd_ladder",
                 "regime_caps", "candidate_pool", "min_price", "min_dollar_volume", "target_names",
                 "min_weight", "max_weight", "max_sector_weight", "max_monthly_turnover", "rebalance_band",
                 "cash_buffer", "stop_loss_from_entry", "trailing_stop_from_peak", "rank_exit_threshold",
                 "rank_exit_weeks", "server_side_stop", "sentinel_interval_sec", "shock_intraday_drop",
                 "crash_intraday_drop", "crash_3day_drop", "crash_exposure", "vix_alert", "vix_crash",
                 "max_order_value", "rebalance_retry_days", "execute_on_approval", "order_style"]

    def mechanics(self) -> dict:
        """Live config + current state for the fund's 'How it works' page."""
        latest, ledger = self.latest(), self.ledger()
        pack = self.pack(latest)
        return {"fund": self.id, "cfg": {k: getattr(self.cfg, k, None) for k in self.MECH_KEYS},
                "asof": latest.get("asof"), "drawdown": latest.get("drawdown"),
                "regime": pack.get("regime", {}), "risk_caps": pack.get("risk_caps", {}),
                "halted": bool(ledger.get("halted")) or self.cfg.kill_switch.exists(),
                "buys_frozen_until": ledger.get("buys_frozen_until") or ""}

    def overview(self) -> dict:
        raise NotImplementedError

    def market(self) -> dict:
        return {"blocks": []}


# =========================================================================== #
class USAdapter(FundAdapter):
    ROLES = [("macro", "Macro Strategist"), ("sector", "Sector Analyst"),
             ("fundamental", "Fundamental Analyst"), ("technical", "Technical Analyst"),
             ("strategy", "Portfolio Strategist"), ("pm", "Portfolio Manager"), ("risk", "Risk Manager")]
    FLOW = "Research pack → Macro · Sector · Fundamental · Technical → Strategist → PM → Risk (+code check) → CIO memo → Your approval → Execution"
    STRATEGY = "Stable growth: quant screen of the S&P 500 (momentum, quality, trend, growth), weekly committee, drawdown ladder."
    EXEC_NOTE = ("Sized at the last close; re-sized with live prices at execution. Approving during US hours "
                 "(09:40–15:40 New York time) sends the orders within a minute; otherwise they go at the next "
                 "scheduled run (21:40 HKT, retry 22:40). IBKR Adaptive market orders; GTC −20% stops refreshed after.")
    MARKET_NAME = "US market"
    HOW_PAGE = "how.html"
    DETAIL_COLS = ["name", "sector", "industry", "rank", "score", "pe_ttm", "fcf_yield",
                   "market_cap", "revenue_growth", "eps_growth_proxy", "gross_margin",
                   "operating_margin", "roe", "debt_to_equity", "share_change_1y", "ret_1m",
                   "ret_3m", "ret_6m", "mom_12_1", "dist_52w_high", "rsi14", "vol_63d",
                   "atr20_pct", "above_sma200", "sma50_above_sma200", "price"]

    def folders(self):                       # US research folders are flat <date>/
        rd = self.cfg.research_dir
        if not rd.exists():
            return []
        return sorted((d for d in rd.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True)

    def after_approve(self, d):
        return self.appr.trigger_execution(self.cfg, d)

    def _theses(self) -> dict:
        out = {}
        for d in self.appr.proposal_folders(self.cfg):
            prop = read_json(d / "target_portfolio.json", {}) or {}
            for h in prop.get("holdings", []):
                if h.get("symbol"):
                    out[h["symbol"]] = {**h, "from_proposal": d.name}
        return out

    def holdings(self, latest, ledger) -> dict:
        import pandas as pd
        cfg = self.cfg
        positions = ledger.get("positions", {}) or {}
        live = self.live()
        asof = latest.get("asof")
        scr = pd.DataFrame()
        if asof and (cfg.research_dir / asof / "screen_full.csv").exists():
            scr = pd.read_csv(cfg.research_dir / asof / "screen_full.csv", index_col=0)
        sector_pe = {}
        if len(scr) and "pe_ttm" in scr:
            pe = pd.to_numeric(scr["pe_ttm"], errors="coerce")
            sector_pe = pe[(pe > 0) & (pe < 300)].groupby(scr["sector"]).median().round(1).to_dict()
        theses = self._theses()
        lp = live.get("positions", {}) or {}
        rows = []
        for sym, p in positions.items():
            r = {"symbol": sym, "qty": p.get("qty"), "avg_cost": p.get("avg_cost"),
                 "entry_date": p.get("entry_date"), "peak_close": p.get("peak_close")}
            if sym in scr.index:
                row = scr.loc[sym]
                r.update({c: (row[c].item() if hasattr(row[c], "item") else row[c])
                          for c in self.DETAIL_COLS if c in scr.columns})
            close_px = r.get("price")
            q = lp.get(sym, {})
            px = q.get("price") or close_px or p.get("avg_cost")
            r.update({"close_price": close_px, "price": px, "price_source": q.get("source") or "last close",
                      "prev_close": q.get("prev_close"), "day_chg": q.get("day_chg"),
                      "bid": q.get("bid"), "ask": q.get("ask")})
            pe = r.get("pe_ttm")
            if pe is not None and not (0 < pe < 1000):
                r["pe_ttm"] = pe = None
            if pe and close_px and px:
                r["pe_live"] = pe * px / close_px
            if r.get("market_cap") and close_px and px:
                r["market_cap"] = r["market_cap"] * px / close_px
            r["sector_median_pe"] = sector_pe.get(r.get("sector"))
            qty, cost = r["qty"] or 0, r["avg_cost"] or 0
            r["market_value"] = qty * px
            r["unrealized_pnl"] = qty * (px - cost)
            r["unrealized_pct"] = px / cost - 1 if cost else None
            peak = max(p.get("peak_close") or px, px)
            r["from_peak"] = px / peak - 1
            r["stop_entry_at"] = cost * (1 - cfg.stop_loss_from_entry)
            r["trail_stop_at"] = peak * (1 - cfg.trailing_stop_from_peak) if peak > cost else None
            r["server_stop_at"] = cost * (1 - cfg.server_side_stop)
            r["active_stop"] = max(x for x in (r["stop_entry_at"], r["trail_stop_at"] or 0))
            r["dist_to_stop"] = r["active_stop"] / px - 1 if px else None
            r["pe_vs_sector"] = r["pe_live"] / r["sector_median_pe"] - 1 if r.get("pe_live") and r.get("sector_median_pe") else None
            r["rets"] = " / ".join(_p(r.get(k), 0, True) for k in ("ret_1m", "ret_3m", "ret_6m"))
            r["rank_score"] = f"{r.get('rank', '–')} / " + (f"{r['score']:.2f}" if r.get("score") is not None else "–")
            t = theses.get(sym)
            if t:
                r["thesis"] = {k: t.get(k) for k in ("thesis", "bear_case", "review_trigger", "conviction", "from_proposal")}
            rows.append(r)
        nav = live.get("nav")
        if nav is None:
            nav = (ledger.get("cash") or 0) + sum(r["market_value"] for r in rows)
        for r in rows:
            r["weight"] = r["market_value"] / nav if nav else 0
        rows.sort(key=lambda r: -r["weight"])
        return {
            "rows": rows, "close_asof": asof,
            "cols": [["symbol", "Holding", "sym"], ["qty", "Qty", "qty"], ["avg_cost", "Avg cost", "money"],
                     ["price", "Price", "price"], ["day_chg", "Day", "pct2s"], ["market_value", "Value", "money"],
                     ["weight", "Weight", "pct"], ["unrealized_pnl", "P&L", "moneys"], ["unrealized_pct", "P&L %", "pct1s"],
                     ["pe_live", "P/E", "pe"], ["fcf_yield", "FCF yld", "pct"], ["revenue_growth", "Rev gr.", "pct0s"],
                     ["dist_to_stop", "To stop", "stop"], ["rank", "Rank", "int"]],
            "detail": [
                {"title": "Valuation", "items": [["P/E (TTM, at current price)", "pe_live", "num1"], ["P/E at last close", "pe_ttm", "num1"],
                                                 ["Sector median P/E", "sector_median_pe", "num1"], ["P/E vs sector", "pe_vs_sector", "pct0s"],
                                                 ["FCF yield", "fcf_yield", "pct"], ["Market cap", "market_cap", "mcap"]]},
                {"title": "Growth & quality", "items": [["Revenue growth (TTM)", "revenue_growth", "pct1s"], ["Earnings growth (TTM)", "eps_growth_proxy", "pct1s"],
                                                        ["Gross margin", "gross_margin", "pct"], ["Operating margin", "operating_margin", "pct"],
                                                        ["ROE", "roe", "pct"], ["Debt / equity", "debt_to_equity", "num2"], ["Share count, 1y", "share_change_1y", "pct1s"]]},
                {"title": "Trend & momentum", "items": [["1M / 3M / 6M", "rets", "text"], ["12-1 month momentum", "mom_12_1", "pct0s"],
                                                        ["vs 52-week high", "dist_52w_high", "pct"], ["Above 200-day avg", "above_sma200", "yn"],
                                                        ["50-day above 200-day", "sma50_above_sma200", "yn"], ["RSI(14)", "rsi14", "num0"],
                                                        ["Volatility (63d, ann.)", "vol_63d", "pct0"], ["Screen rank / score", "rank_score", "text"]]},
                {"title": "Position & stops", "items": [["Entered", "entry_date", "text"], ["Avg cost", "avg_cost", "money"], ["Price", "price", "money"],
                                                        ["Previous close", "prev_close", "money"], ["From peak close", "from_peak", "pct"],
                                                        [f"Stop −{cfg.stop_loss_from_entry:.0%} from entry", "stop_entry_at", "money"],
                                                        [f"Trailing stop −{cfg.trailing_stop_from_peak:.0%} from peak", "trail_stop_at", "money"],
                                                        ["Server GTC stop at IBKR", "server_stop_at", "money"], ["Distance to active stop", "dist_to_stop", "pct"]]}],
            "note": "P/E = price ÷ trailing-12-month net income from SEC filings, re-priced to the current price. "
                    "Sector P/E = median of S&P 500 members in that sector. Price source shown on hover."}

    def events(self, ledger) -> dict:
        from fh import midweek
        cfg = self.cfg
        today = datetime.now(HKT).date()
        start = today - timedelta(days=today.weekday())
        if today.weekday() == 6:
            start += timedelta(days=7)
        end = start + timedelta(days=34)
        rows = []
        cal = midweek.load_calendar(cfg)
        for e in cal.get("events", []):
            fri = datetime.strptime(e["date"], "%Y-%m-%d").weekday() == 4
            rows.append({"date": e["date"], "kind": "macro", "label": e["name"],
                         "detail": "US release / decision; " + ("reviewed by the Saturday committee"
                                   if fri else "mid-week review the next morning (08:17 HKT)")})
        for s, d in midweek.earnings_dates(cfg, list((ledger.get("positions") or {}).keys())).items():
            rows.append({"date": d, "kind": "earnings", "label": f"{s} earnings",
                         "detail": "holding; date from the latest weekly fundamental report"})
        d = start
        while d <= end:
            if d.weekday() == 5:
                rows.append({"date": d.isoformat(), "kind": "committee", "label": "Weekly committee",
                             "detail": "09:47 HKT (safeguard re-run 15:47 HKT)"})
            d += timedelta(days=1)
        rows = sorted((r for r in rows if start.isoformat() <= r["date"] <= end.isoformat()),
                      key=lambda r: (r["date"], r["kind"]))
        gate = read_json(cfg.state_dir / "midweek_trigger.json", {}) or {}
        gtxt = ""
        if gate.get("asof"):
            gtxt = (f"TRIGGERED — mid-week review after the {gate['asof']} close: " + "; ".join(gate.get("reasons") or [])
                    if gate.get("triggered") else f"Mid-week review not triggered after the {gate['asof']} close.")
        return {"start": start.isoformat(), "days": 35, "today": today.isoformat(), "events": rows,
                "kinds": [["macro", "Macro", 1], ["earnings", "Earnings", 2], ["committee", "Committee", 3]],
                "note": "US dates. Macro events and holdings' earnings trigger a mid-week review the next morning "
                        "(08:17 HKT); the weekly committee meets on Saturdays.",
                "gate": {"triggered": bool(gate.get("triggered")), "text": gtxt},
                "warning": gate.get("calendar_warning")}

    def overview(self) -> dict:
        cfg = self.cfg
        latest, ledger = self.latest(), self.ledger()
        pack = read_json(Path(latest["pack"]), {}) if latest.get("pack") and Path(latest["pack"]).exists() else {}
        if not pack:
            pack = self.pack(latest)
        sentinel = (read_json(cfg.state_dir / "sentinel_state.json", {}) or {}).get("last")
        hold = safe(self.holdings, latest, ledger, default={"rows": [], "cols": [], "detail": []})
        f = pack.get("fund", {}) or {}
        m = self.marks(latest, ledger, hold.get("rows", []))
        if not m["live"] and f.get("nav") is not None:
            m.update(nav=f["nav"], cash=f.get("cash"), gross=f.get("gross_exposure"), drawdown=f.get("drawdown") or 0)
        caps = pack.get("risk_caps", {}) or {}
        regime = pack.get("regime", {}) or {}
        restart = read_json(cfg.state_dir / "restart_plan.json", {}) or {}
        pending = self.pending()
        exits = (read_json(cfg.state_dir / "pending_risk_exits.json", {}) or {}).get("exits", [])
        gw = port_open(cfg.ib_host, cfg.ib_port)
        banners = []
        if restart.get("status") == "liquidating":
            banners.append(["warn", f"<b>Fund restart in progress:</b> all positions are sold at the next US open (21:40 HKT), "
                                    f"then the fund restarts with ${restart.get('new_capital', 0):,.0f}. The current period is archived."])
        if ledger.get("halted"):
            banners.append(["crit", f"<b>Fund halted:</b> {E(ledger.get('halt_reason') or '')} — run <code>scripts/reset_hwm.py</code> to start a new risk period."])
        if ledger.get("buys_frozen_until") and ledger["buys_frozen_until"] >= (latest.get("asof") or ""):
            trig = next((x for x in self.alerts(200) if str(x.get("msg", "")).startswith("Market ")
                         and "frozen" in str(x.get("msg", ""))), None)
            banners.append(["warn", f"New buys frozen by the crash sentinel until <b>{E(ledger['buys_frozen_until'])}</b>"
                                    + (f" — trigger: {E(trig['msg'].split('. New buys')[0])} "
                                       f"({E(str(trig.get('time_hkt', ''))[:16].replace('T', ' '))} HKT)" if trig else "")
                                    + ". Approving a proposal now is fine: sells go at the next run, buys wait until the freeze ends."])
        if not latest.get("asof"):
            banners.append(["warn", "No data yet. Run <code>python3 scripts/check_setup.py</code> and <code>python3 scripts/daily_update.py</code> on the Mac."])
        if exits:
            banners.append(["crit", "<b>Risk exits queued for next open:</b> " + ", ".join(E(f"{e['symbol']} ({e['rule']})") for e in exits)])
        rg = regime.get("regime")
        lvl = (sentinel or {}).get("level")
        return {
            "id": self.id, "label": self.label, "kind": self.kind, "currency": self.currency,
            "now_hkt": datetime.now(HKT).isoformat(timespec="seconds"),
            "fund_name": cfg.fund_name, "starting_capital": cfg.starting_capital, "inception_date": cfg.inception_date,
            "latest": latest, "marks": m, "market_name": self.MARKET_NAME,
            "ledger": {k: ledger.get(k) for k in ("cash", "hwm", "halted", "halt_reason", "buys_frozen_until", "risk_period_start")},
            "nav_history": self.nav_history(),
            "benchmarks": [{"key": "shadow_nav", "label": "Shadow quant"}, {"key": "ew_level", "label": "Equal-weight S&P 500"}],
            "holdings": hold, "events": safe(self.events, ledger),
            "account": read_json(cfg.state_dir / "account.json", {}) or {},
            "alerts": self.alerts(),
            "pending_approval": pending, "approved_target": read_json(cfg.state_dir / "approved_target.json", {}) or {},
            "banners": banners,
            "risk": {"ladder": cfg.dd_ladder, "mandate": cfg.max_drawdown_mandate, "max_sector": cfg.max_sector_weight},
            "invested_lines": [f"Cap now {_p(caps.get('gross_cap'), 0)} (regime {_p(caps.get('regime_cap'), 0)}, "
                               f"ladder {_p(caps.get('ladder_cap'), 0)})"],
            "pills": [["good" if gw else "critical", "IB Gateway connected" if gw else "IB Gateway offline"],
                      ["critical", "KILL SWITCH ON"] if cfg.kill_switch.exists() else
                      [{"normal": "good", "shock": "warning", "crash": "critical", "blind": "critical"}.get(lvl, "muted"),
                       ("Sentinel: blind (no market data)" if lvl == "blind" else f"Sentinel: {lvl}") if lvl else "Sentinel: no data"],
                      [{"risk_on": "good", "neutral": "warning", "risk_off": "critical"}.get(rg, "muted"),
                       f"Regime: {rg.replace('_', ' ')} · cap {_p(caps.get('gross_cap'), 0)}" if rg else "Regime: —"]],
            "side_info": [["Crash sentinel — last check",
                           (f"{sentinel.get('time_et')} ET · level <b>{E(str(sentinel.get('level')))}</b> · market "
                            f"{_p(sentinel.get('market_chg'), 2, True)} · 3-day {_p(sentinel.get('three_day'), 2, True)} · "
                            f"VIX {sentinel.get('vix') or 'n/a'}") if sentinel else
                           '<span class="muted">Sentinel has not run in market hours yet.</span>']],
            "health": [["Data job", file_age_hours(cfg.logs_dir / "daily_update.log"), 30],
                       ["Sentinel log", file_age_hours(cfg.logs_dir / "sentinel.log"), 30],
                       ["Execution log", file_age_hours(cfg.logs_dir / "execute.log"), 200],
                       ["Account snapshot", file_age_hours(cfg.state_dir / "account.json"), 24]],
            "mechanics": bool(self.HOW_PAGE), "refresh": True,
        }

    def market(self) -> dict:
        o = self.pack()
        r = o.get("regime", {}) or {}
        yn = lambda v: "–" if v is None else ("yes" if v else "no")  # noqa: E731
        blocks = [
            {"type": "kv", "half": True, "title": "Market regime (computed by code)", "rows": [
                ["Regime", f"<b>{E((r.get('regime') or '–').replace('_', ' '))}</b> (score {r.get('score', '–')})"],
                ["Exposure cap from regime", _p(r.get("exposure_cap"), 0)],
                ["Equal-weight index above 10-month average", yn(r.get("ew_index_trend_up"))],
                ["Stocks above 200-day average", _p(r.get("breadth_above_200d"), 0)],
                ["Equal-weight index drawdown", _p(r.get("ew_index_drawdown"))],
                ["Equal-weight index, 1 month", _p(r.get("ew_index_ret_1m"), 1, True)],
                ["High-yield spread widening (3m > +1pt)", yn(r.get("hy_spread_widening_3m"))],
                ["VIX", str(r.get("vix") or "–")]]},
            {"type": "table", "half": True, "title": "Macro (FRED)",
             "cols": [["label", "Series", "text"], ["last", "Last", "raw"], ["c1", "1M", "raw"], ["c3", "3M", "raw"], ["c12", "12M", "raw"]],
             "rows": [{"label": m.get("label"), "last": m.get("last"),
                       **{k: (f"{m.get(src)}{'%' if m.get('chg_unit') == '%' else ''}" if m.get(src) is not None else "–")
                          for k, src in (("c1", "chg_1m"), ("c3", "chg_3m"), ("c12", "chg_12m"))}}
                      for m in o.get("macro", [])]},
            {"type": "table", "title": "Sectors (built from S&P 500 constituents — ETFs not tradable)",
             "cols": [["sector", "Sector", "text"], ["names", "Names", "int"], ["median_ret_3m", "3M median", "pct1s"],
                      ["median_ret_6m", "6M median", "pct1s"], ["pct_above_200d", "% above 200d", "pct0"], ["in_top40", "In top-40", "int"]],
             "rows": o.get("sectors", [])},
            {"type": "table", "title": f"Candidate pool — top {len(o.get('candidates', []))} of the quant screen", "filter": True, "sort": "rank",
             "note": "Agents may only buy from this list. Score = momentum 35% + quality 25% + trend 20% + growth 20% − high-volatility penalty.",
             "cols": [["rank", "#", "int"], ["symbol", "Symbol", "sym"], ["sector", "Sector", "text"], ["score", "Score", "num2"],
                      ["mom_12_1", "Mom 12-1", "pct0s"], ["ret_6m", "6M", "pct0s"], ["ret_1m", "1M", "pct0s"],
                      ["dist_52w_high", "vs 52w high", "pct0"], ["rsi14", "RSI", "num0"], ["vol_63d", "Vol", "pct0"],
                      ["pe_ttm", "P/E", "num0"], ["revenue_growth", "Rev growth", "pct0s"], ["roe", "ROE", "pct0"],
                      ["operating_margin", "Op margin", "pct0"]],
             "rows": o.get("candidates", [])},
            {"type": "text", "title": "Shadow quant portfolio (the benchmark)",
             "html": " · ".join(f"{E(s)} {_p(w)}" for s, w in (o.get("shadow_quant_portfolio") or {}).items()) or "–"},
        ]
        return {"asof": o.get("asof") or self.latest().get("asof"), "blocks": blocks}


# =========================================================================== #
def _p(v, d=1, sign=False):
    if v is None or v != v:
        return "–"
    return f"{'+' if sign and v > 0 else ''}{v * 100:.{d}f}%"


ADAPTERS = {"us": USAdapter}
