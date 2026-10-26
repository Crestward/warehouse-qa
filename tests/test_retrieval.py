"""Retrieval (Phase 5): pure units, plus graph-backed tests that need warehouse-graph's
build/graph.wgz (no database). The graph-backed ones are `integration`; CI's eval-data job
builds the graph from the eval fixture (ad_spend stale) before running them.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from contextlib import AsyncExitStack
from typing import Any

import pytest
from warehouse_graph.governance import load_policies, resolve
from warehouse_graph.graph import GraphArtifact
from warehouse_graph.graph.embed import get_embedder

from warehouse_qa.config import Env
from warehouse_qa.eval.labels import SqlNodeResolver
from warehouse_qa.eval.retrieval import RetrievalRecord, Visibility, retrieval_trap_ok
from warehouse_qa.eval.schema import EvalQuestion, load_questions, load_redteam
from warehouse_qa.eval.splits import load_split
from warehouse_qa.eval.stats import paired_difference
from warehouse_qa.retrieval.packer import estimate_tokens, pack
from warehouse_qa.retrieval.rerank import OverlapReranker
from warehouse_qa.retrieval.retrievers import HybridRetriever, VectorRetriever
from warehouse_qa.retrieval.tools import GraphTools, identity_for, open_tools
from warehouse_qa.retrieval.understanding import AS_OF, parse_time_range

ROLES = ["admin", "finance", "marketing", "analyst_au", "analyst_nz"]

# -- units --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("net revenue in NZD for August 2026", "2026-08-01", "2026-09-01"),
        ("revenue in Q2 2026", "2026-04-01", "2026-07-01"),
        ("in January and February 2024", "2024-01-01", "2024-03-01"),
        ("January to September 2026 vs 2025", "2026-01-01", "2026-10-01"),
        ("gross revenue on Black Friday, 28 November 2025", "2025-11-28", "2025-11-29"),
        ("signed up in 2025", "2025-01-01", "2026-01-01"),
        ("the first half of 2026", "2026-01-01", "2026-07-01"),
        ("total sales for 2026 so far", "2026-01-01", "2026-10-01"),
        ("net revenue over the last 30 days", "2026-09-01", "2026-10-01"),
    ],
)
def test_parse_time_range(text: str, start: str, end: str) -> None:
    r = parse_time_range(text)
    assert r is not None
    assert (r.start.isoformat(), r.end.isoformat()) == (start, end)


def test_no_time_range() -> None:
    assert parse_time_range("What's our churn rate?") is None
    assert dt.date(2026, 9, 30) == AS_OF


def _q(**kw: Any) -> EvalQuestion:
    base: dict[str, Any] = {
        "id": "q001",
        "category": "trap",
        "trap_type": "deprecated_model",
        "trap_nodes": ["model.orders_v1"],
        "difficulty": "easy",
        "role": "finance",
        "question": "?",
        "expected_mode": "answer",
        "gold_sql": "select 1",
    }
    return EvalQuestion.model_validate(base | kw)


def _r(**kw: Any) -> RetrievalRecord:
    base: dict[str, Any] = {
        "id": "q001",
        "role": "finance",
        "retrieved_nodes": [],
        "labelled_nodes": [],
    }
    return RetrievalRecord.model_validate(base | kw)


def test_retrieval_trap_ok() -> None:
    q = _q()
    assert retrieval_trap_ok(q, _r()) is True
    assert retrieval_trap_ok(q, _r(retrieved_nodes=["model.orders_v1"])) is False
    labelled = _r(retrieved_nodes=["model.orders_v1"], labelled_nodes=["model.orders_v1"])
    assert retrieval_trap_ok(q, labelled) is True
    stale = _q(trap_type="stale_source", trap_nodes=[])
    assert retrieval_trap_ok(stale, _r()) is False
    assert (
        retrieval_trap_ok(stale, _r(warnings=["stale: source.marketing.ad_spend (error)"])) is True
    )


def test_paired_difference() -> None:
    a = [1.0] * 8 + [0.5] * 2
    b = [0.5] * 8 + [0.5] * 2
    d = paired_difference(a, b, n=2000)
    assert d["mean_diff"] == 0.4 and d["wins"] == 8 and d["ties"] == 2
    assert d["ci95_low"] > 0 and d["p_value"] < 0.05
    same = paired_difference([0.5, 0.7], [0.5, 0.7], n=500)
    assert same["mean_diff"] == 0 and same["p_value"] == 1.0


def test_overlap_reranker_and_token_estimate() -> None:
    scores = OverlapReranker().scores("net revenue by region", ["net revenue", "salary"])
    assert scores[0] > scores[1] == 0.0
    assert estimate_tokens("x" * 400) == 100


# -- graph-backed -------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def graph() -> GraphArtifact:
    env = Env.load()
    if not env.graph.exists():
        pytest.skip("no warehouse-graph build/graph.wgz")
    return GraphArtifact.load(env.graph)


async def _sessions(graph: GraphArtifact, stack: AsyncExitStack) -> dict[str, GraphTools]:
    policies = load_policies(Env.load().policies)
    embedder = get_embedder(graph.meta.embedding_model)
    return {
        role: await stack.enter_async_context(open_tools(graph, policies, role, embedder))
        for role in ROLES
    }


@pytest.mark.integration
def test_every_retriever_respects_every_role(graph: GraphArtifact) -> None:
    """Task 5.5: nothing hidden from the asking principal is ever retrieved or packed."""
    spec = resolve(load_policies(Env.load().policies), graph)
    visibility = Visibility(graph, spec)
    questions = {q.id: q for q in load_questions()}
    prompts = [questions[i].question for i in load_split("dev_mini")]
    prompts += [r.prompt for r in load_redteam()]
    retrievers: list[VectorRetriever | HybridRetriever] = [
        VectorRetriever(k=40),
        HybridRetriever(OverlapReranker(), SqlNodeResolver(graph)),
    ]

    async def go() -> list[str]:
        problems: list[str] = []
        async with AsyncExitStack() as stack:
            sessions = await _sessions(graph, stack)
            for role in ROLES:
                for prompt in prompts:
                    for retriever in retrievers:
                        r = await retriever.retrieve(prompt, sessions[role])
                        packed = await pack(r, sessions[role])
                        for v in visibility.violations(
                            role, [*r.nodes, *packed.included], packed.text
                        ):
                            problems.append(f"{role} / {retriever.name} / {prompt[:40]}: {v}")
        return problems

    problems = asyncio.run(go())
    assert problems == [], problems[:5]


@pytest.mark.integration
def test_hybrid_labels_deprecated_and_stale_nodes(graph: GraphArtifact) -> None:
    hybrid = HybridRetriever(OverlapReranker(), SqlNodeResolver(graph))

    async def go() -> tuple[Any, Any]:
        async with AsyncExitStack() as stack:
            sessions = await _sessions(graph, stack)
            deprecated = await hybrid.retrieve(
                "What is the total order amount in the old orders v1 table?", sessions["finance"]
            )
            stale = await hybrid.retrieve(
                "How much did we spend on ads in September 2026?", sessions["marketing"]
            )
            return deprecated, stale

    deprecated, stale = asyncio.run(go())
    assert "model.orders_v1" in deprecated.nodes
    assert any("model.fct_orders" in label for label in deprecated.labels["model.orders_v1"])
    assert "model.fct_orders" in deprecated.nodes  # the replacement is pulled in
    assert any(w.startswith("stale") and "SLA" in w for w in stale.warnings)


@pytest.mark.integration
def test_identity_for_each_role() -> None:
    policies = load_policies(Env.load().policies)
    assert {identity_for(policies, r) for r in ROLES} <= set(policies.identities)


class _NoMatchTools:
    """Stands in for GraphTools when nothing in the graph matches the question."""

    calls = 0

    async def search(self, query: str, **kw: Any) -> list[dict[str, Any]]:
        return [{"id": "column.x.y", "score": 0.1}] if kw.get("mode") == "vector" else []

    async def metric(self, name: str) -> dict[str, Any] | None:
        return None


def test_llm_fallback_only_when_nothing_matches(tmp_path: Any) -> None:
    from warehouse_graph.llm import FakeProvider, LLMClient

    from warehouse_qa.retrieval.understanding import understand

    provider = FakeProvider(lambda req: "weather forecast auckland")
    llm = LLMClient(provider, ledger_path=tmp_path / "costs.csv", cache_dir=None)
    u = asyncio.run(understand("Will it rain tomorrow?", _NoMatchTools(), llm=llm))  # type: ignore[arg-type]
    assert u.fallback_used and "weather forecast" in u.search_text
    assert provider.calls[0].call_site == "p1.understanding.fallback"
    assert provider.calls[0].model == "claude-haiku-4-5"
    no_llm = asyncio.run(understand("Will it rain tomorrow?", _NoMatchTools()))  # type: ignore[arg-type]
    assert not no_llm.fallback_used and no_llm.signals["needs_fallback"]
