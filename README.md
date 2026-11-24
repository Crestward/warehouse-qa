# warehouse-qa

A permission-aware, lineage-aware "ask your warehouse" assistant. Retrieval runs over a metadata graph rather than text chunks, access policies are enforced in the database rather than the prompt, generated SQL is verified before answering, and the assistant asks or abstains when unsure.

Part of a pair of linked portfolio projects that share [`warehouse-graph`](https://github.com/Crestward/warehouse-graph), a lineage/metadata graph served over MCP. The sibling project is [`pipeline-sentinel`](https://github.com/Crestward/pipeline-sentinel).

> **Status: Phase 4 (eval set and harness, built before the system).** The measuring stick exists; the system that gets measured is built in Phases 5–7.

## How it will be measured
Everything is in [`docs/eval.md`](docs/eval.md):
- **150 labelled questions** in [`evals/questions.yaml`](evals/questions.yaml): 90 regular, 40 aimed at the warehouse's 13 planted traps, and 20 that should be clarified or abstained on. They're spread across five roles.
- **Gold SQL is executed as the question's role**, so an Australian analyst's gold answer is already Australia-only and masked. Questions a role isn't permitted to answer are labelled `refuse`.
- **50 red-team prompts** in [`evals/redteam.yaml`](evals/redteam.yaml): direct asks, small-cell inference, prompt injection (including three injections planted in the dbt docs), role confusion, and SQL escalation.
- **Frozen dev/test splits** (60/90 plus a 30-question dev-mini), stratified and committed with hashes. There's a contamination check against the QueryExamples: 8 questions were rewritten, and every other hit was reviewed by hand.
- **Deterministic metrics**, no LLM-as-judge:
  - execution accuracy (multiset result comparison)
  - mode accuracy
  - recall@k and trap avoidance
  - leakage (a scanner for hidden values, out-of-scope rows, existence confirmation and small cells)
  - over-refusal, cost and latency
  - 95% bootstrap CIs and exact McNemar tests
- **A harness that is cheap to re-use:** Batch API, response cache, one global cost ledger with a US$150 cap, and `eval rescore`, which recomputes every metric from the saved JSONL with no database and no LLM.

## First result: config 1 (schema dump) on dev
The naive baseline puts the role's whole visible schema in context and makes one Sonnet 5.5 call per question, with no retrieval, no verification and no gates.

| Metric (dev, n = 60) | Baseline 1 |
|---|---|
| Execution accuracy | **95.9%** [89.8, 100] (47/49) |
| Trap avoidance | 87.5% [68.8, 100]: misses both stale-data warnings |
| Mode accuracy, lenient | 90.0%: it can't ask a clarifying question (it answered 3 of the 4 ambiguous questions and abstained on the 4th); all 4 out-of-scope questions were correctly abstained on |
| Leakage | **0 / 60** |
| Cost | US$0.19 nominal (Batch API, cached schema prefix), $0.003 per question |

**What this says about the plan:** on a 20-table warehouse the whole schema fits in context, and a strong model avoids most planted traps on its own. Graph-aware retrieval therefore has little room to win on raw accuracy here. Its measurable edge has to come from:
- the warnings and caveats a schema dump doesn't carry: stale sources, the refunds data gap, and the fixed FX rate (the one regular question it refused, q067)
- clarifying instead of guessing
- refusing correctly instead of returning a row-filtered NULL as an answer
- cost per question

Phase 5 tests that claim on recall, and records the result whichever way it goes. Full report: [`results/`](results/).

## Quick start
```bash
uv sync
make check            # lint, types, unit tests
# with the eval warehouse up (warehouse-graph: make policies, ad_spend stale; docs/eval.md §8):
make eval-check       # gold queries as their roles, labels, contamination (no LLM)
make eval-baseline    # config 1 on dev via the Batch API (~US$0.20, or $0 from the cache)
```

## Licence
MIT. See [LICENSE](LICENSE).
