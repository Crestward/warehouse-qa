# warehouse-qa

A permission-aware, lineage-aware "ask your warehouse" assistant. Retrieval runs over a metadata graph rather than text chunks, access policies are enforced in the database rather than the prompt, generated SQL is verified before answering, and the assistant asks or abstains when unsure.

Part of a pair of linked portfolio projects that share [`warehouse-graph`](https://github.com/Crestward/warehouse-graph), a lineage/metadata graph served over MCP. The sibling project is [`pipeline-sentinel`](https://github.com/Crestward/pipeline-sentinel).

> **Status: Phase 5 (retrieval).** The eval set and harness exist (Phase 4); graph-aware retrieval is built and measured. SQL generation and verification come in Phase 6.

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

Full report: [`results/`](results/).

## Retrieval (Phase 5): hybrid vs vector-only on dev
Both retrievers go through the warehouse-graph MCP tools *as the asking user*, so role filtering is the server's job: 800 retrievals across five roles surfaced 0 hidden nodes. Retrieval is scored on retrieved nodes, so the comparison cost $0.

| Dev, n = 49 with gold nodes | Vector-only | **Hybrid** (seed → metric/QueryExample/deprecation expansion → cross-encoder) |
|---|---|---|
| Recall@20 | 0.534 | **0.767** (+0.233 [+0.158, +0.312], paired p = 0.0001) |
| Recall@10 | 0.343 | 0.394 (+0.051, not significant) |
| Planted-trap checks | 14/16 | **16/16** (adds the stale-data warnings) |
| Context size | 2,011 tokens | **1,423 tokens** (schema dump: 2,562) |

**What it showed:**
- Expansion that follows *meaning* helps: metric → model, deprecated → replacement, similar example → its tables. Expansion that follows *lineage structure* doesn't.
- Every role that can only see `marts` was blind to stale upstream data. This was fixed in the graph: each model now carries its upstream freshness, without revealing the hidden source.

Details and negative results: [`docs/design-decisions.md`](docs/design-decisions.md) (Q1–Q3).

## Quick start
```bash
uv sync
make check            # lint, types, unit tests
# with the eval warehouse up (warehouse-graph: make policies, ad_spend stale; docs/eval.md §8):
make eval-check       # gold queries as their roles, labels, contamination (no LLM)
make eval-baseline    # config 1 on dev via the Batch API (~US$0.20, or $0 from the cache)
uv run warehouse-qa eval retrieve --retriever hybrid --split dev   # retrieval only, $0
uv run warehouse-qa eval sweep --split dev                          # re-tune on dev, $0
```

## Licence
MIT. See [LICENSE](LICENSE).
