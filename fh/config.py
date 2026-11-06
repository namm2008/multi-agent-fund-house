"""Fund mandate and every tunable number, in one place.

The LLM agents READ these limits (via the research pack) but can never change
them. Only a human editing this file can loosen a risk limit.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    # ---- Identity ------------------------------------------------------
    fund_name: str = os.environ.get("FH_FUND_NAME", "Fund House — US Equity Paper Fund")
    # SEC EDGAR and FRED ask for a descriptive User-Agent with a contact e-mail.
    # Set FH_CONTACT_EMAIL (see .env.example); never commit your real address.
    contact_email: str = os.environ.get("FH_CONTACT_EMAIL", "you@example.com")
    sec_user_agent: str = os.environ.get(
        "FH_SEC_UA",
        f"FundHouse research bot (contact: {os.environ.get('FH_CONTACT_EMAIL', 'you@example.com')})")

    # ---- Capital ---------------------------------------------------------
    # The fund keeps its OWN ledger. If the paper account holds more than this
    # (IBKR paper defaults to $1M), the fund still only ever deploys its
    # sleeve: starting capital + realised/unrealised P&L of fund positions.
    starting_capital: float = 200_000.0
    inception_date: str = "2026-10-16"

    # ---- IBKR ------------------------------------------------------------
    ib_host: str = os.environ.get("FH_IB_HOST", "127.0.0.1")
    ib_port: int = int(os.environ.get("FH_IB_PORT", "4002"))   # Gateway paper
    ib_client_id_data: int = 31
    ib_client_id_sentinel: int = 32
    ib_client_id_exec: int = 33
    paper_only: bool = True           # refuse live ports / non-DU accounts
    # 1 = live, 3 = delayed. Paper accounts without shared subscriptions only
    # get delayed data; 15-min delay is acceptable for a mid/long-term fund.
    market_data_type: int = 3

    # ---- Universe ----------------------------------------------------------
    # Built for accounts where US-listed ETFs are blocked (e.g. EU/UK retail
    # under PRIIPs), so the fund trades single stocks only.
    allow_etfs: bool = False
    universe_source: str = (
        "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
        "main/data/constituents.csv")
    min_price: float = 10.0
    min_dollar_volume: float = 50_000_000.0     # 20-day avg $ volume
    history_days: int = 420                    # ~ 20 months of daily bars

    # ---- Portfolio construction -----------------------------------------
    target_names: Tuple[int, int] = (10, 15)   # min, max holdings when invested
    max_weight: float = 0.10                   # per stock, of fund NAV
    min_weight: float = 0.04
    max_sector_weight: float = 0.30
    candidate_pool: int = 40                   # agents may only pick from the top-N screen
    # Rank-buffer exit (exit-rule study, Oct 2026): a holding ranked worse than this on
    # `rank_exit_weeks` consecutive weekly reviews is sold automatically at the next open.
    rank_exit_threshold: int = 60
    rank_exit_weeks: int = 2
    max_monthly_turnover: float = 0.40         # one-way, fraction of NAV
    rebalance_band: float = 0.02               # ignore weight drifts smaller than this
    allow_fractional: bool = False             # test in paper before enabling
    cash_buffer: float = 0.02                  # always keep >= 2% cash

    # ---- Risk: drawdown ladder (fund NAV vs its high-water mark) --------
    max_drawdown_mandate: float = 0.20
    # (drawdown threshold, maximum gross exposure allowed at/after it)
    dd_ladder: List[Tuple[float, float]] = field(default_factory=lambda: [
        (-0.08, 0.75),
        (-0.12, 0.50),
        (-0.15, 0.00),   # hard floor: flat. 5% buffer left for gaps/slippage.
    ])

    # ---- Risk: market regime (computed daily, no LLM) --------------------
    # exposure cap by regime; the committee may go BELOW these, never above.
    regime_caps: dict = field(default_factory=lambda: {
        "risk_on": 1.00, "neutral": 0.70, "risk_off": 0.30})

    # ---- Risk: per-position stops ------------------------------------------
    stop_loss_from_entry: float = 0.15       # close-basis, checked by sentinel
    trailing_stop_from_peak: float = 0.25    # once the position is in profit
    server_side_stop: float = 0.20           # GTC stop parked at IBKR (works if Mac is off)

    # ---- Risk: crash sentinel (intraday) -----------------------------------
    sentinel_interval_sec: int = 300
    shock_intraday_drop: float = -0.03       # market proxy: freeze new buys + alert
    crash_intraday_drop: float = -0.05       # market proxy: cut gross to crash_exposure
    crash_3day_drop: float = -0.08
    crash_exposure: float = 0.50
    vix_alert: float = 30.0
    vix_crash: float = 40.0
    # Proxy basket used when SPX index data is not subscribed.
    market_proxy: List[str] = field(default_factory=lambda: [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "BRK B",
        "JPM", "LLY", "XOM", "UNH", "V", "COST", "HD", "PG", "JNJ", "WMT"])

    # ---- Execution ---------------------------------------------------------
    limit_offset_bps: float = 10.0           # (legacy, unused)
    # Marketable limit: buy up to ref x (1 + cap), sell down to ref x (1 - cap). The fill
    # happens at the market price; the cap only protects against a bad print.
    # "adaptive" (IBKR Adaptive algo market order, default: the paper account only has
    # 15-min delayed quotes, which cannot price a limit), "market", or "limit" (capped
    # limit; switch to it only once live quotes are shared with the paper account).
    order_style: str = "adaptive"
    execute_on_approval: bool = True         # approval during US hours trades right away
    limit_cap_bps: float = 50.0
    order_reprice_after_sec: int = 180       # re-price unfilled orders once after 3 min
    order_wait_sec: int = 600                # then wait up to 10 min before cancelling
    rebalance_retry_days: int = 3            # unfilled approved buys are retried this many sessions
    order_tif: str = "DAY"
    max_order_value: float = 30_000.0          # fat-finger guard per order

    # ---- Paths -------------------------------------------------------------
    data_dir: Path = ROOT / "data"
    state_dir: Path = ROOT / "state"
    research_dir: Path = ROOT / "research"
    logs_dir: Path = ROOT / "logs"
    kill_switch: Path = ROOT / "STOP_TRADING"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.state_dir, self.research_dir, self.logs_dir,
                  self.data_dir / "bars", self.data_dir / "fundamentals",
                  self.data_dir / "macro"):
            p.mkdir(parents=True, exist_ok=True)


CONFIG = Config()
