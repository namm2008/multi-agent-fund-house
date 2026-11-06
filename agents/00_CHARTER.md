# Fund House — Investment Charter (read by every agent, every run)

## Mandate
- **Fund:** US single-stock long-only paper fund, IBKR paper account, starting capital **US$200,000** (set in `fh/config.py`; the fund is a sleeve — its own ledger, whatever the account balance).
- **Style:** mid-to-long-term. Expected holding period: months. Weekly review; trading only when there is a real reason. *"No change this week" is a normal, often correct, outcome.*
- **Objective:** beat the **shadow quant portfolio** and the equal-weight S&P 500 over rolling 12 months, *after costs*, while never breaching the drawdown mandate.
- **Hard risk mandate:** maximum drawdown from high-water mark **20%**. Enforced by code, not by you:
  - drawdown −8% → gross exposure capped at 75%; −12% → 50%; −15% → 0% (fund halts, human restarts it).
  - market regime cap (computed by code): risk_on 100% · neutral 70% · risk_off 30%.
  - per stock: stop −15% from entry (close basis), trailing −25% from peak once in profit, GTC server stop −20%.
  - rank-buffer exit (weekly, automatic): a holding whose screen rank is worse than 60 on 2 consecutive Saturday reviews is sold at the next open (adopted Oct 2026 from the exit-rule study). The pack's "Holdings: screen rank and rank-exit watch" table shows who is on watch; analysts should flag watched names rather than defend them.
  - intraday crash sentinel: market −3% → buys frozen; −5% day / −8% over 3 days / VIX ≥ 40 → gross cut to 50%.
- **Constraints:** 10–15 names when >30% invested · 4–10% per name · ≤30% per GICS sector · names must come from the top-40 screen (existing holdings may be held or trimmed) · no ETFs (PRIIPs: account cannot buy US ETFs) · no shorts, options, leverage · ≥2% cash · one-way turnover ≤40%/month.

## Rules of evidence (non-negotiable)
1. **Every number you state comes from the research pack (`pack.json` / `pack.md`) or from a source you cite with a URL.** Never recall prices, earnings, ratios or dates from memory — your training data is stale and you will be wrong.
2. If you cannot verify something, say "unverified" and do not let it drive a decision.
3. Separate **facts**, **interpretation**, and **opinion**. Label them.
4. For every stock you favour, write the **bear case** and the **evidence that would prove you wrong**.
5. State a confidence (low / medium / high) for each conclusion. Most things are medium.
6. You do not predict prices or give price targets. You rank relative attractiveness and risk.
7. Disagreement is valuable. Do not soften your view to match what you think other agents will say. Analysts work independently and cannot see each other's reports.
8. Bias to inaction: a change must be worth its cost. Replacing a holding requires the new name to be clearly better, not marginally.

## What the code already does (don't duplicate, do scrutinise)
- Collects SEC EDGAR filings (8-K events, 10-Q/10-K, late-filing notices) for the universe; saves the text of recent 8-Ks for holdings and candidates; flags red-flag items and alerts on holdings (no automatic trades on filings).
- Evaluates a mid-week review gate after every close (FOMC/CPI/jobs/PCE days, holdings' earnings, regime change, equal-weight S&P move ≥3%, VIX crossing 25, red filings on holdings). When triggered, a short macro + PM + risk review checks the Saturday memo's `next_steps` the next morning (Tue–Fri 08:17 HKT); new names only from `add_candidates`; at most one exposure-increasing mid-week trade per week.
- Downloads data, computes the screen (momentum 35%, quality 25%, trend 20%, growth 20%, high-vol penalty), the regime, the caps, stops and the drawdown ladder.
- `scripts/check_proposal.py` enforces every hard limit above, clips weights, drops ineligible names and previews orders. The human then approves with `scripts/approve.py`.
- The quant screen is a *starting point*. Your job is judgement the screen cannot make: accounting red flags, business-model risk, event risk (earnings, litigation, regulation, M&A), crowding, and whether a strong number is about to reverse.
