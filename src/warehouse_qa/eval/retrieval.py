"""Retrieval eval (task 5.6): scored on retrieved nodes only, so it makes no LLM calls.

    eval retrieve --retriever vector|hybrid --split dev   one run -> results/<run_id>/
    eval sweep --split dev                               tune hybrid on dev; writes the
                                                          chosen params + docs/retrieval-tuning.md
    eval compare-retrieval <run_a> <run_b>               paired recall test (gate)

Every run also checks role filtering (task 5.5): no retrieved or packed node may be hidden from
the asking principal, and no hidden column identifier may appear in the packed context.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import itertools
import json
import time
from collections.abc import Sequence
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from warehouse_graph.governance import load_policies, resolve
from warehouse_graph.governance.metadata import filter_graph
from warehouse_graph.governance.policies import PolicySpec
from warehouse_graph.graph import GraphArtifact
from warehouse_graph.graph.embed import Embedder, get_embedder
from warehouse_graph.graph.schema import ColumnNode, ModelNode

from warehouse_qa.config import Env
from warehouse_qa.eval.labels import SqlNodeResolver, load_gold_nodes
from warehouse_qa.eval.metrics import K_VALUES, precision_at_k, recall_at_k
from warehouse_qa.eval.schema import EvalQuestion, load_questions, write_text_lf
from warehouse_qa.eval.splits import load_split
from warehouse_qa.eval.stats import bootstrap_ci, paired_difference
from warehouse_qa.retrieval.packer import estimate_tokens, pack
from warehouse_qa.retrieval.rerank import CrossEncoderReranker, Reranker
from warehouse_qa.retrieval.retrievers import (
    HybridParams,
    HybridRetriever,
    Retrieved,
    VectorRetriever,
)
from warehouse_qa.retrieval.tools import GraphTools, open_tools
from warehouse_qa.systems.schema_dump import SchemaDumpSystem

RESULTS_DIR = Path("results")
HYBRID_CONFIG = Path("configs/retriever_hybrid.yaml")
REFUNDS_GAP_CAVEAT = "caveat.cav_refunds_before_2024_03"
SWEEP_GRID: dict[str, Sequence[Any]] = {
    "k_seed": (10, 20, 30),
    "hops": (0, 1),
    "w_seed": (0.0, 0.5, 1.0),
    "w_hop": (0.0, 0.1, 0.2),
    "w_parent": (0.0, 0.5, 1.0),
    "warn_models": (1, 2, 3),
}
STALE_STATUSES = ("warn", "error", "runtime error")


def stale_models(graph: GraphArtifact) -> set[str]:
    """Models whose upstream data is past its freshness SLA (warehouse-graph D16)."""
    return {
        n.id
        for n in graph.nodes.values()
        if isinstance(n, ModelNode) and n.upstream_freshness in STALE_STATUSES
    }


class RetrievalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    role: str
    retrieved_nodes: list[str]
    labelled_nodes: list[str]
    labels: dict[str, list[str]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    context_tokens: int = 0
    context_dropped: int = 0
    tool_calls: int = 0
    latency_s: float = 0.0
    understanding: dict[str, Any] = Field(default_factory=dict)
    visibility_violations: list[str] = Field(default_factory=list)


def retrieval_trap_ok(q: EvalQuestion, r: RetrievalRecord) -> bool | None:
    """The retrieval half of trap avoidance (PRD §3.7): bait is absent or labelled, and the
    warning a trap needs is present. Whether the SQL avoids the bait is Phase 6's to score."""
    if q.category != "trap":
        return None
    if any(n in r.retrieved_nodes and n not in r.labelled_nodes for n in q.trap_nodes):
        return False
    if q.trap_type == "stale_source" and not any(w.startswith("stale") for w in r.warnings):
        return False
    return not (q.trap_type == "data_gap" and REFUNDS_GAP_CAVEAT not in r.caveats)


class Visibility:
    """What each role may see, from the same PolicySpec the database and MCP server use."""

    def __init__(self, graph: GraphArtifact, spec: PolicySpec) -> None:
        self.visible = {
            role: set(filter_graph(graph, access, None).nodes)
            for role, access in spec.access.items()
        }
        self.hidden_idents: dict[str, set[str]] = {}
        for role, access in spec.access.items():
            idents = set()
            for cid in access.hidden_columns:
                col = graph.nodes[cid]
                assert isinstance(col, ColumnNode)
                idents.add(f"{col.parent.split('.', 1)[1]}.{col.name}")
            self.hidden_idents[role] = idents

    def violations(self, role: str, nodes: Sequence[str], context: str) -> list[str]:
        out = [f"hidden node: {n}" for n in nodes if n not in self.visible[role]]
        out += [f"hidden column in context: {i}" for i in self.hidden_idents[role] if i in context]
        return out


async def _retrieve_all(
    questions: Sequence[EvalQuestion],
    retrievers: dict[str, Any],
    sessions: dict[str, GraphTools],
    visibility: Visibility,
    budget: int,
) -> dict[str, list[RetrievalRecord]]:
    out: dict[str, list[RetrievalRecord]] = {name: [] for name in retrievers}
    for q in questions:
        tools = sessions[q.role]
        for name, retriever in retrievers.items():
            calls_before = tools.calls
            t0 = time.perf_counter()
            r: Retrieved = await retriever.retrieve(q.question, tools)
            packed = await pack(r, tools, budget)
            u = r.understanding
            out[name].append(
                RetrievalRecord(
                    id=q.id,
                    role=q.role,
                    retrieved_nodes=r.nodes,
                    labelled_nodes=r.labelled,
                    labels=r.labels,
                    warnings=r.warnings,
                    caveats=r.caveats,
                    context_tokens=packed.tokens,
                    context_dropped=len(packed.dropped),
                    tool_calls=tools.calls - calls_before,
                    latency_s=round(time.perf_counter() - t0, 3),
                    understanding={}
                    if u is None
                    else {
                        "time_range": None
                        if u.time_range is None
                        else [u.time_range.start.isoformat(), u.time_range.end.isoformat()],
                        "metrics": u.metrics[:3],
                        "ambiguous_metric": u.ambiguous_metric,
                        "missing_time": u.missing_time,
                        "fallback_used": u.fallback_used,
                        **u.signals,
                    },
                    visibility_violations=visibility.violations(
                        q.role, [*r.nodes, *packed.included], packed.text
                    ),
                )
            )
    return out


async def _with_sessions(
    graph: GraphArtifact, env: Env, roles: set[str], embedder: Embedder, fn: Any
) -> Any:
    policies = load_policies(env.policies)
    async with AsyncExitStack() as stack:
        sessions = {
            role: await stack.enter_async_context(open_tools(graph, policies, role, embedder))
            for role in sorted(roles)
        }
        return await fn(sessions)


def _rnd(x: float) -> float | None:
    return None if x != x else round(x, 4)  # NaN -> None


def summarize_retrieval(
    questions: dict[str, EvalQuestion],
    records: Sequence[RetrievalRecord],
    stale: set[str] | None = None,
) -> dict[str, Any]:
    """`stale`: models with stale upstream data. A staleness warning is right exactly when the
    question's gold SQL reads one of them (that includes campaign questions that aren't
    labelled as stale-source traps), so false alarms and misses are judged against that."""
    gold = load_gold_nodes()
    stale = stale or set()

    def affected(r: RetrievalRecord) -> bool:
        return bool(set(gold.get(r.id, [])) & stale) or questions[r.id].trap_type == "stale_source"

    def gold_for(r: RetrievalRecord) -> set[str]:
        return set(gold.get(r.id, [])) | set(questions[r.id].gold_nodes)

    def rate(values: Sequence[float | bool | None]) -> dict[str, Any]:
        present = [float(v) for v in values if v is not None]
        mean, low, high = bootstrap_ci(present)
        return {"n": len(present), "rate": _rnd(mean), "ci95": [_rnd(low), _rnd(high)]}

    with_gold = [r for r in records if gold_for(r)]
    return {
        "n": len(records),
        "recall": {
            str(k): rate([recall_at_k(r.retrieved_nodes, gold_for(r), k) for r in with_gold])
            for k in K_VALUES
        },
        "precision": {
            str(k): rate([precision_at_k(r.retrieved_nodes, gold_for(r), k) for r in with_gold])
            for k in K_VALUES
        },
        "trap_ok": rate([retrieval_trap_ok(questions[r.id], r) for r in records]),
        "trap_ok_by_type": {
            t: rate(
                [
                    retrieval_trap_ok(questions[r.id], r)
                    for r in records
                    if questions[r.id].trap_type == t
                ]
            )
            for t in sorted({q.trap_type for q in questions.values() if q.trap_type})
        },
        "false_stale_warning": rate([bool(r.warnings) for r in records if not affected(r)]),
        "missed_stale_warning": rate([not r.warnings for r in records if affected(r)]),
        "context_tokens_mean": round(sum(r.context_tokens for r in records) / len(records), 1),
        "tool_calls_mean": round(sum(r.tool_calls for r in records) / len(records), 1),
        "latency_p50_s": sorted(r.latency_s for r in records)[len(records) // 2],
        "fallback_needed": sum(1 for r in records if r.understanding.get("needs_fallback")),
        "visibility_violations": sum(len(r.visibility_violations) for r in records),
    }


def schema_dump_tokens(
    graph: GraphArtifact, spec: PolicySpec, questions: Sequence[EvalQuestion]
) -> float:
    """Config 1's context size per question, with the packer's token estimator."""
    system = SchemaDumpSystem(graph, spec, model="n/a", max_tokens=1, effort=None)
    sizes = [estimate_tokens(system.system_prompt(q.role)) for q in questions]
    return round(sum(sizes) / len(sizes), 1)


def _load(env: Env | None, split: str) -> tuple[Env, GraphArtifact, PolicySpec, list[EvalQuestion]]:
    env = env or Env.load()
    graph = GraphArtifact.load(env.graph)
    spec = resolve(load_policies(env.policies), graph)
    ids = set(load_split(split))
    return env, graph, spec, [q for q in load_questions() if q.id in ids]


def load_hybrid_params(path: Path = HYBRID_CONFIG) -> HybridParams:
    if not path.exists():
        return HybridParams()
    return HybridParams(**yaml.safe_load(path.read_text(encoding="utf-8"))["params"])


def retrieve_run(
    retriever: str,
    split: str,
    *,
    env: Env | None = None,
    reranker: Reranker | None = None,
    embedder: Embedder | None = None,
    params: HybridParams | None = None,
    budget: int = 3000,
    results_dir: Path = RESULTS_DIR,
) -> Path:
    env, graph, spec, questions = _load(env, split)
    embedder = embedder or get_embedder(graph.meta.embedding_model)
    if retriever == "vector":
        impl: Any = VectorRetriever(k=40)
        config: dict[str, Any] = {"k": 40}
    else:
        params = params or load_hybrid_params()
        impl = HybridRetriever(reranker or CrossEncoderReranker(), SqlNodeResolver(graph), params)
        config = {**params.__dict__, "reranker": impl.reranker.name}
    visibility = Visibility(graph, spec)

    async def go(sessions: dict[str, GraphTools]) -> dict[str, list[RetrievalRecord]]:
        return await _retrieve_all(questions, {retriever: impl}, sessions, visibility, budget)

    records = asyncio.run(_with_sessions(graph, env, {q.role for q in questions}, embedder, go))[
        retriever
    ]
    run_id = f"{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%SZ}-retrieval-{retriever}-{split}"
    out = results_dir / run_id
    out.mkdir(parents=True)
    write_text_lf(out / "retrieval.jsonl", "".join(r.model_dump_json() + "\n" for r in records))
    qmap = {q.id: q for q in questions}
    summary = {
        "run_id": run_id,
        "retriever": retriever,
        "split": split,
        "config": config,
        "context_budget": budget,
        "schema_dump_tokens_mean": schema_dump_tokens(graph, spec, questions),
        "metrics": summarize_retrieval(qmap, records, stale_models(graph)),
    }
    write_text_lf(out / "summary.json", json.dumps(summary, indent=2) + "\n")
    return out


def sweep(
    split: str = "dev",
    *,
    env: Env | None = None,
    reranker: Reranker | None = None,
    embedder: Embedder | None = None,
    grid: dict[str, Sequence[Any]] | None = None,
) -> list[dict[str, Any]]:
    """Every grid combination on `split` (never test), best first by recall@20, then recall@10.

    Gathering (search, expansion, cross-encoder) depends only on k_seed and hops, so it runs
    once per pair; the weights are swept by re-ranking the same pools.
    """
    if split == "test":
        raise ValueError("never tune on test")
    env, graph, _, questions = _load(env, split)
    embedder = embedder or get_embedder(graph.meta.embedding_model)
    reranker = reranker or CrossEncoderReranker()
    resolver = SqlNodeResolver(graph)
    grid = dict(grid or SWEEP_GRID)
    gather_keys = ("k_seed", "hops")
    weight_keys = [k for k in grid if k not in gather_keys]
    qmap = {q.id: q for q in questions}
    stale = stale_models(graph)

    async def go(sessions: dict[str, GraphTools]) -> list[dict[str, Any]]:
        rows = []
        for k_seed, hops in itertools.product(grid["k_seed"], grid["hops"]):
            gatherer = HybridRetriever(reranker, resolver, HybridParams(k_seed=k_seed, hops=hops))
            pools = [(q, await gatherer.gather(q.question, sessions[q.role])) for q in questions]
            for values in itertools.product(*(grid[k] for k in weight_keys)):
                weights = dict(zip(weight_keys, values, strict=True))
                params = HybridParams(k_seed=k_seed, hops=hops, **weights)
                recs = []
                for q, pool in pools:
                    r = gatherer.rank(pool, params)
                    recs.append(
                        RetrievalRecord(
                            id=q.id,
                            role=q.role,
                            retrieved_nodes=r.nodes,
                            labelled_nodes=r.labelled,
                            warnings=r.warnings,
                            caveats=r.caveats,
                        )
                    )
                m = summarize_retrieval(qmap, recs, stale)
                rows.append(
                    {
                        "params": {"k_seed": k_seed, "hops": hops, **weights},
                        "recall@10": m["recall"]["10"]["rate"],
                        "recall@20": m["recall"]["20"]["rate"],
                        "trap_ok": m["trap_ok"]["rate"],
                        "false_stale_warning": m["false_stale_warning"]["rate"],
                        "missed_stale_warning": m["missed_stale_warning"]["rate"],
                    }
                )
        return rows

    roles: set[str] = {q.role for q in questions}
    rows: list[dict[str, Any]] = asyncio.run(_with_sessions(graph, env, roles, embedder, go))
    rows.sort(
        key=lambda r: (
            -(r["recall@20"] or 0),
            -(r["recall@10"] or 0),
            -(r["trap_ok"] or 0),
            (r["false_stale_warning"] or 0) + (r["missed_stale_warning"] or 0),
        )
    )
    return rows


def compare_runs(run_a: Path, run_b: Path, k: int = 20) -> dict[str, Any]:
    """Paired recall@k comparison of two retrieval runs on the same questions."""

    def load(run: Path) -> dict[str, RetrievalRecord]:
        lines = (run / "retrieval.jsonl").read_text(encoding="utf-8").splitlines()
        return {
            r.id: r for r in (RetrievalRecord.model_validate_json(x) for x in lines if x.strip())
        }

    a, b = load(run_a), load(run_b)
    questions = {q.id: q for q in load_questions()}
    gold = load_gold_nodes()
    ids = sorted(i for i in a if i in b and (set(gold.get(i, [])) | set(questions[i].gold_nodes)))

    def rec(r: RetrievalRecord) -> float:
        value = recall_at_k(
            r.retrieved_nodes, set(gold.get(r.id, [])) | set(questions[r.id].gold_nodes), k
        )
        return 0.0 if value is None else value

    return {
        "k": k,
        "n": len(ids),
        **paired_difference([rec(a[i]) for i in ids], [rec(b[i]) for i in ids]),
    }
