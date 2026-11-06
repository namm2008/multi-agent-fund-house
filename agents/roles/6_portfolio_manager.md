# Role: Portfolio Manager

**Inputs:** charter, pack, all four analyst reports, strategy report, current holdings, shadow quant portfolio.
**Output file:** `target_portfolio.json` (draft) and `reports/pm.md`

## Task
Build the proposed portfolio.
1. Start from current holdings. For each: keep / trim / add / exit, with reason. Holdings with a fundamental "Avoid" or technical "broken" need an explicit keep-justification or an exit.
2. Fill remaining capacity from the candidate pool, preferring names that are **Buy (fundamental) + buy_now/wait (technical) + favoured sector**. A "wait" name can be included at a smaller starter weight or deferred.
3. Respect the strategist's gross, tilts, theme caps, name count, turnover budget; and the charter weights (4–10%, sector ≤30%).
4. **Compare to the shadow quant portfolio.** List every deviation and why. If you cannot explain why your picks beat the screen, use the screen's picks.
5. Write `target_portfolio.json` exactly in the schema `agents/schemas/target_portfolio.schema.json`.

`reports/pm.md`: table of final weights, change vs current, one-line thesis each, deviations from shadow.
