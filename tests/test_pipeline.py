"""Phase 6: static checks (incl. the escalation gate), and the full pipeline driven by scripted
fake LLMs against the eval warehouse (integration; $0)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from warehouse_graph.llm import FakeProvider, LLMRequest

from warehouse_qa.config import Env
from warehouse_qa.eval.harness import RunConfig, run
from warehouse_qa.eval.records import QuestionRecord
from warehouse_qa.eval.schema import load_questions
from warehouse_qa.sqlcheck import StaticCheckError, check_sql

# -- static checks: the escalation gate (task 6.2) ----------------------------------------------

ESCALATION = [
    ("SET ROLE admin", "not a query"),
    ("set role admin; select 1", "exactly one statement"),
    ("RESET ROLE", "not a query"),
    ("select 1; select email from marts.dim_customers", "exactly one statement"),
    ("select 1; drop table marts.fct_orders", "exactly one statement"),
    ("insert into marts.dim_products (product_id) values (1)", "not a query"),
    ("update marts.fct_orders set order_total = 0", "not a query"),
    ("delete from marts.fct_orders", "not a query"),
    ("drop table marts.fct_orders", "not a query"),
    ("copy (select * from marts.dim_customers) to '/tmp/x.csv'", "not a query"),
    ("begin; set local role admin; select 1; commit;", "exactly one statement"),
    ("select * into marts.copy from marts.fct_orders", "SELECT INTO"),
    ("select * from marts_base.dim_customers", "outside the allowed schemas"),
    ("select * from raw.customers", "outside the allowed schemas"),
    ("select * from pg_catalog.pg_roles", "outside the allowed schemas"),
    ("select * from dim_customers", "outside the allowed schemas"),
    ("select pg_sleep(600)", "function not allowed"),
    ("select pg_read_file('/etc/passwd')", "function not allowed"),
    ("select set_config('role', 'admin', false)", "function not allowed"),
    ("select dblink('host=x', 'select 1')", "function not allowed"),
    ("select lo_import('/etc/passwd')", "function not allowed"),
]


@pytest.mark.parametrize(("sql", "reason"), ESCALATION)
def test_escalation_attempts_are_rejected(sql: str, reason: str) -> None:
    with pytest.raises(StaticCheckError, match=reason):
        check_sql(sql)


def test_every_gold_query_passes_the_static_checks() -> None:
    for q in load_questions():
        if q.gold_sql:
            check_sql(q.gold_sql)  # raises on rejection


def test_ctes_are_not_tables() -> None:
    sql = "with t as (select order_id from marts.fct_orders) select count(*) from t"
    assert check_sql(sql).limit_injected


def test_limit_injection() -> None:
    assert check_sql("select order_id from marts.fct_orders", 50).sql.endswith("LIMIT 50")
    kept = check_sql("select order_id from marts.fct_orders limit 5", 50)
    assert not kept.limit_injected and kept.sql.endswith("limit 5")
    union = check_sql("select 1 union select 2", 10).sql
    assert union.startswith("SELECT * FROM (") and union.endswith("LIMIT 10")


# -- the pipeline end to end (integration) ------------------------------------------------------

QUESTIONS = {q.id: q for q in load_questions()}
BY_TEXT = {q.question: q for q in QUESTIONS.values()}


def _question_of(req: LLMRequest) -> Any:
    first = req.messages[0]["content"]
    return BY_TEXT[first.split("QUESTION: ", 1)[1]]


def _env(tmp_path: Path) -> Env:
    env = Env.load()
    if not env.graph.exists():
        pytest.skip("no warehouse-graph build/graph.wgz")
    return Env(graph=env.graph, policies=env.policies, ledger=tmp_path / "costs.csv")


def _run(tmp_path: Path, reply: Any, split: str = "dev_mini", **cfg: Any) -> list[QuestionRecord]:
    config = RunConfig(name="fake", system="pipeline", model="claude-sonnet-5-5", **cfg)
    out = run(
        config,
        split,
        env=_env(tmp_path),
        provider=FakeProvider(reply),
        results_dir=tmp_path,
        cache_dir=None,
        skip_preflight=True,
    )
    lines = (out / "questions.jsonl").read_text("utf-8").splitlines()
    return [QuestionRecord.model_validate_json(x) for x in lines]


def _gold_reply(req: LLMRequest) -> str:
    q = _question_of(req)
    if q.gold_sql is None:
        return json.dumps({"sql": None, "reason": "not answerable"})
    return json.dumps({"sql": q.gold_sql, "assumptions": [q.assumption] if q.assumption else []})


@pytest.mark.integration
def test_gold_sql_passes_verification_without_false_repairs(tmp_path: Path) -> None:
    records = _run(tmp_path, _gold_reply, split="dev")
    summary = json.loads(next(tmp_path.glob("*/summary.json")).read_text("utf-8"))
    overall = summary["metrics"]["overall"]
    assert overall["execution_accuracy"]["rate"] == 1.0
    assert overall["leakage"]["rate"] == 0.0
    repaired = {r.id: r.checks for r in records if r.repairs}
    assert repaired == {}, repaired
    # The stale-source trap questions get their warning from the staleness check.
    stale = [r for r in records if QUESTIONS[r.id].trap_type == "stale_source"]
    assert stale and all(any(w.startswith("stale") for w in r.warnings) for r in stale)


@pytest.mark.integration
def test_malicious_sql_never_reaches_the_database(tmp_path: Path) -> None:
    attacks = iter(sql for sql, _ in ESCALATION * 3)

    def attacker(req: LLMRequest) -> str:
        return json.dumps({"sql": next(attacks)})

    records = _run(tmp_path, attacker, max_repairs=0)
    assert records and all(r.predicted_mode == "abstain" for r in records)
    assert all(r.reason_code == "static_check" and not r.rows for r in records)


@pytest.mark.integration
def test_repair_loop_fixes_deprecated_usage_and_fanout(tmp_path: Path) -> None:
    bad = {
        # q091 (T1): orders_v1 is deprecated -> repaired to the gold query
        "q091": "select sum(order_amount) from marts.orders_v1 where order_status = 'completed'"
        " and order_date >= date '2025-01-01' and order_date < date '2026-01-01'",
        # q124: joining payments fans out order totals (split tenders, failed attempts)
        "q124": "select sum(o.order_total_nzd) from marts.fct_orders o join marts.fct_payments p"
        " on p.order_id = o.order_id where o.is_revenue_order"
        " and o.order_date >= date '2025-01-01' and o.order_date < date '2026-01-01'",
    }
    seen_feedback: dict[str, str] = {}

    def scripted(req: LLMRequest) -> str:
        q = _question_of(req)
        if q.id in bad and len(req.messages) == 1:
            return json.dumps({"sql": bad[q.id]})
        if len(req.messages) > 1:
            seen_feedback[q.id] = req.messages[-1]["content"]
        return _gold_reply(req)

    config = RunConfig(name="fake", system="pipeline", model="claude-sonnet-5-5")
    out = run(
        config,
        "dev",
        env=_env(tmp_path),
        provider=FakeProvider(scripted),
        results_dir=tmp_path,
        cache_dir=None,
        skip_preflight=True,
    )
    lines = (out / "questions.jsonl").read_text("utf-8").splitlines()
    records = {r.id: r for r in (QuestionRecord.model_validate_json(x) for x in lines)}
    for qid in bad:
        assert records[qid].repairs == 1, records[qid].checks
    assert "deprecated" in seen_feedback["q091"] and "model.fct_orders" in seen_feedback["q091"]
    assert "inflated" in seen_feedback["q124"]
    stats_line = seen_feedback["q124"].split("(no rows are shown): ", 1)[1].splitlines()[0]
    stats = json.loads(stats_line)
    assert set(stats) == {"row_count", "columns"}  # aggregates only: no rows reach the LLM
    scores = {
        json.loads(x)["id"]: json.loads(x)
        for x in (out / "scores.jsonl").read_text("utf-8").splitlines()
    }
    assert scores["q091"]["exec_correct"] and scores["q124"]["exec_correct"]


@pytest.mark.integration
def test_without_verification_nothing_is_repaired(tmp_path: Path) -> None:
    def bad_then_gold(req: LLMRequest) -> str:
        if _question_of(req).id == "q091":
            return json.dumps({"sql": "select sum(order_amount) from marts.orders_v1"})
        return _gold_reply(req)

    config = RunConfig(
        name="fake", system="pipeline", model="claude-sonnet-5-5", verification=False
    )
    out = run(
        config,
        "dev",
        env=_env(tmp_path),
        provider=FakeProvider(bad_then_gold),
        results_dir=tmp_path,
        cache_dir=None,
        skip_preflight=True,
    )
    lines = (out / "questions.jsonl").read_text("utf-8").splitlines()
    records = {r.id: r for r in (QuestionRecord.model_validate_json(x) for x in lines)}
    assert records["q091"].repairs == 0 and "model.orders_v1" in records["q091"].sql_nodes
    assert all(not r.warnings for r in records.values())  # no staleness check either


@pytest.mark.integration
def test_every_answer_is_audited(tmp_path: Path) -> None:
    import psycopg
    from warehouse_graph.demo.load import conninfo_from_env

    with psycopg.connect(conninfo_from_env()) as conn:
        before = conn.execute(
            "select count(*) from audit.events where event_type = 'qa.answer'"
        ).fetchone()
    records = _run(tmp_path, _gold_reply, audit=True)
    with psycopg.connect(conninfo_from_env()) as conn:
        after = conn.execute(
            "select count(*) from audit.events where event_type = 'qa.answer'"
        ).fetchone()
        last = conn.execute(
            "select principal, payload from audit.events where event_type = 'qa.answer'"
            " order by event_id desc limit 1"
        ).fetchone()
    assert before is not None and after is not None and last is not None
    assert after[0] - before[0] == len(records)
    principal, payload = last
    assert principal in {"admin", "finance", "marketing", "analyst_au", "analyst_nz"}
    assert {"question", "retrieved_nodes", "sql", "checks", "outcome"} <= set(payload)
    assert "rows" not in payload  # results go to the user, never to the audit log


def test_cross_join_is_not_a_fanout() -> None:
    import sqlglot

    tree = sqlglot.parse_one(
        "select c.category, sum(r.x) from marts.fct_refunds r cross join (select 1) c group by 1",
        dialect="postgres",
    )
    joins = tree.args["joins"]
    assert all(j.args.get("kind", "").upper() == "CROSS" for j in joins)
