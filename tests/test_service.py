"""Phase 8: the live path behind the CLI and the demo page, replaying recorded replies ($0)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from warehouse_qa.config import Env
from warehouse_qa.eval import ci_gate
from warehouse_qa.eval.harness import refuse_second_test_run
from warehouse_qa.eval.schema import load_questions
from warehouse_qa.service import ROOT, Assistant, NotRecordedError, demo_questions


@pytest.fixture(scope="module")
def replay(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Assistant]:
    env = Env.load()
    if not env.graph.exists():
        pytest.skip("no warehouse-graph build/graph.wgz")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("ANTHROPIC_API_KEY", "")
        ledger = tmp_path_factory.mktemp("ledger") / "costs.csv"
        bot = Assistant(Env(graph=env.graph, policies=env.policies, ledger=ledger))
        if bot.graph.meta.embedding_model == "hash":
            pytest.skip("recorded replies need the real embedding model")
        yield bot


def test_demo_questions_are_not_eval_questions() -> None:
    eval_texts = {q.question.lower() for q in load_questions()}
    assert not {q.question.lower() for q in demo_questions()} & eval_texts


@pytest.mark.integration
@pytest.mark.parametrize("i", range(len(demo_questions())))
def test_every_demo_question_runs_without_a_key(replay: Assistant, i: int) -> None:
    q = demo_questions()[i]
    answer = replay.ask(q.role, q.question)
    assert answer.record.answer_text
    if answer.answered:
        assert answer.record.sql and answer.tables
        # a role's lineage never shows a node the role can't see
        visible = set(replay.pipeline.access[q.role].relations)
        models = {n for e in answer.lineage for n in e if n.startswith(("model.", "source."))}
        assert models <= visible


@pytest.mark.integration
def test_other_questions_say_they_need_a_key(replay: Assistant) -> None:
    with pytest.raises(NotRecordedError):
        replay.ask("admin", "How many orders had exactly three lines in May 2024?")


@pytest.mark.integration
def test_the_demo_page_runs(replay: Assistant, monkeypatch: pytest.MonkeyPatch) -> None:
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    page = AppTest.from_file(str(ROOT / "src/warehouse_qa/app.py"), default_timeout=180).run()
    assert not page.exception
    page.selectbox[0].select_index(1).run()
    page.button[0].click().run()
    assert not page.exception
    assert "Answered" in page.success[0].value


def _run(tmp_path: Path, name: str, **changes: dict[str, Any]) -> Path:
    out = tmp_path / name
    out.mkdir()
    base = json.loads(ci_gate.BASELINE.read_text("utf-8"))["questions"]
    scores = [{"id": i, "leaked": False, **s, **changes.get(i, {})} for i, s in base.items()]
    (out / "scores.jsonl").write_text("\n".join(map(json.dumps, scores)), encoding="utf-8")
    return out


def test_ci_gate_allows_one_flip_but_no_drop_or_leak(tmp_path: Path) -> None:
    base = json.loads(ci_gate.BASELINE.read_text("utf-8"))["questions"]
    right = [i for i, s in base.items() if s["exec_correct"]]
    wrong: dict[str, Any] = {"exec_correct": False}
    assert ci_gate.check(_run(tmp_path, "same"))[0]
    assert ci_gate.check(_run(tmp_path, "one", **{right[0]: wrong}))[0]
    assert not ci_gate.check(_run(tmp_path, "two", **{right[0]: wrong, right[1]: wrong}))[0]
    assert not ci_gate.check(_run(tmp_path, "leak", **{right[0]: {"leaked": True}}))[0]


def test_the_test_split_runs_once(tmp_path: Path) -> None:
    (tmp_path / "20270101T000000Z-config4-test").mkdir()
    with pytest.raises(RuntimeError, match="runs once"):
        refuse_second_test_run("config4", tmp_path)
