"""The P1 pipeline over a batch of questions (tasks 6.1-6.8, 7.1-7.6), in Batch API rounds.

    retrieve (hybrid | vector | oracle) -> pack                         per question, via MCP
    Gate A: clarify | abstain | carry on                               no LLM call if it stops
    round 0: generate SQL for every question left                      one batch
      static checks -> row scope -> small cells -> dry run as the role -> execute as the role
      -> 5 sanity checks
    rounds 1..max_repairs: regenerate only the questions that failed,  one batch each
      with the errors / check results / result stats (never rows)
    Gate B: answer | answer_with_assumption | abstain
    compose the answer (result, SQL, tables, caveats, warnings) -> audit

Config 6 (verification off) keeps the safety parts (static checks, small cells, executing as the
role, mapping permission errors to "not permitted") and drops the dry run, sanity checks and
repairs. `gates: observe` records the gates' signals but decides nothing, for calibration.
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
from warehouse_graph.governance.policies import PolicyFile, resolve
from warehouse_graph.graph.artifact import GraphArtifact
from warehouse_graph.graph.embed import Embedder
from warehouse_graph.llm import LLMClient, LLMResponse
from warehouse_graph.llm.pricing import cost_usd

from warehouse_qa.eval.compare import normalize
from warehouse_qa.eval.labels import SqlNodeResolver
from warehouse_qa.eval.records import QuestionRecord, Usage
from warehouse_qa.pipeline import answers, gates, generate
from warehouse_qa.pipeline.entities import Entities, dimensions
from warehouse_qa.pipeline.small_cells import MIN_CELL, SmallCellError, SmallCells
from warehouse_qa.pipeline.verify import REPAIR_HARD, Verifier
from warehouse_qa.retrieval.packer import PackedContext, pack
from warehouse_qa.retrieval.rerank import Reranker
from warehouse_qa.retrieval.retrievers import (
    HybridParams,
    HybridRetriever,
    Retrieved,
    VectorRetriever,
)
from warehouse_qa.retrieval.tools import GraphTools, open_tools
from warehouse_qa.retrieval.understanding import AS_OF, understand
from warehouse_qa.sqlcheck import StaticCheckError, check_sql


@dataclass(frozen=True)
class PipelineConfig:
    model: str
    effort: Any = "low"
    max_tokens: int = 1500
    batch: bool = True
    retriever: str = "hybrid"  # hybrid | vector | oracle
    verification: bool = True
    max_repairs: int = 2
    row_limit: int = 1000
    timeout_s: int = 30
    context_budget: int = 3000
    audit: bool = False
    gates: str = "off"  # off | observe | enforce


@dataclass(frozen=True)
class Ask:
    """One question from one role: an eval question, a red-team prompt or a user's."""

    id: str
    role: str
    question: str
    gold_nodes: tuple[str, ...] = ()  # oracle retrieval only


@dataclass
class Job:
    q: Ask
    retrieved: Retrieved
    context: PackedContext
    messages: list[dict[str, Any]]
    usage: Usage = field(default_factory=Usage)
    checks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    repairs: int = 0
    null_repaired: bool = False
    signals: dict[str, Any] = field(default_factory=dict)
    assume: bool = False  # Gate A: a term with competing definitions, default assumed
    suppressed: int | None = None
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
        gate_params: gates.GateParams | None = None,
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
        self.small_cells = SmallCells(graph, self.resolver)
        self.access = resolve(policies, graph).access
        self.guards = {role: answers.OutputGuard(a, graph) for role, a in self.access.items()}
        self.policy_matcher = answers.PolicyMatcher(graph, embedder)
        self.terms = gates.Terms(graph)
        self.entities = Entities(dimensions(graph), adapter)
        self.gate_params = gate_params or gates.GateParams()
        self.gold_nodes = gold_nodes or {}
        self.contexts: dict[str, str] = {}  # question id -> everything the LLM was sent

    async def run(self, questions: list[Ask]) -> list[QuestionRecord]:
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
            for job in jobs:
                self._gate_a(job, sessions[job.q.role])
            pending = [j for j in jobs if j.record is None]
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

    def _gate_a(self, job: Job, session: Any) -> None:
        """Record the gate signals; when gates are enforced, stop here if Gate A says so."""
        u = job.retrieved.understanding
        if self.c.gates == "off" or u is None:
            return
        question = job.q.question
        measured = self.terms.names_measure(question)
        mention = self.entities.unclear(question, session)
        job.signals = {
            **u.signals,
            **job.retrieved.signals,
            "missing_time": u.missing_time and bool(u.metrics),
            "future": u.time_range is not None and u.time_range.start > AS_OF,
            "intent": gates.intent(question),
            "names_measure": measured,
            "vague": gates.is_vague(question, measured),
            "entity": mention.to_signal() if mention else None,
            "competing_terms": self.terms.competing_terms(question),
            "misleading_terms": self.terms.misleading_terms(question),
        }
        if self.c.gates != "enforce":
            return
        d = gates.gate_a(job.signals, self.gate_params)
        job.assume = d.assume
        if d.mode is not None:
            job.checks.append(f"gate A: {d.reason}")
            self._finish(job, d.mode, reason=d.reason)

    async def _prepare(self, q: Ask, tools: GraphTools) -> Job:
        if self.c.retriever == "oracle":
            gold = [*self.gold_nodes.get(q.id, []), *q.gold_nodes]
            retrieved = await oracle_retrieval(gold, tools)
        elif self.c.retriever == "vector":
            retrieved = await VectorRetriever().retrieve(q.question, tools)
            retrieved.understanding = await understand(q.question, tools)  # for Gate A only
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
            signals=job.signals,
            suppressed_groups=job.suppressed,
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
        assumptions = self.guards[job.q.role].clean(
            [str(a) for a in reply.get("assumptions") or []]
        )

        def repair(problems: list[str], stats: Any = None) -> bool:
            if last_round:
                return False
            job.repairs += 1
            job.messages = generate.repair_turn(job.messages, resp.text, problems, stats)
            return True

        sql = reply.get("sql")
        if not sql:
            kind = str(reply.get("kind") or "not_available")
            if kind == generate.INVALID:
                job.checks.append(f"invalid reply: {reply.get('reason')}")
                if self.c.verification and repair(
                    ["your reply was not a single valid JSON object"]
                ):
                    return True
                self._finish(job, "abstain", reason="repair_exhausted: invalid reply")
                return False
            if kind not in generate.KINDS:
                kind = "not_available"
            if kind == "ambiguous" and self.c.gates != "off":
                self._finish(job, "clarify", reason="llm_ambiguous")
                return False
            # The reason is the model's own words: kept for the audit, never shown.
            self._finish(job, "abstain", reason=f"{kind}: {str(reply.get('reason') or '')[:200]}")
            return False

        try:
            checked = check_sql(str(sql), row_limit=self.c.row_limit + 1)
        except StaticCheckError as e:
            job.checks.append(f"static: {e}")
            if self.c.verification and repair([f"rejected before running: {e}"]):
                return True
            self._finish(job, "abstain", reason="static_check", sql=str(sql), error=str(e))
            return False
        if answers.outside_row_scope(checked.sql, self.access[job.q.role].row_filter):
            job.checks.append("outside_row_scope")
            self._finish(job, "refuse", reason="outside_row_scope", sql=str(sql))
            return False
        try:
            cells = self.small_cells.plan(checked.sql)
        except SmallCellError as e:
            job.checks.append("small_cells: undetermined")
            if self.c.verification and repair([str(e)]):
                return True
            self._finish(job, "abstain", reason="small_cells", sql=str(sql))
            return False
        if cells.applies:
            sql = cells.sql

        t0 = time.perf_counter()
        try:
            if self.c.verification:
                self.adapter.explain(session, cells.sql)
            rows = self.adapter.execute(session, cells.sql, self.c.row_limit, self.c.timeout_s)
            job.suppressed = None
            if cells.suppressed_sql is not None:
                found = self.adapter.execute(session, cells.suppressed_sql, 1, self.c.timeout_s)
                job.suppressed = int(found.rows[0][0])
        except psycopg.errors.InsufficientPrivilege as e:
            job.checks.append("not_permitted")
            self._finish(job, "refuse", reason="not_permitted", sql=str(sql), error=_error(e))
            return False
        except psycopg.Error as e:
            job.checks.append(f"error: {_error(e)}")
            if self.c.verification and repair([f"the database rejected the query: {_error(e)}"]):
                return True
            missing = isinstance(e, answers.UNAVAILABLE_ERRORS)
            reason = "not_available: unknown name" if missing else "sql_error"
            self._finish(job, "abstain", reason=reason, sql=str(sql), error=_error(e))
            return False
        finally:
            job.exec_s += time.perf_counter() - t0

        mode = "answer_with_assumption" if assumptions else "answer"
        if not self.c.verification:
            if self.c.gates == "enforce":  # Gate A only: Gate B needs a verification outcome
                mode = "answer_with_assumption" if job.assume else "answer"
            self._finish(job, mode, sql=str(sql), rows=rows)
            job.record.assumptions = assumptions  # type: ignore[union-attr]
            return False

        v = self.verifier.check(session, checked.sql, rows, self.c.timeout_s)
        job.warnings = v.warnings
        problems = list(v.problems)
        if job.suppressed and not rows.rows:  # everything was suppressed: not a bad query
            problems = [p for p in problems if p.check != "empty_result"]
        job.checks.append(", ".join(f"{p.check}" for p in problems) or "checks passed")
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
        assert job.record is not None
        job.record.assumptions = assumptions
        self._gate_b(job, job.record)
        return False

    def _gate_b(self, job: Job, r: QuestionRecord) -> None:
        if self.c.gates == "off":
            return
        if self.gate_params.retrieval_signal not in job.signals:  # vector: no retrieval margin
            if self.c.gates == "enforce":
                r.predicted_mode = "answer_with_assumption" if job.assume else "answer"
            return
        r.confidence = gates.confidence(
            job.signals, job.checks, job.repairs, job.warnings, self.gate_params
        )
        if self.c.gates != "enforce":
            return
        d = gates.gate_b(r.predicted_mode, r.confidence, job.assume, self.gate_params)
        if d.mode == "abstain":
            job.checks.append(f"gate B: {d.reason}")
            self._finish(job, "abstain", reason=d.reason, sql=r.sql)
            assert job.record is not None
            job.record.confidence = r.confidence
        else:
            r.predicted_mode = d.mode  # type: ignore[assignment]

    async def _compose(self, job: Job, tools: GraphTools) -> None:
        """Task 6.7: result + SQL + tables + caveats + freshness, as the user sees it."""
        r = job.record
        if r is None:
            return
        r.usage = job.usage
        r.latency_s = round(job.exec_s, 3)
        access = self.access[r.role]
        if r.predicted_mode == "clarify":
            entity = job.signals.get("entity")
            r.clarify_options = answers.clarify_options(
                r.reason_code, entity, job.retrieved, self.graph
            )
            r.answer_text = answers.clarifying(r.reason_code, entity, r.clarify_options)
            return
        if r.predicted_mode not in gates.ANSWERS:
            code = (r.reason_code or "").split(":", 1)[0]
            # Internal only (audit, scoring): the text below is the same for both modes.
            if (
                code in ("not_available", "not_permitted", "static_check", "nothing_relevant")
                and (target := self.policy_matcher.hidden_target(job.q.question, access))
            ):  # fmt: skip
                r.predicted_mode = "refuse"
                job.checks.append(f"policy: asked for hidden data ({target})")
            elif "empty_result" in " ".join(job.checks) and access.row_filter is not None:
                # Nothing to see for a row-filtered role: say what the role covers.
                r.predicted_mode, r.reason_code = "refuse", "outside_row_scope"
            r.answer_text = answers.declined(
                r.reason_code, answers.related_topics(job.retrieved, self.graph), access.row_filter
            )
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
        if r.suppressed_groups is not None:
            lines.append(
                f"Groups of fewer than {MIN_CELL} people are never shown"
                f" ({r.suppressed_groups} left out here)."
            )
        if access.row_filter is not None:
            rf = access.row_filter
            lines.append(f"Your role covers {rf.column} = '{rf.equals}' only.")
        lines += [f"Assumption: {a}" for a in r.assumptions]
        lines += [f"Warning: {w}" for w in r.warnings]
        # Documentation is quoted and attributed: it's data, not this system speaking.
        lines += [
            f'Documented caveat ({cid.removeprefix("caveat.")}): "{text}"'
            for cid, text in caveats.items()
        ]
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
                payload = {
                    "question_id": r.id,
                    "question": r.prompt,
                    "retrieved_nodes": r.retrieved_nodes[:40],
                    "sql": r.sql,
                    "checks": r.checks,
                    "repairs": r.repairs,
                    "outcome": r.predicted_mode,
                    "reason": r.reason_code,
                    "warnings": r.warnings,
                    "confidence": r.confidence,
                    "suppressed_groups": r.suppressed_groups,
                }
                writer.write(r.role, "qa.answer", payload)
                if r.predicted_mode == "refuse" or r.reason_code == "static_check":
                    denied = ("question_id", "question", "sql", "checks", "reason")
                    writer.write(r.role, "qa.policy_denied", {k: payload[k] for k in denied})
        finally:
            writer.close()
