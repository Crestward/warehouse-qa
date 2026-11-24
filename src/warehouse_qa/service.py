"""One question from one role, live: what the CLI (`warehouse-qa ask`) and the demo page call.

The same pipeline and config as the evals (configs/config4.yaml), without the Batch API. With no
ANTHROPIC_API_KEY it replays the recorded replies of the demo questions (demo/recorded), so the
demo runs for $0; everything else (retrieval, gates, database, checks) is real either way.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel
from warehouse_graph.adapters import PostgresAdapter
from warehouse_graph.governance import load_policies
from warehouse_graph.graph import GraphArtifact
from warehouse_graph.graph.embed import get_embedder
from warehouse_graph.llm import AnthropicProvider, LLMClient, LLMRequest, LLMResponse
from warehouse_graph.llm.provider import LLMProvider

from warehouse_qa.config import Env
from warehouse_qa.eval.harness import RunConfig
from warehouse_qa.eval.records import QuestionRecord
from warehouse_qa.eval.retrieval import load_hybrid_params
from warehouse_qa.pipeline.gates import GateParams
from warehouse_qa.pipeline.runner import Ask, Pipeline, PipelineConfig
from warehouse_qa.retrieval.rerank import CrossEncoderReranker
from warehouse_qa.retrieval.tools import open_tools

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs" / "config4.yaml"
DEMO_QUESTIONS = ROOT / "demo" / "questions.yaml"
RECORDED = ROOT / "demo" / "recorded"
LIVE_CACHE = ROOT / ".llm-cache"
ROLES = ("analyst_au", "analyst_nz", "marketing", "finance", "admin")

# What a user is told. `refuse` and `abstain` are one outcome on purpose: telling them apart
# would confirm what the wording of the answer doesn't (docs/threat-model.md C3).
OUTCOMES = {
    "answer": "Answered",
    "answer_with_assumption": "Answered, with an assumption",
    "clarify": "Needs one detail from you",
    "refuse": "Not answered",
    "abstain": "Not answered",
}


class DemoQuestion(BaseModel):
    role: str
    question: str
    shows: str


def demo_questions() -> list[DemoQuestion]:
    data = yaml.safe_load(DEMO_QUESTIONS.read_text(encoding="utf-8"))
    return [DemoQuestion.model_validate(q) for q in data["questions"]]


class NotRecordedError(RuntimeError):
    """No API key, and this question isn't one of the recorded demo questions."""


class ReplayProvider:
    """Reached only on a cache miss, i.e. for a question that wasn't recorded."""

    def complete(self, request: LLMRequest) -> LLMResponse:
        raise NotRecordedError(
            "This question isn't one of the recorded demo questions. Add ANTHROPIC_API_KEY to"
            " warehouse-graph/.env to ask anything (about US$0.01 per question)."
        )

    def complete_batch(self, requests: Sequence[LLMRequest]) -> list[LLMResponse]:
        return [self.complete(r) for r in requests]


@dataclass
class Answer:
    record: QuestionRecord
    outcome: str
    tables: list[str] = field(default_factory=list)
    lineage: list[tuple[str, str]] = field(default_factory=list)  # (upstream, downstream) ids

    @property
    def answered(self) -> bool:
        return self.record.predicted_mode in ("answer", "answer_with_assumption")


class Assistant:
    def __init__(
        self,
        env: Env | None = None,
        *,
        provider: LLMProvider | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self.env = env or Env.load(ROOT / "warehouse-qa.toml")
        self.live = provider is not None or bool(os.environ.get("ANTHROPIC_API_KEY"))
        if provider is None:
            provider = AnthropicProvider() if self.live else ReplayProvider()
        self.graph = GraphArtifact.load(self.env.graph)
        self.policies = load_policies(self.env.policies)
        self.embedder = get_embedder(self.graph.meta.embedding_model)
        c = RunConfig.load(CONFIG)
        self.client = LLMClient(
            provider,
            ledger_path=self.env.ledger,
            cache_dir=cache_dir or (LIVE_CACHE if self.live else RECORDED),
            run_id="ask",
        )
        self.pipeline = Pipeline(
            PipelineConfig(
                model=c.model,
                effort=c.effort,
                max_tokens=c.max_tokens,
                batch=False,
                verification=c.verification,
                max_repairs=c.max_repairs,
                row_limit=c.row_limit,
                timeout_s=c.timeout_s,
                context_budget=c.context_budget,
                audit=c.audit,
                gates=c.gates,
            ),
            self.graph,
            self.policies,
            PostgresAdapter(self.graph),
            self.client,
            reranker=CrossEncoderReranker(),
            embedder=self.embedder,
            hybrid_params=load_hybrid_params(ROOT / "configs" / "retriever_hybrid.yaml"),
            gate_params=GateParams.load(ROOT / "configs" / "gates.yaml"),
        )

    def ask(self, role: str, question: str) -> Answer:
        try:
            return asyncio.run(self._ask(role, question))
        except BaseExceptionGroup as group:  # the MCP client's task group wraps errors
            leaf: BaseException = group
            while isinstance(leaf, BaseExceptionGroup):
                leaf = leaf.exceptions[0]
            raise leaf from None

    async def _ask(self, role: str, question: str) -> Answer:
        [record] = await self.pipeline.run([Ask("live", role, question)])
        answer = Answer(record, OUTCOMES[record.predicted_mode])
        if answer.answered:
            answer.tables = [n for n in record.sql_nodes if n.startswith("model.")]
            answer.lineage = await self._lineage(role, answer.tables)
        return answer

    async def _lineage(self, role: str, tables: list[str]) -> list[tuple[str, str]]:
        """Where the answer's tables come from and what uses them, as the role may see it."""
        edges: dict[tuple[str, str], None] = {}
        async with open_tools(self.graph, self.policies, role, self.embedder) as tools:
            for table in tables:
                for direction in ("upstream", "downstream"):
                    sub = await tools.lineage(table, direction, 2, "model")
                    for e in (sub or {}).get("edges", []):
                        edges[(e["target"], e["source"])] = None  # source depends on target
        return list(edges)
