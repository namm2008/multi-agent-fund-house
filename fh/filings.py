"""SEC EDGAR filings (8-K events, 10-Q/10-K, late-filing notices) for the committee.

Source: the free EDGAR submissions API (data.sec.gov/submissions/CIK##########.json),
one request per company, plus the filing's index.json to find the press-release exhibit.

Stored:
  data/filings/filings.parquet        one row per filing (all universe names), deduplicated
  data/filings/text/<accession>.txt   plain text of an 8-K (main doc + EX-99 exhibits),
                                      fetched only for holdings and the candidate pool

Nothing here trades. Red-flag filings on holdings raise an alert; the committee and the daily
risk officer read the filings section of the pack and the text files.
"""
from __future__ import annotations

import html
import json
import re
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pandas as pd

FORMS = {"8-K", "8-K/A", "10-Q", "10-K", "10-Q/A", "10-K/A", "NT 10-K", "NT 10-Q"}

# 8-K item numbers -> (label, severity). red = rare and usually bad; amber = can matter.
ITEMS: Dict[str, tuple] = {
    "1.01": ("Material agreement", "info"),
    "1.02": ("Material agreement terminated", "amber"),
    "1.03": ("Bankruptcy / receivership", "red"),
    "1.04": ("Mine safety", "info"),
    "1.05": ("Cybersecurity incident", "red"),
    "2.01": ("Acquisition / disposal completed", "amber"),
    "2.02": ("Results of operations", "info"),
    "2.03": ("New debt obligation", "info"),
    "2.04": ("Debt acceleration triggered", "red"),
    "2.05": ("Restructuring / exit costs", "amber"),
    "2.06": ("Material impairment", "red"),
    "3.01": ("Delisting / listing-rule notice", "red"),
    "3.02": ("Unregistered equity sale", "amber"),
    "3.03": ("Change to shareholder rights", "amber"),
    "4.01": ("Auditor change", "red"),
    "4.02": ("Prior financials unreliable (restatement)", "red"),
    "5.01": ("Change in control", "amber"),
    "5.02": ("Director / officer change", "amber"),
    "5.03": ("Charter / bylaw amendment", "info"),
    "5.05": ("Code of ethics change / waiver", "amber"),
    "5.07": ("Shareholder vote results", "info"),
    "5.08": ("Shareholder director nominations", "info"),
    "7.01": ("Reg FD disclosure", "info"),
    "8.01": ("Other events", "info"),
    "9.01": ("Exhibits", "info"),
}
FORM_SEVERITY = {"NT 10-K": "red", "NT 10-Q": "red", "8-K/A": "info", "10-K/A": "amber",
                 "10-Q/A": "amber"}
SEV_ORDER = {"red": 0, "amber": 1, "info": 2}
MIN_INTERVAL = 0.12           # SEC fair-access limit is 10 requests/second


def _dir(cfg) -> Path:
    d = cfg.data_dir / "filings"
    (d / "text").mkdir(parents=True, exist_ok=True)
    return d


class _Sec:
    """Polite EDGAR client: SEC's required User-Agent, <= ~8 requests/second, retries."""

    def __init__(self, cfg, session=None):
        import requests
        self.s = session or requests.Session()
        self.h = {"User-Agent": cfg.sec_user_agent, "Accept-Encoding": "gzip, deflate"}
        self.last = 0.0

    def get(self, url: str, timeout: int = 30):
        for attempt in range(3):
            wait = MIN_INTERVAL - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            try:
                r = self.s.get(url, headers=self.h, timeout=timeout)
            except Exception:  # noqa: BLE001  network hiccup
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
            time.sleep(2 * (attempt + 1))      # 403/429/5xx: back off
        return None


def describe_items(items: str) -> List[dict]:
    out = []
    for it in [i.strip() for i in str(items or "").split(",") if i.strip()]:
        label, sev = ITEMS.get(it, (f"Item {it}", "info"))
        out.append({"item": it, "label": label, "severity": sev})
    return out


def severity(form: str, items: str) -> str:
    sevs = [d["severity"] for d in describe_items(items)] + [FORM_SEVERITY.get(form, "info")]
    return min(sevs, key=lambda s: SEV_ORDER[s])


def parse_submissions(js: dict, symbol: str, since: str) -> pd.DataFrame:
    """Flatten filings.recent of one submissions JSON into rows (forms in FORMS, >= since)."""
    r = (js.get("filings") or {}).get("recent") or {}
    if not r.get("accessionNumber"):
        return pd.DataFrame()
    df = pd.DataFrame({k: r.get(k, [None] * len(r["accessionNumber"])) for k in
                       ("accessionNumber", "filingDate", "reportDate", "acceptanceDateTime",
                        "form", "items", "primaryDocument", "primaryDocDescription")})
    df = df[df["form"].isin(FORMS) & (df["filingDate"] >= since)].copy()
    if df.empty:
        return df
    cik = str(int(js.get("cik", 0)))
    df.insert(0, "symbol", symbol)
    df.insert(1, "cik", cik)
    df["items"] = df["items"].fillna("")
    df["severity"] = [severity(f, i) for f, i in zip(df["form"], df["items"])]
    acc = df["accessionNumber"].str.replace("-", "", regex=False)
    df["url"] = [f"https://www.sec.gov/Archives/edgar/data/{cik}/{a}/{p}"
                 for a, p in zip(acc, df["primaryDocument"].fillna(""))]
    return df.rename(columns={"accessionNumber": "accession", "filingDate": "filed",
                              "reportDate": "report_date", "acceptanceDateTime": "accepted",
                              "primaryDocument": "primary_doc",
                              "primaryDocDescription": "description"})


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", raw)
    raw = re.sub(r"(?is)<ix:header>.*?</ix:header>", " ", raw)         # inline-XBRL metadata
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    txt = html.unescape(raw).replace("\xa0", " ")
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n\s*\n+", "\n\n", txt)
    return txt.strip()


def pick_documents(index_items: List[dict], primary: str) -> List[str]:
    """Primary 8-K document first, then EX-99.x press-release exhibits (htm/txt)."""
    names = [i.get("name", "") for i in index_items]
    ex = [n for n in names if re.search(r"(?i)ex[-_.]?99", n) and n.lower().endswith((".htm", ".html", ".txt"))]
    docs = [primary] if primary else []
    return docs + [n for n in sorted(ex) if n != primary]


def fetch_text(sec: _Sec, cfg, row, max_chars: int = 30_000) -> Optional[Path]:
    """Save main document + EX-99 exhibits of one filing as plain text (cached)."""
    out = _dir(cfg) / "text" / f"{row['accession']}.txt"
    if out.exists():
        return out
    acc = row["accession"].replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{row['cik']}/{acc}/"
    r = sec.get(base + "index.json")
    items = (r.json().get("directory") or {}).get("item", []) if r is not None else []
    parts = []
    for name in pick_documents(items, row.get("primary_doc") or "")[:4]:
        d = sec.get(base + name)
        if d is None:
            continue
        parts.append(f"===== {name} =====\n{html_to_text(d.text)}")
    if not parts:
        return None
    head = (f"{row['symbol']} {row['form']} filed {row['filed']} (accepted {row['accepted']})\n"
            f"Items: {row['items'] or '-'}  |  {row['url']}\n\n")
    out.write_text((head + "\n\n".join(parts))[:max_chars])
    return out


def load_filings(cfg) -> pd.DataFrame:
    p = _dir(cfg) / "filings.parquet"
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def update_filings(cfg, universe: pd.DataFrame, symbols: Iterable[str], text_symbols: Iterable[str],
                   log=print, lookback_days: int = 400, text_days: int = 45, session=None) -> dict:
    """Refresh filings for `symbols` (needs a CIK in the universe) and fetch the text of recent
    8-Ks for `text_symbols`. Returns a small status dict; never raises on network errors."""
    sec = _Sec(cfg, session)
    cik = {s: str(c).zfill(10) for s, c in zip(universe["ib_symbol"], universe["CIK"]) if pd.notna(c)}
    since = (pd.Timestamp.today() - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    frames, failed = [], []
    for s in sorted(set(symbols)):
        if s not in cik:
            continue
        r = sec.get(f"https://data.sec.gov/submissions/CIK{cik[s]}.json")
        if r is None:
            failed.append(s)
            continue
        try:
            frames.append(parse_submissions(r.json(), s, since))
        except Exception:  # noqa: BLE001
            failed.append(s)
    old = load_filings(cfg)
    new = pd.concat([old] + frames, ignore_index=True) if frames else old
    if not new.empty:
        new = (new.drop_duplicates("accession", keep="last")
               .sort_values(["filed", "accepted"], ascending=False).reset_index(drop=True))
        new.to_parquet(_dir(cfg) / "filings.parquet")
    texts = 0
    if not new.empty:
        cut = (pd.Timestamp.today() - pd.Timedelta(days=text_days)).strftime("%Y-%m-%d")
        want = new[new["symbol"].isin(set(text_symbols)) & new["form"].str.startswith(("8-K", "NT"))
                   & (new["filed"] >= cut)]
        for _, row in want.iterrows():
            if not (_dir(cfg) / "text" / f"{row['accession']}.txt").exists():
                texts += fetch_text(sec, cfg, row) is not None
    log(f"filings: {len(frames)} companies refreshed, {len(failed)} failed, "
        f"{texts} new 8-K texts saved")
    return {"refreshed": len(frames), "failed": failed, "texts": texts}


# --------------------------------------------------------------------------- pack / alerts
def recent(cfg, symbols: Iterable[str], asof: str, days: int = 30) -> pd.DataFrame:
    f = load_filings(cfg)
    if f.empty:
        return f
    cut = (pd.Timestamp(asof) - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
    f = f[f["symbol"].isin(set(symbols)) & (f["filed"] >= cut) & (f["filed"] <= asof)].copy()
    f["sev_rank"] = f["severity"].map(SEV_ORDER)
    return f.sort_values(["sev_rank", "filed"], ascending=[True, False])


def _labels(form: str, items: str) -> str:
    if form.startswith("NT"):
        return "Late-filing notice"
    if not form.startswith("8-K"):
        return {"10-Q": "Quarterly report", "10-K": "Annual report"}.get(form.split("/")[0], form)
    ds = [d for d in describe_items(items) if d["item"] != "9.01"]
    return "; ".join(f"{d['item']} {d['label']}" for d in ds) or "Exhibits only"


def pack_section(cfg, holdings: List[str], pool: List[str], asof: str, days: int = 30) -> tuple:
    """Markdown section + JSON list of recent filings for holdings and the candidate pool."""
    f = recent(cfg, list(holdings) + list(pool), asof, days)
    if f.empty:
        return (f"\n## SEC filings (last {days} days): holdings and candidate pool\n\n"
                "No filings data (run scripts/update_filings.py).\n", [])
    rows, md = [], []
    for _, r in f.iterrows():
        tp = cfg.data_dir / "filings" / "text" / f"{r['accession']}.txt"
        rec = {"symbol": r["symbol"], "holding": r["symbol"] in holdings, "filed": r["filed"],
               "form": r["form"], "items": r["items"], "what": _labels(r["form"], r["items"]),
               "severity": r["severity"], "url": r["url"],
               "text_file": str(tp.relative_to(cfg.data_dir.parent)) if tp.exists() else None}
        rows.append(rec)
        flag = {"red": "**RED**", "amber": "amber", "info": ""}[r["severity"]]
        md.append(f"| {r['filed']} | {r['symbol']}{' (held)' if rec['holding'] else ''} | {r['form']} | "
                  f"{rec['what']} | {flag} | [doc]({r['url']}){' · text' if rec['text_file'] else ''} |")
    n_red = sum(r["severity"] == "red" for r in rows)
    head = (f"\n## SEC filings (last {days} days): holdings and candidate pool\n\n"
            f"{len(rows)} filings, {n_red} red-flag. Severity is by 8-K item type (red: bankruptcy, "
            f"restatement, auditor change, impairment, debt acceleration, delisting notice, cyber "
            f"incident, late filing). Read the saved text (data/filings/text/<accession>.txt) for any "
            f"red or amber filing on a holding or a name you propose to buy.\n\n"
            "| Filed | Symbol | Form | What | Flag | Link |\n|---|---|---|---|---|---|\n")
    return head + "\n".join(md) + "\n", rows


def new_red_flags(cfg, holdings: Iterable[str], asof: str, days: int = 3) -> List[dict]:
    f = recent(cfg, holdings, asof, days)
    if f.empty:
        return []
    f = f[f["severity"] == "red"]
    return [{"symbol": r["symbol"], "filed": r["filed"], "form": r["form"],
             "what": _labels(r["form"], r["items"]), "url": r["url"]} for _, r in f.iterrows()]


def watch_symbols(cfg, holdings: Iterable[str], top: int = 60) -> List[str]:
    """Holdings + the top `top` names of the latest saved screen + names in the latest
    committee target: the names whose filings are refreshed daily and whose 8-K text is saved."""
    out = set(holdings)
    dirs = sorted(p for p in cfg.research_dir.glob("20*") if p.is_dir())
    for d in reversed(dirs):
        sc = d / "screen_full.csv"
        if sc.exists():
            s = pd.read_csv(sc, index_col=0)
            if "rank" in s:
                out |= set(s[s["rank"] <= top].index.astype(str))
            break
    for d in reversed(dirs):
        t = d / "target_portfolio.json"
        if t.exists():
            try:
                out |= {h["symbol"] for h in json.loads(t.read_text()).get("holdings", [])
                        if isinstance(h, dict) and h.get("symbol")}
            except Exception:  # noqa: BLE001
                pass
            break
    return sorted(out)
