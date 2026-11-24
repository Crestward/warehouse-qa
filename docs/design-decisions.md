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
