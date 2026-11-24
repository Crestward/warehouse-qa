"""The P1 pipeline over a batch of questions (tasks 6.1-6.8), in Batch API rounds.

    retrieve (hybrid | oracle) -> pack context                         per question, via MCP
    round 0: generate SQL for every question                           one batch
      static checks -> dry run as the role -> execute as the role -> 5 sanity checks
    rounds 1..max_repairs: regenerate only the questions that failed,  one batch each
      with the errors / check results / result stats (never rows)
    compose the answer (result, SQL, tables, caveats, warnings) -> audit

Config 6 (verification off) keeps the safety parts (static checks, executing as the role,
mapping permission errors to "not permitted") and drops the dry run, sanity checks and repairs.
"""

from __future__ import annotations

import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

import psycopg
from warehouse_graph.adapters import PostgresAdapter, Rows
from warehouse_graph.governance import Principal
from warehouse_graph.governance.audit import AuditWriter
from warehouse_graph.governance.policies import PolicyFile
from warehouse_graph.graph.artifact import GraphArtifact
from warehouse_graph.graph.embed import Embedder
from warehouse_graph.llm import LLMClient, LLMResponse
from warehouse_graph.llm.pricing import cost_usd

from warehouse_qa.eval.compare import normalize
from warehouse_qa.eval.labels import SqlNodeResolver
from warehouse_qa.eval.records import QuestionRecord, Usage
from warehouse_qa.eval.schema import EvalQuestion
from warehouse_qa.pipeline import generate
from warehouse_qa.pipeline.verify import REPAIR_HARD, Verifier
from warehouse_qa.retrieval.packer import PackedContext, pack
from warehouse_qa.retrieval.rerank import Reranker
from warehouse_qa.retrieval.retrievers import HybridParams, HybridRetriever, Retrieved
from warehouse_qa.retrieval.tools import GraphTools, open_tools
from warehouse_qa.sqlcheck import StaticCheckError, check_sql


@dataclass(frozen=True)
class PipelineConfig:
    model: str
    effort: Any = "low"
    max_tokens: int = 1500
    batch: bool = True
    retriever: str = "hybrid"  # hybrid | oracle
    verification: bool = True
    max_repairs: int = 2
    row_limit: int = 1000
    timeout_s: int = 30
    context_budget: int = 3000
    audit: bool = False


@dataclass
class Job:
    q: EvalQuestion
    retrieved: Retrieved
    context: PackedContext
    messages: list[dict[str, Any]]
    usage: Usage = field(default_factory=Usage)
    checks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    repairs: int = 0
    null_repaired: bool = False
    exec_s: float = 0.0
    record: QuestionRecord | None = None


def _add_usage(job: Job, model: str, response: LLMResponse, batch: bool) -> None:
    u = response.usage
    job.usage.calls += 1
    job.usage.input_tokens += u.input_tokens
    job.usage.output_tokens += u.output_tokens
    job.usage.cache_read_tokens += u.cache_read_input_tokens
    job.usage.cache_write_tokens += u.cache_creation_input_tokens
    job.usage.usd = round(job.usage.usd + cost_usd(model, u, batch=batch), 6)


def _error(e: Exception) -> str:
    first = str(e).splitlines()[0][:300] if str(e) else ""
    return f"{type(e).__name__}: {first}" if first else type(e).__name__


async def oracle_retrieval(q_nodes: list[str], tools: GraphTools) -> Retrieved:
    """Gold nodes as the context (PRD §3.7 oracle upper bound), labelled like any retrieval."""
    order = {"model": 0, "metric": 1, "column": 2, "caveat": 3}
    nodes: list[str] = []
    labels: dict[str, list[str]] = {}
    for nid in sorted(q_nodes, key=lambda n: (order.get(n.split(".")[0], 9), n)):
        rec = await tools.node(nid)
        if rec is None:
            continue
        nodes.append(nid)
        node = rec["node"]
        found = []
        if node.get("deprecated"):
            found.append(f"deprecated; use {node.get('replaced_by')}")
        if node.get("trap"):
            found.append(f"trap: {node['trap']['kind']}")
        if node.get("upstream_freshness") in ("warn", "error", "runtime error"):
            found.append(f"stale data: {node.get('upstream_freshness_note')}")
        if found:
            labels[nid] = found
    return Retrieved(nodes=nodes, labels=labels)


class Pipeline:
    def __init__(
        self,
        config: PipelineConfig,
        graph: GraphArtifact,
        policies: PolicyFile,
        adapter: PostgresAdapter,
        client: LLMClient,
        *,
        reranker: Reranker,
        embedder: Embedder,
        hybrid_params: HybridParams,
        gold_nodes: dict[str, list[str]] | None = None,
    ) -> None:
        self.c = config
        self.graph = graph
        self.policies = policies
        self.adapter = adapter
        self.client = client
        self.embedder = embedder
        self.resolver = SqlNodeResolver(graph)
        self.retriever = HybridRetriever(reranker, self.resolver, hybrid_params)
        self.verifier = Verifier(graph, adapter, self.resolver)
        self.gold_nodes = gold_nodes or {}
        self.contexts: dict[str, str] = {}  # question id -> everything the LLM was sent

    async def run(self, questions: list[EvalQuestion]) -> list[QuestionRecord]:
        async with AsyncExitStack() as stack:
            tools = {
                role: await stack.enter_async_context(
                    open_tools(self.graph, self.policies, role, self.embedder)
                )
                for role in sorted({q.role for q in questions})
            }
            sessions = {role: self.adapter.session_as(Principal(role, "")) for role in tools}
            for s in sessions.values():
                stack.callback(s.close)
            jobs = [await self._prepare(q, tools[q.role]) for q in questions]
            pending = list(jobs)
            for round_no in range(self.c.max_repairs + 1 if self.c.verification else 1):
                if not pending:
                    break
                requests = [
                    generate.request(self.c.model, j.messages, self.c.effort, self.c.max_tokens)
                    for j in pending
                ]
                t0 = time.perf_counter()
                responses = self.client.complete_many(requests, batch=self.c.batch)
                share = (time.perf_counter() - t0) / len(pending)
                nxt = []
                for job, resp in zip(pending, responses, strict=True):
                    job.exec_s += share
                    _add_usage(job, self.c.model, resp, self.c.batch)
                    last_round = round_no >= self.c.max_repairs or not self.c.verification
                    if self._step(job, resp, sessions[job.q.role], last_round):
                        nxt.append(job)
                pending = nxt
            for job in jobs:
                await self._compose(job, tools[job.q.role])
            if self.c.audit:
                self._audit(jobs)
            self.contexts = {j.q.id: self.llm_context(j.messages) for j in jobs}
            return [j.record for j in jobs if j.record is not None]

    async def _prepare(self, q: EvalQuestion, tools: GraphTools) -> Job:
        if self.c.retriever == "oracle":
            gold = [*self.gold_nodes.get(q.id, []), *q.gold_nodes]
            retrieved = await oracle_retrieval(gold, tools)
        else:
            retrieved = await self.retriever.retrieve(q.question, tools)
        context = await pack(retrieved, tools, self.c.context_budget)
        return Job(q, retrieved, context, generate.first_turn(q.question, context.text))

    def _finish(self, job: Job, mode: Any, *, reason: str | None = None, sql: str | None = None,
                rows: Rows | None = None, error: str | None = None) -> None:  # fmt: skip
        q = job.q
        record = QuestionRecord(
            id=q.id,
            role=q.role,
            prompt=q.question,
            predicted_mode=mode,
            sql=sql,
            error=error,
            retrieved_nodes=job.retrieved.nodes,
            labelled_nodes=job.retrieved.labelled,
            warnings=job.warnings,
            context_tokens=job.context.tokens,
            checks=job.checks,
            repairs=job.repairs,
            reason_code=reason,
        )
        if sql:
            record.sql_nodes = sorted(self.resolver.nodes(sql))
        if rows is not None:
            record.columns = list(rows.columns)
            record.rows = [[normalize(v) for v in r] for r in rows.rows]
            record.truncated = rows.truncated
        job.record = record

    def _step(self, job: Job, resp: LLMResponse, session: Any, last_round: bool) -> bool:
        """Process one reply. True = send it back for another repair round."""
        reply = generate.parse(resp)
        assumptions = [str(a) for a in reply.get("assumptions") or []]
        sql = reply.get("sql")
        if not sql:
            self._finish(job, "abstain", reason=f"no_sql: {str(reply.get('reason') or '')[:200]}")
            return False

        def repair(problems: list[str], stats: Any = None) -> bool:
            if last_round:
                return False
            job.repairs += 1
            job.messages = generate.repair_turn(job.messages, resp.text, problems, stats)
            return True

        try:
            checked = check_sql(str(sql), row_limit=self.c.row_limit + 1)
        except StaticCheckError as e:
            job.checks.append(f"static: {e}")
            if self.c.verification and repair([f"rejected before running: {e}"]):
                return True
            self._finish(job, "abstain", reason="static_check", sql=str(sql), error=str(e))
            return False

        t0 = time.perf_counter()
        try:
            if self.c.verification:
                self.adapter.explain(session, checked.sql)
            rows = self.adapter.execute(session, checked.sql, self.c.row_limit, self.c.timeout_s)
        except psycopg.errors.InsufficientPrivilege as e:
            job.checks.append("not_permitted")
            self._finish(job, "refuse", reason="not_permitted", sql=str(sql), error=_error(e))
            return False
        except psycopg.Error as e:
            job.checks.append(f"error: {_error(e)}")
            if self.c.verification and repair([f"the database rejected the query: {_error(e)}"]):
                return True
            self._finish(job, "abstain", reason="sql_error", sql=str(sql), error=_error(e))
            return False
        finally:
            job.exec_s += time.perf_counter() - t0

        mode = "answer_with_assumption" if assumptions else "answer"
        if not self.c.verification:
            self._finish(job, mode, sql=str(sql), rows=rows)
            job.record.assumptions = assumptions  # type: ignore[union-attr]
            return False

        v = self.verifier.check(session, checked.sql, rows, self.c.timeout_s)
        job.warnings = v.warnings
        job.checks.append(", ".join(f"{p.check}" for p in v.problems) or "checks passed")
        problems = list(v.problems)
        if any(p.check == "null_blowup" for p in problems) and job.null_repaired:
            problems = [p for p in problems if p.check != "null_blowup"]  # warn only, once
            job.warnings = [*job.warnings, "many NULLs in the result (kept after review)"]
        if problems:
            if any(p.check == "null_blowup" for p in problems):
                job.null_repaired = True
            if repair([p.detail for p in problems], rows.stats()):
                return True
            hard = [p for p in problems if p.check in REPAIR_HARD]
            if hard:
                self._finish(
                    job, "abstain", reason=f"repair_exhausted: {hard[0].check}", sql=str(sql)
                )
                return False
        self._finish(job, mode, sql=str(sql), rows=rows)
        job.record.assumptions = assumptions  # type: ignore[union-attr]
        return False

    async def _compose(self, job: Job, tools: GraphTools) -> None:
        """Task 6.7: result + SQL + tables + caveats + freshness, as the user sees it."""
        r = job.record
        if r is None:
            return
        r.usage = job.usage
        r.latency_s = round(job.exec_s, 3)
        if r.predicted_mode not in ("answer", "answer_with_assumption"):
            r.answer_text = {
                "refuse": "That data isn't available to your role.",
            }.get(r.predicted_mode, f"I can't answer this reliably ({r.reason_code}).")
            return
        caveats: dict[str, str] = {}
        tables = [n for n in r.sql_nodes if n.startswith("model.")]
        for t in tables:
            rec = await tools.node(t)
            for c in (rec or {}).get("caveats", []):
                if c.get("severity") in ("high", "medium"):
                    caveats[c["id"]] = c["description"]
        r.caveats = sorted(caveats)
        lines = [f"{len(r.rows)} row(s){' (truncated)' if r.truncated else ''}."]
        lines += [f"Assumption: {a}" for a in r.assumptions]
        lines += [f"Warning: {w}" for w in r.warnings]
        lines += [f"Note ({cid.removeprefix('caveat.')}): {text}" for cid, text in caveats.items()]
        lines.append("Tables: " + ", ".join(t.removeprefix("model.") for t in tables))
        r.answer_text = "\n".join(lines)

    def llm_context(self, job_messages: list[dict[str, Any]]) -> str:
        return generate.SYSTEM + "\n" + "\n".join(str(m["content"]) for m in job_messages)

    def _audit(self, jobs: list[Job]) -> None:
        writer = AuditWriter()
        try:
            for j in jobs:
                r = j.record
                if r is None:
                    continue
                writer.write(
                    r.role,
                    "qa.answer",
                    {
                        "question_id": r.id,
                        "question": r.prompt,
                        "retrieved_nodes": r.retrieved_nodes[:40],
                        "sql": r.sql,
                        "checks": r.checks,
                        "repairs": r.repairs,
                        "outcome": r.predicted_mode,
                        "reason": r.reason_code,
                        "warnings": r.warnings,
                    },
                )
        finally:
            writer.close()
