"""`warehouse-qa eval run` and `eval rescore` (task 4.8).

run:
  1. preflight: database up, policy roles present, the eval fixture state (ad_spend stale,
     everything else fresh), committed splits intact
  2. gold results: every gold query executed AS its question's role
  3. the system under test, in batch rounds through the LLM wrapper (cache, ledger, cap)
  4. static check -> execute AS the role -> leak scan -> per-question record
  5. results/<run_id>/: config.yaml, questions.jsonl, summary.json, report.md

rescore: recompute every metric from questions.jsonl alone (no database, no LLM calls), so
metric fixes and gate calibration (Phase 7) are free.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import psycopg
import yaml
from pydantic import BaseModel, ConfigDict
from warehouse_graph.adapters import PostgresAdapter
from warehouse_graph.demo.load import conninfo_from_env
from warehouse_graph.governance import Principal, load_policies, resolve
from warehouse_graph.graph import GraphArtifact
from warehouse_graph.llm import AnthropicProvider, LLMClient, LLMRequest
from warehouse_graph.llm.pricing import cost_usd
from warehouse_graph.llm.provider import LLMProvider

from warehouse_qa.config import Env
from warehouse_qa.eval.compare import normalize
from warehouse_qa.eval.labels import SqlNodeResolver, load_gold_nodes
from warehouse_qa.eval.leakage import LeakScanner
from warehouse_qa.eval.metrics import score, summarize
from warehouse_qa.eval.records import ROW_CAP, QuestionRecord, Usage
from warehouse_qa.eval.report import render_report
from warehouse_qa.eval.schema import EvalQuestion, load_questions, write_text_lf
from warehouse_qa.eval.splits import load_split, verify_manifest
from warehouse_qa.sqlcheck import check_select
from warehouse_qa.systems.schema_dump import SchemaDumpSystem, parse_reply

RESULTS_DIR = Path("results")
STALE_SOURCE = "source.marketing.ad_spend"


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    system: Literal["schema_dump"]
    model: str
    effort: Literal["low", "medium", "high"] | None = "low"
    max_tokens: int = 1024
    batch: bool = True
    row_limit: int = ROW_CAP
    timeout_s: int = 30

    @classmethod
    def load(cls, path: Path) -> RunConfig:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


@dataclass
class Context:
    env: Env
    graph: GraphArtifact
    adapter: PostgresAdapter
    resolver: SqlNodeResolver
    scanner: LeakScanner


def preflight(env: Env, graph: GraphArtifact) -> list[str]:
    problems = verify_manifest()
    adapter = PostgresAdapter(graph)
    try:
        stale = adapter.freshness(STALE_SOURCE)
        fresh = adapter.freshness("source.shop.orders")
    except psycopg.OperationalError as e:
        return [*problems, f"database unreachable: {e}"]
    if stale.status != "error":
        problems.append(
            f"{STALE_SOURCE} is {stale.status}; the eval fixture loads it stale"
            " (warehouse-graph: `warehouse-graph demo-data --stale ad_spend`, then rebuild)"
        )
    if fresh.status != "pass":
        problems.append(f"source.shop.orders is {fresh.status}; reload the warehouse first")
    try:
        adapter.session_as(Principal("analyst_au", "")).close()
    except psycopg.OperationalError:
        problems.append("policy roles missing; run `make policies` in warehouse-graph")
    return problems


def _json_safe(rows: Sequence[Sequence[Any]]) -> list[list[Any]]:
    return [[normalize(v) for v in r] for r in rows]


def _error(e: Exception) -> str:
    return f"{type(e).__name__}: {str(e).splitlines()[0][:300]}" if str(e) else type(e).__name__


def gold_results(
    adapter: PostgresAdapter, questions: Sequence[EvalQuestion], limit: int
) -> dict[str, tuple[list[list[Any]] | None, str | None]]:
    out: dict[str, tuple[list[list[Any]] | None, str | None]] = {}
    sessions: dict[str, Any] = {}
    try:
        for q in questions:
            if q.gold_sql is None:
                continue
            s = sessions.setdefault(q.role, adapter.session_as(Principal(q.role, "")))
            try:
                rows = adapter.execute(s, q.gold_sql, limit, 60)
                out[q.id] = (_json_safe(rows.rows), None)
            except psycopg.Error as e:
                out[q.id] = (None, _error(e))
    finally:
        for s in sessions.values():
            s.close()
    return out


def _usage(model: str, response: Any, batch: bool) -> Usage:
    u = response.usage
    return Usage(
        calls=1,
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        cache_read_tokens=u.cache_read_input_tokens,
        cache_write_tokens=u.cache_creation_input_tokens,
        usd=round(cost_usd(model, u, batch=batch), 6),
    )


def execute_answer(
    ctx: Context,
    q: EvalQuestion,
    reply: dict[str, Any],
    config: RunConfig,
    session: Any,
) -> QuestionRecord:
    sql = reply.get("sql")
    assumptions = [str(a) for a in reply.get("assumptions") or []]
    record = QuestionRecord(
        id=q.id, role=q.role, prompt=q.question, predicted_mode="abstain", assumptions=assumptions
    )
    if not sql:
        record.answer_text = str(reply.get("reason") or "no SQL")
        return record
    record.sql = str(sql)
    record.sql_nodes = sorted(ctx.resolver.nodes(record.sql))
    record.sql_check = check_select(record.sql)
    if record.sql_check is not None:
        record.answer_text = f"query rejected: {record.sql_check}"
        return record
    try:
        rows = ctx.adapter.execute(session, record.sql, config.row_limit, config.timeout_s)
    except psycopg.errors.InsufficientPrivilege as e:
        record.predicted_mode = "refuse"
        record.error = _error(e)
        record.answer_text = "That data isn't available to your role."
        return record
    except psycopg.Error as e:
        record.error = _error(e)
        record.answer_text = "The query failed."
        return record
    record.columns = list(rows.columns)
    record.rows = _json_safe(rows.rows)
    record.truncated = rows.truncated
    record.predicted_mode = "answer_with_assumption" if assumptions else "answer"
    record.answer_text = f"{len(rows.rows)} row(s)"
    return record


def run(
    config: RunConfig,
    split: str,
    *,
    env: Env | None = None,
    provider: LLMProvider | None = None,
    results_dir: Path = RESULTS_DIR,
    cache_dir: Path | None = Path(".llm-cache"),
    skip_preflight: bool = False,
) -> Path:
    env = env or Env.load()
    graph = GraphArtifact.load(env.graph)
    if not skip_preflight:
        problems = preflight(env, graph)
        if problems:
            raise RuntimeError("preflight failed:\n- " + "\n- ".join(problems))
    spec = resolve(load_policies(env.policies), graph)
    ids = set(load_split(split))
    questions = [q for q in load_questions() if q.id in ids]
    with psycopg.connect(conninfo_from_env()) as owner:
        ctx = Context(
            env=env,
            graph=graph,
            adapter=PostgresAdapter(graph),
            resolver=SqlNodeResolver(graph),
            scanner=LeakScanner.from_database(owner, spec, graph),
        )

    started = dt.datetime.now(dt.UTC)
    run_id = f"{started:%Y%m%dT%H%M%SZ}-{config.name}-{split}"
    out = results_dir / run_id
    out.mkdir(parents=True)
    (out / "config.yaml").write_text(
        yaml.safe_dump(
            {**config.model_dump(), "split": split, "graph_manifest": graph.meta.manifest_sha256}
        ),
        encoding="utf-8",
    )

    gold = gold_results(ctx.adapter, questions, config.row_limit)
    system = SchemaDumpSystem(
        graph, spec, model=config.model, max_tokens=config.max_tokens, effort=config.effort
    )
    client = LLMClient(
        provider or AnthropicProvider(),
        ledger_path=env.ledger,
        cache_dir=cache_dir,
        run_id=run_id,
    )
    spent_before = client.spent_usd
    requests: list[LLMRequest] = [system.request(q.role, q.question) for q in questions]
    t0 = time.perf_counter()
    responses = client.complete_many(requests, batch=config.batch)  # one batch round
    round_s = time.perf_counter() - t0

    records: list[QuestionRecord] = []
    sessions: dict[str, Any] = {}
    try:
        for q, req, resp in zip(questions, requests, responses, strict=True):
            s = sessions.setdefault(q.role, ctx.adapter.session_as(Principal(q.role, "")))
            t1 = time.perf_counter()
            record = execute_answer(ctx, q, parse_reply(resp), config, s)
            record.usage = _usage(config.model, resp, config.batch)
            record.context_tokens = (
                resp.usage.input_tokens
                + resp.usage.cache_read_input_tokens
                + resp.usage.cache_creation_input_tokens
            )
            # Batch latency is the round, shared; report the per-question execution share on top.
            record.latency_s = round(round_s / len(questions) + time.perf_counter() - t1, 3)
            record.gold_rows, record.gold_error = gold.get(q.id, (None, None))
            record.leaks = ctx.scanner.scan(
                q.role,
                q.question,
                record.answer_text,
                record.columns,
                record.rows,
                llm_context=req.system,
            )
            records.append(record)
    finally:
        for s in sessions.values():
            s.close()

    write_text_lf(out / "questions.jsonl", "".join(r.model_dump_json() + "\n" for r in records))
    run_meta = {
        "spent_usd": round(client.spent_usd - spent_before, 6),
        "batch_round_s": round(round_s, 1),
    }
    rescore(out, run_meta=run_meta)
    return out


def rescore(run_dir: Path, run_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Recompute scores, summary and report from questions.jsonl only."""
    questions = {q.id: q for q in load_questions()}
    gold_nodes = load_gold_nodes()
    records = [
        QuestionRecord.model_validate_json(line)
        for line in (run_dir / "questions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    scores = [
        score(
            questions[r.id],
            r,
            set(gold_nodes.get(r.id, [])) | set(questions[r.id].gold_nodes),
        )
        for r in records
    ]
    summary_path = run_dir / "summary.json"
    previous = json.loads(summary_path.read_text("utf-8")) if summary_path.exists() else {}
    config = yaml.safe_load((run_dir / "config.yaml").read_text(encoding="utf-8"))
    summary = {
        "run_id": run_dir.name,
        "config": config,
        "run": run_meta or previous.get("run", {}),
        "gold_errors": sorted(r.id for r in records if r.gold_error),
        "metrics": summarize(questions, scores),
    }
    write_text_lf(summary_path, json.dumps(summary, indent=2, default=str) + "\n")
    write_text_lf(run_dir / "scores.jsonl", "".join(s.model_dump_json() + "\n" for s in scores))
    write_text_lf(run_dir / "report.md", render_report(summary, records, scores, questions))
    return summary
