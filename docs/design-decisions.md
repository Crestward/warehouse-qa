# Design decisions: warehouse-qa

Project-wide decisions (stack, governance layout, graph format) are in [`warehouse-graph/docs/design-decisions.md`](https://github.com/Crestward/warehouse-graph/blob/main/docs/design-decisions.md). These are the P1-specific ones. Each entry gives the context, the choice and why.

## Q1. Retrieval: what was built, what was measured (Phase 5)
**Built.** Two retrievers, both through the five warehouse-graph MCP tools as the asking principal:
- **Retriever 2, vector-only (baseline):** `search_nodes(mode=vector)`, k = 40. Nodes are labelled with the flags each hit carries.
- **Retriever 4, hybrid (main):**
  1. Deterministic query understanding: metrics by embedding margin, BM25 entities, date ranges.
  2. Hybrid seeds (BM25 + vector, RRF).
  3. Expansion: matched metrics → their model and canonical alternative; the best tables → columns, caveats, and their replacement if deprecated; the 3 most similar QueryExamples → the tables and columns their SQL reads (resolved with sqlglot, then re-fetched through MCP, so nothing hidden can slip in).
  4. Rerank with a local cross-encoder (`ms-marco-MiniLM-L-6-v2`, CPU, $0).
  5. Labels: deprecated (with replacement), trap kind, non-canonical metric (with the canonical one), stale upstream data, masked column.
- **Context packer:** token-budgeted. Labelled nodes are kept with their warning, not dropped, and docs text is wrapped in `<data>` blocks.

**Measured on dev** (60 questions; 49 have gold nodes; scored on retrieved nodes, so $0 of LLM calls):

| | Vector-only (2) | Hybrid (4) | Paired difference (hybrid − vector) |
|---|---|---|---|
| Recall@5 | 0.224 | 0.242 | |
| Recall@10 | 0.343 | 0.394 | +0.051 [−0.014, +0.116], p = 0.14 (18 wins, 6 losses) |
| **Recall@20** | 0.534 [0.43, 0.63] | **0.767** [0.70, 0.83] | **+0.233 [+0.158, +0.312], p = 0.0001** (30 wins, 2 losses) |
| Retrieval trap check | 14/16 (misses both stale-data traps) | **16/16** | |
| Stale warning: false alarms / misses | 0% / 100% | 3.6% / 0% | |
| Context size (tokens) | 2,011 | **1,423** | config 1's schema dump: 2,562 |
| Tool calls / latency p50 | 20 / 0.02 s | 24 / 0.58 s | |
| Hidden nodes surfaced | 0 | 0 | role-filter test: 800 retrievals, 0 violations |

The paired test is a bootstrap 95% CI on the per-question recall difference, with a two-sided sign-flip permutation p (`eval compare-retrieval`).

**Gate (task 5.6/5.7): passed.** Hybrid beats vector-only on dev recall@20 with a paired test. The honest qualifier is that at recall@10 the gain is small and not significant. Hybrid's advantage is breadth: it reliably gets the right *table plus its columns, metric and caveats* into the top 20, where vector-only finds the table but misses half its columns.

**Negative results (kept, not hidden):**
- **Lineage hops didn't help** (hops = 1 scored below hops = 0). Neighbours of the right table are mostly other tables a question doesn't need. Expansion that follows *meaning* (metric → model, deprecated → replacement, QueryExample → tables) helped; expansion that follows *structure* didn't.
- **Letting columns inherit their table's score (`w_parent`) didn't help** either. The cross-encoder already ranks a well-described column of the right table highly.
- **Neither retriever beats the other on the planted traps except staleness.** Every trap node in this warehouse carries its flag on the node itself, so even a plain vector search hands it over labelled. The traps that need the *graph* are the ones whose evidence lives elsewhere: stale upstream data (found via D16 below).

**What it means for Phase 6:** retrieval is not the bottleneck on accuracy. Config 1 already scores 96% with the whole schema in context, which on this warehouse is only ~2,600 tokens. The main system has to earn its keep elsewhere:
- 44% less context than the schema dump
- warnings the schema dump can't carry: staleness, caveats, canonical alternatives
- the verification loop

That's what the Phase 6 configs 4 vs 6 comparison has to show.

**Tuning hygiene.** Tuned on dev only, over 162 combinations (`docs/retrieval-tuning.md`, chosen params in `configs/retriever_hybrid.yaml`). The test split is untouched until Phase 8. Ties in recall were broken by trap checks, then by staleness-warning errors.

## Q2. Freshness reaches marts-only roles through the models (Phase 5)
The first retrieval eval scored **0% on stale-source traps for both retrievers**. The cause: roles that only see `marts` have no source nodes in their filtered graph, so no retriever acting as them can learn that `ad_spend` is late. That's exactly PRD scenario 5, failing for the users it describes.

**Fix:** warehouse-graph now propagates each model's worst upstream freshness into the model itself, as a note that names no source (warehouse-graph D16). The hybrid retriever reads it from the model record, which is cheaper too: there's no lineage walk per question.

**Rejected alternatives:** querying lineage as a privileged service account during the request, which would put a second identity inside the request path, and showing source nodes to everyone.

**Scoring fix that came with it:** a staleness warning is judged against whether the question's gold SQL reads stale-derived data, not against the trap label alone. Campaign questions labelled "regular" really are stale, and warning on them is correct.

## Q3. Retrieval is scored without an LLM
Recall@k is computed against the derived gold nodes (Phase 4), and trap checks against labels and warnings, so the whole Phase 5 sweep and comparison cost $0 (PRD §5.2 levers 5–6). The Haiku fallback for query understanding exists and is unit-tested. It never triggered on dev (0/60), because every question matched graph nodes deterministically.

## Q4. SQL generation and verification: results and what they mean (Phase 6)
**Built** (`pipeline/`):
- Generation with structured output (`{sql, tables_used, assumptions}`) from the packed context. The system prompt is stable and cached.
- Static checks: one read-only query, `marts` tables only, function allow-list, LIMIT injection.
- Dry run (EXPLAIN) and execution as the asker's own login role.
- Exactly five sanity checks: empty result, join fan-out (probed with a COUNT vs COUNT DISTINCT of the grain key), null blow-up, deprecated usage, staleness.
- Up to 2 repair rounds. Each round is one Batch API call, and the model gets the errors, check results and **result stats only, never rows**.
- An answer composer (result, SQL, tables, caveats of the tables used, freshness warnings) and an audit event per answer.

**Iteration on dev (allowed for the main system only; logged):** the first pass scored 81.6% execution accuracy, with 8.2% over-refusal. Reading every failure gave three general fixes:
1. **The model was acting as the gatekeeper.** It refused finance's salary question because a caveat said "restricted", and refused a masked column. The prompt now says what D13 means: access is enforced by the database for this user, and labels are never a reason to refuse. Over-refusal fell from 8.2% to 2.0%.
2. **Fan-out false positive.** A deliberate `CROSS JOIN` ("count each refund once per category") was flagged three times and ended in an abstain. Cartesian joins without a join condition are now exempt. The gold-SQL oracle test proves that no gold query triggers a repair.
3. **Comparison:** an all-zero group ("Prospect: 0 churned") may be present or absent (docs/eval.md change log). All runs were re-scored offline for free.

**Final dev results** (n = 60; 49 answerable; 16 traps; Sonnet 5.5, low effort, Batch API):

| | Config 1, schema dump | Config 6, no verification | **Config 4, main** | Oracle retrieval |
|---|---|---|---|---|
| Execution accuracy | 95.9% | 91.8% | **91.8%** [83.7, 98.0] | 95.9% |
| Trap avoidance | 14/16 | 12/16 | **14/16** | 16/16 |
| Over-refusal | 2.0% | 2.0% | 2.0% | 0% |
| Leakage | 0 | 0 | 0 | 0 |
| Context given to the model | ~2,560 tokens (whole schema) | 1,423 | 1,423 | 211 |
| Cost per question (nominal) | $0.0031 | $0.0038 | $0.0041 | $0.0019 |

**Verification (config 4 vs 6, the Phase 6 gate):**
- Same accuracy (45/49 each, no discordant pairs).
- **Trap failures halve, 4 → 2.** It wins exactly the two stale-data questions (q098, q099) through the staleness check, with 0 losses. Exact McNemar p = 0.5: on 16 trap questions a 2–0 split can't be significant. That's reported as is.
- It also turned q086 (an AU analyst asking for NZ revenue) from a row-filtered NULL presented as an answer into an abstain ("you likely can't see NZ rows").
- It fixed two genuine fan-outs by repair (q012, q075).
- It costs 3 extra calls in 60 questions.

**Main system vs baseline 1:**
- **Not more accurate on this warehouse:** 91.8% vs 95.9%, McNemar p = 0.63 (1 win, 3 losses).
- **Where the losses come from:** one is a pure retrieval miss (q115: the payments table never reached the context). Two are model choices: gross instead of net revenue on q106, and a slightly different filter on q045.
- **Retrieval is the gap:** oracle retrieval brings config 4 back to 95.9%.
- **What config 4 does offer:**
  - 44% less context
  - caveats and freshness in every answer
  - the stale warnings (trap wins the baseline can't get)
  - an honest abstain on q086, where the baseline returned the NULL
  - the caveat-dependent q067 (salary bill in NZD), which the baseline refused

**What it means:** on a 20-table warehouse, "put the whole schema in the prompt" is a strong, cheap baseline, and this project shows that rather than hiding it. The graph's value is everything around the SQL (governance-aware context, warnings, verification, refusals), and context size, which matters as warehouses grow past what fits in a prompt. The stretch cascade (S1) and Phase 7's Gate A/B work on the mode and refusal side, where the main system's edge is.

**Cost:** the whole of Phase 6's iteration (two passes of configs 4, 6 and oracle on dev) billed US$0.75. Config 6's first round is byte-identical to config 4's round 0, so the response cache served it for $0. Project total so far: US$0.99.
