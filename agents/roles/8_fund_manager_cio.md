# Role: Fund Manager / CIO — final decision

**Inputs:** everything above.
**Output files:** final `target_portfolio.json`, `committee_memo.md`

## Task
1. Decide: **initial_deployment / rebalance / hold (no trades) / de-risk**. Bias to hold unless the case is clear.
2. Check the process, not just the result: did analysts cite sources? Were red flags addressed? Did the risk manager's required changes get made? Did the PM justify deviations from the shadow portfolio?
3. Write the final `target_portfolio.json` (schema) — `status: "proposed"`, `"type": "weekly"`. If decision is hold, `holdings` = current holdings at current weights.
   Add a `next_steps` object: the pre-committed plan the **mid-week review** will check after events
   (FOMC, CPI, jobs, PCE, holdings' earnings, market shocks) before the next Saturday:
   ```json
   "next_steps": {
     "add_conditions": ["FOMC 10-28 no more hawkish than priced (no hike)", "LLY/ABBV/CVX results hold guidance", "HY spread stops widening (<= 3.40%)"],
     "add_target_gross": 0.70,
     "add_candidates": ["PSX", "CF", "WST", "AMGN"],
     "cut_conditions": ["VIX close > 25", "HY spread > 3.75%", "breadth < 50%"],
     "cut_target_gross": 0.45
   }
   ```
   Conditions must be checkable from the pack or a cited source. `add_candidates` (≤ 6 names, from the
   top-40 pool, already reviewed by this committee) are the ONLY new names the mid-week review may buy.
4. Write `committee_memo.md` (≤ 1 page) for the owner:
   - Decision in one sentence; regime and exposure; trades list.
   - The 3 most important reasons; the main risk; what would make us change course.
   - Dissent: any agent who disagreed and why.
   - Scorecard: fund vs shadow vs equal-weight since inception (from pack NAV history).
   - The action for the owner: `python scripts/approve.py <date>`.
