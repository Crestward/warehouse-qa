# Label review (after the test run)

The protocol planned a spot-check of 15 questions before the test run. It was skipped, and done on 2026-11-24, **after** the results were known. So the labels stay frozen, the headline numbers keep them, and this page reports how much the questionable ones matter.

## The spot-check
**Method:** 15 questions drawn at random with a fixed seed (6 regular with SQL, 2 refusals, 4 traps, 3 clarify/abstain). For the 10 with a gold query, the reviewer wrote SQL from the question text alone, by a different route where there was one (for example fct_orders minus fct_refunds instead of fct_revenue_daily), ran it as the question's role, and only then compared it with the gold result.

**Reviewer:** the same AI assistant that drafted the questions, at the repo owner's request. It catches mistakes, but it isn't an independent check. Some of the failing questions had also been read in the error analysis before the draw.

| Outcome | Questions |
|---|---|
| Agrees (12) | q002, q005, q031, q067, q098, q100, q114 (same result); q083, q090 (the database refuses the column); q141, q142, q145 (not answerable from the warehouse) |
| More than one right answer (3) | **q021**: names don't identify products. **q027**: revenue-order filter not stated. **q106** (dev): "revenue per order" is also the canonical average order value |

**3 of the 10 questions with SQL are ambiguous.** Too small a sample for a rate, but it isn't rare. So every gold query was then scanned for the same three defects by its wording (not by which config got it right): products grouped by name, a revenue-order filter that the question doesn't state, and revenue per order. That adds **q026 and q044**. q030 also has an unstated filter, but both readings score the same: the scorer's absolute tolerance is 0.005, which is **half a percentage point for a share**. That rule was fixed in Phase 4 and applies to every config; it's looser for shares than it should be. q078 matches the pattern but isn't a defect: "average order value" is a defined metric.

**Left out on purpose:** q017 and q073 also look arguable, but they were found by reading config 4's failures. Adding alternatives for a system's own failures would favour that system.

## What it changes
- **The order of the systems doesn't change:** schema dump > main system > vector-only, under both sets of labels.
- **The baseline's lead is less certain.** The gap is 6.9 points instead of 8.2, and the paired test goes from p = 0.031 to p = 0.125. One question (q021) is enough to move it, so **the p = 0.031 is fragile**. "The baseline wins" is still the best reading (6 losses, 1 win); "significantly" isn't safe.
- **Oracle retrieval rises most** (90.4% to 95.9%): given the right tables, the model mostly picks the other valid reading of these questions. That supports "retrieval is the gap".
- Every config runs on the same labels, so nothing here favours one by construction.

<!-- generated: warehouse-qa eval label-review -->
### Execution accuracy on test, frozen labels vs accepting the alternatives
| Config | Frozen labels (headline) | Alternatives accepted too | Changed |
|---|---|---|---|
| 1 · schema dump | 94.5% [89, 99] (69/73) | 95.9% [90, 100] (70/73) | q026 |
| 2 · vector-only | 78.1% [68, 88] (57/73) | 80.8% [71, 89] (59/73) | q021, q026 |
| 4 · main system | 86.3% [78, 93] (63/73) | 89.0% [82, 96] (65/73) | q021, q026 |
| 6 · no verification | 84.9% [77, 93] (62/73) | 87.7% [79, 95] (64/73) | q021, q026 |
| oracle retrieval | 90.4% [84, 96] (66/73) | 95.9% [90, 100] (70/73) | q021, q026, q027, q044 |

| Comparison | Only first right | Only second right | p, frozen | p, alternatives |
|---|---|---|---|---|
| 4 · main system vs 1 · schema dump | 1 | 6 | 0.0312 | 0.125 |
| 4 · main system vs 2 · vector-only | 8 | 2 | 0.109 | 0.109 |
| 4 · main system vs 6 · no verification | 1 | 0 | 1 | 1 |
| oracle retrieval vs 4 · main system | 7 | 2 | 0.549 | 0.18 |

### The alternatives
| Question | Defect | Why it is also right |
|---|---|---|
| q021 | products identified by name | 120 products share 91 names, so grouping by name merges different products; the question says products |
| q026 | products identified by name | as q021 |
| q027 | unstated revenue-order filter | the question says order lines in 2025, not lines of completed or returned orders |
| q044 | unstated revenue-order filter | the question says customers with at least one order, of any status |
<!-- end generated -->
