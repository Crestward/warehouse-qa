"""P1 retrievers (tasks 5.2-5.3). Both go through the MCP tools as the asking principal.

Retriever 2, vector-only (the baseline):
    search_nodes(question, mode=vector, k) -> ranked nodes, labelled with the flags each hit
    carries (deprecated, trap kind). No graph walking, so no freshness warnings.

Retriever 4, hybrid (the main system, PRD §3.3 step 2):
    1. understand: metrics by embedding margin, entities, time range (understanding.py)
    2. seed: search_nodes (BM25 + vector, RRF) over node text
    3. expand: matched metrics -> their model and canonical alternative; top models -> their
       columns, caveats and replacement (if deprecated) and lineage neighbours within `hops`;
       similar QueryExamples -> the tables and columns their SQL reads; every model's
       upstream freshness (propagated into the model by warehouse-graph, D16)
    4. rerank: cross-encoder relevance + seed score - hop penalty
    5. label: deprecated (with replaced_by), trap kind, non-canonical metric (with the
       canonical one), stale upstream source, masked column. Labelled nodes stay in the list
       (PRD §3.3, task 5.4): the model must see why it avoided them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from warehouse_qa.eval.labels import SqlNodeResolver
from warehouse_qa.retrieval.rerank import Reranker
from warehouse_qa.retrieval.tools import GraphTools
from warehouse_qa.retrieval.understanding import Understanding, understand

STALE_STATUSES = ("warn", "error", "runtime error")


@dataclass
class Retrieved:
    nodes: list[str]  # ranked
    labels: dict[str, list[str]] = field(default_factory=dict)  # node id -> warning labels
    warnings: list[str] = field(default_factory=list)  # answer-level, e.g. stale sources
    summaries: dict[str, dict[str, Any]] = field(default_factory=dict)  # id -> NodeSummary
    understanding: Understanding | None = None

    @property
    def labelled(self) -> list[str]:
        return [n for n in self.nodes if self.labels.get(n)]

    @property
    def caveats(self) -> list[str]:
        return [n for n in self.nodes if n.startswith("caveat.")]


def _flag_labels(summary: dict[str, Any]) -> list[str]:
    labels = []
    if summary.get("deprecated"):
        labels.append("deprecated")
    if summary.get("trap_kind"):
        labels.append(f"trap: {summary['trap_kind']}")
    return labels


class VectorRetriever:
    name = "vector"

    def __init__(self, k: int = 40) -> None:
        self.k = k

    async def retrieve(self, question: str, tools: GraphTools) -> Retrieved:
        hits = await tools.search(question, k=self.k, mode="vector")
        out = Retrieved(nodes=[h["id"] for h in hits])
        for h in hits:
            out.summaries[h["id"]] = h
            if labels := _flag_labels(h):
                out.labels[h["id"]] = labels
        return out


@dataclass(frozen=True)
class HybridParams:
    k_seed: int = 20
    hops: int = 1
    models_expanded: int = 6
    n_examples: int = 3
    w_seed: float = 0.5
    w_hop: float = 0.1
    w_parent: float = 0.5  # a column inherits this share of its table's score
    k_out: int = 40
    warn_models: int = 3  # stale warnings come from the top-N ranked tables only


@dataclass
class _Candidate:
    id: str
    seed: float = 0.0  # 1/rank from search, or a bonus for understood metrics
    hop: int = 9

    def touch(self, seed: float = 0.0, hop: int = 9) -> None:
        self.seed = max(self.seed, seed)
        self.hop = min(self.hop, hop)


@dataclass
class Pool:
    """Everything gathered for one question, before weighting."""

    understanding: Understanding
    cands: dict[str, _Candidate]
    summaries: dict[str, dict[str, Any]]
    labels: dict[str, list[str]]
    parent_of: dict[str, str]
    stale_by_model: dict[str, dict[str, str]]
    ce: dict[str, float]


def _note_freshness(
    model_id: str,
    node: dict[str, Any],
    stale_by_model: dict[str, dict[str, str]],
    labels: dict[str, list[str]],
) -> None:
    """Models carry their upstream sources' worst freshness (warehouse-graph D16), so this works
    for roles that can't see sources at all."""
    status = node.get("upstream_freshness")
    if status in STALE_STATUSES:
        note = node.get("upstream_freshness_note") or status
        stale_by_model[model_id] = {model_id: f"{note} ({status})"}
        label = f"stale data: {note}"
        if label not in labels.setdefault(model_id, []):
            labels[model_id].append(label)


def _text(summary: dict[str, Any], parent_name: str | None = None) -> str:
    kind = str(summary.get("type", "")).replace("_", " ")
    name = summary.get("name", "")
    if parent_name:
        name = f"{parent_name}.{name}"
    return f"{kind} {name}: {summary.get('description') or ''}"[:600]


class HybridRetriever:
    name = "hybrid"

    def __init__(
        self,
        reranker: Reranker,
        resolver: SqlNodeResolver,
        params: HybridParams | None = None,
    ) -> None:
        self.reranker = reranker
        self.resolver = resolver
        self.params = params or HybridParams()

    async def retrieve(self, question: str, tools: GraphTools) -> Retrieved:
        return self.rank(await self.gather(question, tools))

    async def gather(self, question: str, tools: GraphTools) -> Pool:
        """Steps 1-3 plus cross-encoder scores: the expensive part, independent of weights."""
        p = self.params
        u = await understand(question, tools)
        cands: dict[str, _Candidate] = {}
        summaries: dict[str, dict[str, Any]] = {}
        labels: dict[str, list[str]] = {}
        parent_of: dict[str, str] = {}

        def add(summary: dict[str, Any], seed: float = 0.0, hop: int = 9) -> None:
            nid = summary["id"]
            summaries.setdefault(nid, summary)
            cands.setdefault(nid, _Candidate(nid)).touch(seed, hop)
            for label in _flag_labels(summary):
                labels.setdefault(nid, [])
                if label not in labels[nid]:
                    labels[nid].append(label)

        # 2. seeds
        for rank, h in enumerate(await tools.search(u.search_text, k=p.k_seed), 1):
            add(h, seed=1.0 / rank, hop=0)

        # 3a. understood metrics -> model, canonical alternative
        for rank, (metric_id, _) in enumerate(u.metrics[:2], 1):
            rec = await tools.metric(metric_id)
            if rec is None:
                continue
            m = rec["metric"]
            add(
                {
                    "id": metric_id,
                    "type": "metric",
                    "name": m["name"],
                    "description": m["description"],
                },
                seed=1.0 / rank,
                hop=0,
            )
            for c in rec["caveats"]:
                add(
                    {
                        "id": c["id"],
                        "type": "caveat",
                        "name": c["name"],
                        "description": c["description"],
                    },
                    hop=1,
                )
            if alt := rec.get("canonical_alternative"):
                labels.setdefault(metric_id, []).append(f"non-canonical; use {alt}")
                alt_rec = await tools.metric(alt)
                if alt_rec:
                    am = alt_rec["metric"]
                    add(
                        {
                            "id": alt,
                            "type": "metric",
                            "name": am["name"],
                            "description": am["description"],
                        },
                        seed=1.0 / rank,
                        hop=1,
                    )
                    if alt_rec.get("model"):
                        cands.setdefault(alt_rec["model"], _Candidate(alt_rec["model"])).touch(
                            hop=1
                        )
            if rec.get("model"):
                cands.setdefault(rec["model"], _Candidate(rec["model"])).touch(hop=1)

        # 3b. similar query examples -> the tables and columns they read
        for rank, h in enumerate(
            await tools.search(question, k=p.n_examples, types=["query_example"]), 1
        ):
            add(h, seed=0.5 / rank, hop=0)
            rec = await tools.node(h["id"])
            if rec is None:
                continue
            for nid in sorted(self.resolver.nodes(rec["node"]["sql"])):
                cands.setdefault(nid, _Candidate(nid)).touch(hop=1)

        # Columns found by search pull in their table.
        for nid in list(cands):
            if nid.startswith("column."):
                rec = await tools.node(nid)
                if rec is not None:
                    parent_of[nid] = rec["node"]["parent"]
                    cands.setdefault(
                        rec["node"]["parent"], _Candidate(rec["node"]["parent"])
                    ).touch(hop=1)

        # 3c. expand the best models
        models = sorted(
            (c for c in cands.values() if c.id.startswith(("model.", "source."))),
            key=lambda c: (-c.seed, c.hop, c.id),
        )[: p.models_expanded]
        stale_by_model: dict[str, dict[str, str]] = {}  # model -> {stale source: status}
        for c in models:
            rec = await tools.node(c.id)
            if rec is None:
                cands.pop(c.id, None)  # resolved from SQL but not visible to this principal
                continue
            node = rec["node"]
            _note_freshness(c.id, node, stale_by_model, labels)
            add(
                {
                    "id": c.id,
                    "type": node["type"],
                    "name": node["name"],
                    "description": node["description"],
                    "deprecated": node.get("deprecated"),
                    "trap_kind": (node.get("trap") or {}).get("kind"),
                },
                hop=c.hop,
            )
            if node.get("deprecated") and node.get("replaced_by"):
                labels.setdefault(c.id, []).append(f"use {node['replaced_by']} instead")
                cands.setdefault(node["replaced_by"], _Candidate(node["replaced_by"])).touch(
                    hop=c.hop + 1
                )
            for col in rec["columns"]:
                add(col, hop=c.hop + 1)
                parent_of[col["id"]] = c.id
            for cav in rec["caveats"]:
                add(
                    {
                        "id": cav["id"],
                        "type": "caveat",
                        "name": cav["name"],
                        "description": cav["description"],
                    },
                    hop=c.hop + 1,
                )
            if p.hops:
                sub = await tools.lineage(c.id, "both", p.hops, "model")
                for n in (sub or {}).get("nodes", []):
                    if n["id"] != c.id:
                        add(n, hop=c.hop + 1)
        # Anything still without a summary: fetch it (e.g. a canonical metric's model).
        for nid in list(cands):
            if nid not in summaries:
                rec = await tools.node(nid)
                if rec is None:
                    cands.pop(nid)
                    continue
                node = rec["node"]
                _note_freshness(nid, node, stale_by_model, labels)
                add(
                    {
                        "id": nid,
                        "type": node["type"],
                        "name": node["name"],
                        "description": node["description"],
                        "deprecated": node.get("deprecated"),
                        "trap_kind": (node.get("trap") or {}).get("kind"),
                    }
                )

        ids = sorted(cands)
        texts = [
            _text(summaries[i], summaries.get(parent_of.get(i, ""), {}).get("name")) for i in ids
        ]
        ce = dict(zip(ids, self.reranker.scores(question, texts), strict=True))
        return Pool(u, cands, summaries, labels, parent_of, stale_by_model, ce)

    def rank(self, pool: Pool, params: HybridParams | None = None) -> Retrieved:
        """Step 4-5: weights -> ranked, labelled nodes. Cheap, so sweeps re-rank one pool."""
        p = params or self.params
        base = {
            i: pool.ce[i] + p.w_seed * c.seed - p.w_hop * min(c.hop, 3)
            for i, c in pool.cands.items()
        }
        score = {
            i: s + p.w_parent * base.get(pool.parent_of.get(i, ""), 0.0) for i, s in base.items()
        }
        ranked = sorted(score, key=lambda i: (-score[i], i))[: p.k_out]
        # Answer-level staleness: only from the tables the answer will most likely use.
        top_models = [i for i in ranked if i.startswith("model.")][: p.warn_models]
        stale = {s: st for m in top_models for s, st in pool.stale_by_model.get(m, {}).items()}
        return Retrieved(
            nodes=ranked,
            labels={i: pool.labels[i] for i in ranked if pool.labels.get(i)},
            warnings=[f"stale: {m}: {detail}" for m, detail in sorted(stale.items())],
            summaries={i: pool.summaries[i] for i in ranked},
            understanding=pool.understanding,
        )
