import json
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fh.config import Config  # noqa: E402
from fh import filings  # noqa: E402

TODAY = pd.Timestamp.today().strftime("%Y-%m-%d")
D1 = (pd.Timestamp.today() - pd.Timedelta(days=2)).strftime("%Y-%m-%d")
D2 = (pd.Timestamp.today() - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
OLD = (pd.Timestamp.today() - pd.Timedelta(days=900)).strftime("%Y-%m-%d")


def sub_json(cik, rows):
    keys = ["accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "form", "items",
            "primaryDocument", "primaryDocDescription"]
    return {"cik": str(cik), "filings": {"recent": {k: [r.get(k, "") for r in rows] for k in keys}}}


class Resp:
    def __init__(self, status, body):
        self.status_code, self._b = status, body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return self._b if not isinstance(self._b, str) else json.loads(self._b)


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, headers=None, timeout=None):
        assert "contact" in headers["User-Agent"]
        self.calls.append(url)
        for k, v in self.routes.items():
            if url.endswith(k):
                return Resp(200, v)
        return Resp(404, "")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(filings, "MIN_INTERVAL", 0.0)
    c = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state",
                research_dir=tmp_path / "research", logs_dir=tmp_path / "logs")
    c.ensure_dirs()
    return c


def test_severity_and_labels():
    assert filings.severity("8-K", "2.02,9.01") == "info"
    assert filings.severity("8-K", "5.02,9.01") == "amber"
    assert filings.severity("8-K", "4.02,9.01") == "red"
    assert filings.severity("NT 10-Q", "") == "red"
    assert filings._labels("8-K", "2.02,9.01") == "2.02 Results of operations"
    assert filings._labels("10-Q", "") == "Quarterly report"


def test_html_to_text_and_pick_documents():
    t = filings.html_to_text("<html><head><title>x</title></head><body><p>Net sales rose&nbsp;8%"
                             "</p><script>var a</script><div>Q3 &amp; guidance</div></body></html>")
    assert "Net sales rose 8%" in t and "Q3 & guidance" in t and "var a" not in t
    docs = filings.pick_documents([{"name": "a8k.htm"}, {"name": "ex99-1.htm"},
                                   {"name": "R1.xml"}, {"name": "aex991.htm"}], "a8k.htm")
    assert docs[0] == "a8k.htm" and set(docs[1:]) == {"ex99-1.htm", "aex991.htm"}


def test_update_pack_and_red_flag_alert(cfg):
    uni = pd.DataFrame({"ib_symbol": ["AAA", "BBB", "CCC"], "CIK": ["0000000001", "0000000002", None]})
    routes = {
        "CIK0000000001.json": sub_json(1, [
            {"accessionNumber": "0000000001-26-000010", "filingDate": D1, "acceptanceDateTime": D1 + "T16:05:00.000Z",
             "form": "8-K", "items": "4.02,9.01", "primaryDocument": "a8k.htm"},
            {"accessionNumber": "0000000001-26-000009", "filingDate": D2, "form": "10-Q",
             "primaryDocument": "a10q.htm"},
            {"accessionNumber": "0000000001-26-000008", "filingDate": D2, "form": "4"},          # ignored
            {"accessionNumber": "0000000001-23-000001", "filingDate": OLD, "form": "8-K", "items": "2.02"}]),
        "CIK0000000002.json": sub_json(2, [
            {"accessionNumber": "0000000002-26-000003", "filingDate": D2, "form": "8-K",
             "items": "2.02,9.01", "primaryDocument": "b8k.htm"}]),
        "000000000126000010/index.json": {"directory": {"item": [{"name": "a8k.htm"}, {"name": "ex99-1.htm"}]}},
        "000000000126000010/a8k.htm": "<p>Item 4.02 Non-Reliance on previously issued financials</p>",
        "000000000126000010/ex99-1.htm": "<p>Press release: restatement of FY2025</p>",
    }
    sess = FakeSession(routes)
    st = filings.update_filings(cfg, uni, ["AAA", "BBB", "CCC"], ["AAA"], log=lambda m: None,
                                session=sess)
    assert st["refreshed"] == 2 and st["texts"] == 1 and st["failed"] == []
    f = filings.load_filings(cfg)
    assert set(f["form"]) == {"8-K", "10-Q"} and len(f) == 3          # Form 4 and old 8-K dropped
    txt = (cfg.data_dir / "filings" / "text" / "0000000001-26-000010.txt").read_text()
    assert "Non-Reliance" in txt and "restatement of FY2025" in txt and "AAA 8-K" in txt
    # second run: no duplicate rows, no refetch of text
    n_calls = len(sess.calls)
    filings.update_filings(cfg, uni, ["AAA"], ["AAA"], log=lambda m: None, session=sess)
    assert len(filings.load_filings(cfg)) == 3 and len(sess.calls) == n_calls + 1

    md, rows = filings.pack_section(cfg, ["AAA"], ["BBB"], TODAY)
    assert "**RED**" in md and "AAA (held)" in md and "restatement" in md
    assert rows[0]["symbol"] == "AAA" and rows[0]["severity"] == "red" and rows[0]["text_file"]
    red = filings.new_red_flags(cfg, ["AAA", "BBB"], TODAY)
    assert [r["symbol"] for r in red] == ["AAA"]


def test_watch_symbols(cfg):
    d = cfg.research_dir / "2026-10-16"
    d.mkdir(parents=True)
    pd.DataFrame({"rank": [1, 2, 99]}, index=["X1", "X2", "X3"]).to_csv(d / "screen_full.csv")
    (d / "target_portfolio.json").write_text(json.dumps({"holdings": [{"symbol": "T1", "weight": 0.05}]}))
    w = filings.watch_symbols(cfg, ["H1"])
    assert w == ["H1", "T1", "X1", "X2"]
