# Design: feasibility, architecture and risk

This is the design note behind the Multi-Agent Fund House. It covers what can and can't work, how the system is split, and the evidence behind the drawdown limits.

**The brief:** a multi-agent system that imitates a fund house (macro, sector, fundamental and technical analysts, a strategist, a PM, a risk manager and a CIO). It trades US stocks mid-to-long-term in an IBKR paper account, picks stocks within strict limits, watches for crashes, and keeps a 20% maximum drawdown.

**Account constraint:** the design assumes US-listed ETFs are unavailable, as they are for many EU/UK retail accounts under PRIIPs. The fund therefore trades single stocks only, and cash is its only defensive asset.

---

## 1. Three critical obstacles

None of these kills the idea, but each forces a design choice. Most multi-agent trading demos skip them.

1. **An LLM committee can't be backtested honestly.** The models have read about the years you would test on, so any backtest of their decisions leaks the future (see *Look-Ahead-Bench*, 2026). Published multi-agent LLM trading results (TradingAgents and others) cover a few months on a handful of tickers. That isn't evidence of an edge.
   → **Fix:** a deterministic quant core that *can* be backtested does the heavy lifting. The agents add judgement on top, within limits. A **shadow quant-only portfolio** runs every week to measure whether the agents add anything.

2. **"Agents that constantly watch for a crash" can't be LLM agents.** Scheduled agent runs fire at most hourly, start a fresh session each time, and depend on a linked computer being online. A crash can take 5% off the market in an hour.
   → **Fix:** the crash watch is a **Python sentinel** that checks every 5 minutes during US hours and acts on its own. A **GTC stop on IBKR's servers** protects every holding even if the computer is off. An LLM "Daily Risk Officer" reviews what happened each morning.

3. **A 20% maximum drawdown can't be guaranteed, only engineered for.** Gaps, correlated crashes and slippage mean stops fill below their trigger. The S&P 500 itself fell 49% (2008), 34% (2020) and 25% (2022).
   → **Fix:** layered defences whose last step goes to cash at **−15%**, leaving a 5% buffer for gaps (Section 3).

**Realistic expectation:** the first 6–12 months are about proving the *process*: it runs unattended, respects risk, and beats its own quant shadow after costs. Beating the index is not the first goal.

## 2. Obstacle register

| # | Obstacle | Severity | How the design handles it |
|---|---|---|---|
| 1 | LLM decisions can't be backtested (look-ahead / training leakage) | **Critical** | Backtestable quant core. LLMs may only pick from its top-40. A shadow portfolio measures their value-add. The forward paper test is the real test |
| 2 | Crash watch needs minute-level reaction; agent schedules are ≥ hourly | **Critical** | Python sentinel every 5 min plus server-side GTC stops. No LLM in the emergency loop |
| 3 | 20% max DD can't be guaranteed (gaps, correlation → 1 in crashes) | **Critical** | Regime cap, drawdown ladder, stops and crash rules, with the hard floor at −15% |
| 4 | No ETFs | High | No index hedge, no T-bill ETF, no sector ETFs. Cash is the defensive asset. Sector and market indices are rebuilt from constituents |
| 5 | LLMs hallucinate numbers | High | Agents get every number from a code-built research pack and must cite URLs for anything else. Hard limits live in code (`fh/risk.py`), not prompts. A human approves every rebalance |
| 6 | Position granularity (whole shares, minimum commissions) | Medium | `fh/sizing.py` checks how well the fund can hit target weights at its size. The risk check flags names whose share price makes a weight impossible |
| 7 | IB Gateway uptime (daily restart, weekly re-login), computer sleep | Medium | Gateway auto-restart, no-sleep, an alert when the sentinel can't connect in market hours. Server-side stops cover outages. Docker/VPS template for later |
| 8 | Free data quality (EDGAR tags vary, FRED lags, no free earnings calendar) | Medium | Point-in-time EDGAR parsing (filing dates, so no look-ahead). Stale data blocks the committee. Agents verify earnings dates by web search or say "unverified" |
| 9 | Survivorship bias in the core backtest (today's S&P 500 list) | Medium | Flagged in the output. Treat results as an upper bound |
| 10 | Paper fills are kinder than live | Low | Marketable orders and a cost model |

## 3. Is a 20% maximum drawdown realistic? Tested on S&P 500 history

S&P 500 total return, **monthly data 1950–2026** (Shiller dataset). Cash earns 0% (conservative). Reproduce with `scripts/stress_test_index_overlay.py`.

| Rule set | CAGR | Max DD | Vol | Calmar |
|---|---|---|---|---|
| Buy & hold | 11.7% | **−49.0%** | 12.0% | 0.24 |
| Drawdown ladder only (−8/−12/−16%) | 9.6% | −24.6% | 9.5% | 0.39 |
| 10-month trend filter | 10.8% | **−18.9%** | 8.8% | 0.57 |
| Trend filter + ladder | 10.2% | −18.9% | 8.6% | 0.54 |

Crisis by crisis (trend filter + ladder vs buy & hold): 1973–74 −13% vs −39% · 2000–02 −10% vs −42% · 2008 −5% vs −49% · 2022 −5% vs −19% · **1987 −15% vs −26% · 2020 −19% vs −19%**.

**How to read it:**
- The trend filter is what keeps drawdowns near 20%. It worked in every *slow* bear market, for about 1 point of CAGR and roughly 12 switches per decade.
- It **fails on fast crashes** (1987, 2020): the market had already fallen before a monthly rule could react. Monthly prices also *understate* true daily drawdowns (2020 was −34% daily, −19% here). **That's why the fund needs the intraday sentinel.** The monthly regime handles bear markets; the sentinel handles crashes.
- A 10–15-stock portfolio is more volatile than the index (≈1.2–1.4× the drawdown), so the fund's ladder starts at −8% and goes flat at −15%, not −20%.
- This tests the index, not a stock-picking strategy. The fund-level test is `scripts/backtest_core.py`.

## 4. Architecture: three layers with separate jobs

| Layer | Runs where | Cadence | Does | Never does |
|---|---|---|---|---|
| **Data and rules** (Python) | Your computer now, VPS later | Daily after close | Bars (IBKR), fundamentals (SEC EDGAR), macro (FRED); screen, regime, caps; stops; ladder; research pack; NAV ledger + shadow | Judgement calls |
| **Sentinel + execution** (Python) | Same | Every 5 min in US hours; at the open | Crash and drawdown rules (automatic sells); executes approved rebalances; keeps GTC stops at IBKR | Buy anything without approval |
| **Committee** (Claude agents) | Claude scheduled tasks, linked to the computer | Weekly + daily brief + event-triggered mid-week review | Macro, sector, fundamental and technical research with web search; strategy; portfolio; risk review; memo | Place orders, change limits, invent numbers |

Agents reach the broker only **through files**: the Python side writes the research pack, the agents read it and write a proposal, and only the Python side talks to IBKR. That boundary is deliberate.

### Tools

| Need | Choice | Cost |
|---|---|---|
| Broker / execution | IBKR paper via IB Gateway, `ib_async` | $0 |
| Daily prices | IBKR historical `ADJUSTED_LAST` (works on delayed data) | $0 |
| Fundamentals | SEC EDGAR companyfacts API (point-in-time via filing dates) | $0 |
| Filings | SEC EDGAR 8-K / 10-Q / 10-K / late-filing notices, red-flag items | $0 |
| Macro | FRED (12 series: rates, curve, HY spread, VIX, claims, unemployment, CPI, NFCI, oil, USD) | $0 |
| Universe | S&P 500 constituents, 11 GICS sectors | $0 |
| News / earnings / events | Web search inside the agents | included |
| Always-on host | Now: a Mac with launchd. Later: a VPS (Docker template included) | $0 → ~$10/mo |

## 5. The agent workflow

```
                                   ┌─────────── Python (deterministic) ─────────────┐
 After every US close              │ data → mark NAV → stops/ladder → screen(top-40)│
                                   │ → regime & caps → research pack → shadow book   │
                                   └───────────────────────┬─────────────────────────┘
 Weekly — Investment Committee (Claude)                    ▼
   ┌───────────── independent, parallel ─────────────┐
   │ Macro Strategist      Sector Analyst             │
   │ Fundamental Analyst   Technical Analyst          │
   └──────────────────────┬───────────────────────────┘
                 Portfolio Strategist (exposure, tilts, theme caps)
                          ▼
                 Portfolio Manager (names & weights vs shadow)
                          ▼
                 Risk Manager (+ code check_proposal.py) ── 1 revision ──┐
                          ▼                                              │
                 Fund Manager / CIO → committee memo + target_portfolio ◄┘
                          ▼
 Human: approve in the dashboard or scripts/approve.py   → execute at next US open
 US hours — Sentinel every 5 min (automatic de-risking, no approval)
 Daily — Risk Officer brief (Claude): health, P&L, alerts, holdings news, ESCALATE
```

Design choices that matter:
- **Independence:** the four analysts run as separate subagents and never see each other's work. That reduces herding, the main failure mode of committee-style prompting.
- **Bounded discretion:** agents may only pick from the quant top-40, within code-enforced weights. A good story can't pull in a stock the screen rejected.
- **Bias to inaction:** "hold" is a first-class decision. Turnover ≤ 40%/month.
- **Asymmetric authority:** risk *reductions* happen automatically in code. Risk *increases* need human approval.
- **Audit trail:** every week leaves `pack → reports → proposal → checker output → memo → approval → execution` in `research/<date>/`.

### Risk defence layers (all code)

| Layer | Trigger | Action |
|---|---|---|
| Regime cap (daily) | Equal-weight index 10-month trend, breadth, HY credit spread, VIX | Max gross 100% / 70% / 30% |
| Drawdown ladder (daily + intraday) | Fund −8% / −12% / −15% from high-water mark | Max gross 75% / 50% / **0% and halt** |
| Position stop (close) | −15% from entry, −25% from peak once in profit | Exit at next open |
| Server GTC stop (IBKR) | −20% from entry | Fires even if the computer is off; booked the next morning |
| Rank-buffer exit (weekly) | Screen rank worse than 60 on 2 consecutive reviews | Sold at next open |
| Sentinel shock | Market −3% intraday or VIX ≥ 30 | Freeze new buys 4 days, alert |
| Sentinel crash | Market −5% day / −8% 3-day / VIX ≥ 40 | Cut gross to 50% immediately |
| Kill switch | File `STOP_TRADING` | Halt; `liquidate.py` goes to cash |

After a halt the fund stays in cash until a human runs `reset_hwm.py`, like a fund's max-loss mandate.

## 6. Pre-committed success and kill criteria

These are set in advance so results can't move the goalposts:

- **After 13 weeks:** the pipeline ran on ≥ 90% of scheduled days with no unreconciled ledger breaks. If not, fix operations before judging performance.
- **After 26 weeks:** committee NAV vs shadow quant NAV, both after costs. If the committee is **behind the shadow**, drop LLM stock selection and keep the LLMs for research and risk commentary only.
- **Any time:** a breach of −20% means the risk system failed. Stop, run a post-mortem, and don't restart until it's explained.
- **Real money:** not before 12 months of paper results that meet the criteria above, and only if the quant core backtest (survivorship caveat included) shows positive risk-adjusted returns vs equal-weight.

## Sources

- LLM trading frameworks and look-ahead bias: [TradingAgents (arXiv 2412.20138)](https://arxiv.org/pdf/2412.20138), [Look-Ahead-Bench (arXiv 2601.13770)](https://arxiv.org/pdf/2601.13770), [Expert Investment Teams (arXiv 2602.23330)](https://arxiv.org/pdf/2602.23330)
- S&P 500 monthly history (Shiller): [datasets/s-and-p-500](https://github.com/datasets/s-and-p-500)
- IBKR fractional shares: [IBKR Knowledge Base](https://ibkb.interactivebrokers.com/fr/node/3416)
