"""The committed eval data (tasks 4.3-4.7) and the harness end to end.

Data tests need nothing. The `integration` tests need the eval warehouse (docs/eval.md §8):
gold queries run as their roles, and a full `eval run` with an oracle fake LLM (it answers each
question with its own gold SQL) must score 100% execution accuracy with zero leaks, for $0.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
import yaml
from warehouse_graph.llm import FakeProvider, LLMRequest

from warehouse_qa.config import Env
from warehouse_qa.eval.labels import load_gold_nodes
from warehouse_qa.eval.schema import (
    QUESTION_COUNT,
    REDTEAM_COUNT,
    load_questions,
    load_redteam,
)
from warehouse_qa.eval.splits import load_split, make_splits, verify_manifest

QUESTIONS = {q.id: q for q in load_questions()}
REDTEAM = load_redteam()


def test_question_counts_are_exact() -> None:
    assert len(QUESTIONS) == QUESTION_COUNT == 150
    cats = Counter(q.category for q in QUESTIONS.values())
    assert cats == {"regular": 90, "trap": 40, "clarify_abstain": 20}
    modes = Counter(q.expected_mode for q in QUESTIONS.values() if q.category == "clarify_abstain")
    assert modes == {"clarify": 10, "abstain": 10}


def test_every_role_is_well_represented() -> None:
    roles = Counter(q.role for q in QUESTIONS.values())
    assert set(roles) == {"admin", "finance", "marketing", "analyst_au", "analyst_nz"}
    assert min(roles.values()) >= 25


def test_every_trap_type_is_covered() -> None:
    types = Counter(q.trap_type for q in QUESTIONS.values() if q.trap_type)
    assert len(types) == 10 and min(types.values()) >= 2


def test_redteam_counts() -> None:
    assert len(REDTEAM) == REDTEAM_COUNT == 50
    cats = Counter(r.category for r in REDTEAM)
    assert cats == {
        "direct": 12,
        "small_cell": 8,
        "injection": 10,
        "role_confusion": 10,
        "sql_escalation": 10,
    }


def test_gold_nodes_cover_every_gold_query() -> None:
    derived = load_gold_nodes()
    with_sql = {q.id for q in QUESTIONS.values() if q.gold_sql}
    assert set(derived) == with_sql
    assert all(any(n.startswith("model.") for n in derived[i]) for i in with_sql)


def test_splits_are_frozen_disjoint_and_stratified() -> None:
    assert verify_manifest() == []
    dev, test, mini = (set(load_split(s)) for s in ("dev", "test", "dev_mini"))
    assert len(dev) == 60 and len(test) == 90 and len(mini) == 30
    assert not dev & test and dev | test == set(QUESTIONS) and mini <= dev
    for cat, n in Counter(q.category for q in QUESTIONS.values()).items():
        in_dev = sum(1 for i in dev if QUESTIONS[i].category == cat)
        assert abs(in_dev - n * 0.4) <= 1


def test_splitting_is_deterministic() -> None:
    splits = make_splits(list(QUESTIONS.values()))
    assert splits["dev"] == load_split("dev") and splits["dev_mini"] == load_split("dev_mini")


def test_contamination_review_refers_to_real_items() -> None:
    data = yaml.safe_load(Path("evals/contamination_reviewed.yaml").read_text("utf-8"))
    ids = set(QUESTIONS) | {r.id for r in REDTEAM}
    for entry in data["reviewed"]:
        assert entry["item"] in ids and entry["reason"]
        assert entry["other"] in ids or entry["other"].startswith(("query_example.", "caveat."))


# -- integration --------------------------------------------------------------------------------


def _env_or_skip() -> Env:
    env = Env.load()
    if not env.graph.exists():
        pytest.skip("no warehouse-graph build/graph.wgz; run `make policies` there")
    return env


@pytest.mark.integration
def test_every_gold_query_returns_rows_as_its_role() -> None:
    from warehouse_graph.adapters import PostgresAdapter
    from warehouse_graph.graph import GraphArtifact

    from warehouse_qa.eval.harness import gold_results

    env = _env_or_skip()
    results = gold_results(
        PostgresAdapter(GraphArtifact.load(env.graph)), list(QUESTIONS.values()), 1000
    )
    failures = {i: e for i, (rows, e) in results.items() if e or not rows}
    assert failures == {}


@pytest.mark.integration
def test_oracle_run_scores_perfectly_with_no_leaks(tmp_path: Path) -> None:
    from warehouse_qa.eval.harness import RunConfig, run

    env = _env_or_skip()
    by_question = {q.question: q for q in QUESTIONS.values()}

    def oracle(req: LLMRequest) -> str:
        q = by_question[req.messages[0]["content"]]
        if q.gold_sql is None:
            return json.dumps({"sql": None, "reason": "not answerable"})
        assumptions = [q.assumption] if q.assumption else []
        return json.dumps({"sql": q.gold_sql, "assumptions": assumptions})

    config = RunConfig(name="oracle", system="schema_dump", model="claude-sonnet-5-5")
    out = run(
        config,
        "dev",
        env=Env(graph=env.graph, policies=env.policies, ledger=tmp_path / "costs.csv"),
        provider=FakeProvider(oracle),
        results_dir=tmp_path,
        cache_dir=None,
        skip_preflight=True,  # the fixture state matters for comparability, not for this test
    )
    summary = json.loads((out / "summary.json").read_text("utf-8"))
    overall = summary["metrics"]["overall"]
    assert summary["gold_errors"] == []
    assert overall["execution_accuracy"]["rate"] == 1.0
    assert overall["leakage"]["rate"] == 0.0
    assert overall["over_refusal"]["rate"] == 0.0
    by_mode = Counter(QUESTIONS[i].expected_mode for i in load_split("dev"))
    answers = by_mode["answer"] + by_mode["answer_with_assumption"]
    assert overall["execution_accuracy"]["n"] == answers
    assert (out / "report.md").exists() and (out / "scores.jsonl").exists()

    # Offline re-scoring reproduces the summary exactly, with no database or LLM.
    from warehouse_qa.eval.harness import rescore

    assert rescore(out)["metrics"] == summary["metrics"]
