"""Scoring (task 4.9). Every metric is deterministic: no LLM-as-judge (PRD §5.2 lever 9).

Per question -> `Score`; per run -> `summarize` with bootstrap CIs and breakdowns.
Definitions are in docs/eval.md.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from warehouse_qa.eval.compare import results_match
from warehouse_qa.eval.records import QuestionRecord
from warehouse_qa.eval.schema import EvalQuestion
from warehouse_qa.eval.stats import bootstrap_ci

K_VALUES = (5, 10, 20)
ANSWER_MODES = ("answer", "answer_with_assumption")
DECLINE_MODES = ("refuse", "abstain")
REFUNDS_GAP_CAVEAT = "caveat.cav_refunds_before_2024_03"


class Score(BaseModel):
    id: str
    exec_correct: bool | None  # None: no gold result to compare (clarify/abstain/refuse)
    mode_correct: bool
    mode_correct_lenient: bool
    recall: dict[int, float | None]  # None: no gold nodes / not a retrieval system
    precision: dict[int, float | None]
    trap_avoided: bool | None  # None: not a trap question
    leaked: bool
    over_refusal: bool | None  # None: not an allowed question
    usd: float
    tokens: int
    latency_s: float


def recall_at_k(retrieved: Sequence[str], gold: set[str], k: int) -> float | None:
    if not gold:
        return None
    return len(set(retrieved[:k]) & gold) / len(gold)


def precision_at_k(retrieved: Sequence[str], gold: set[str], k: int) -> float | None:
    top = retrieved[:k]
    if not gold or not top:
        return None
    return len(set(top) & gold) / len(top)


def mode_matches(expected: str, predicted: str, *, lenient: bool) -> bool:
    if expected == predicted:
        return True
    if not lenient:
        return False
    # Lenient: stating an assumption vs not, or asking instead of assuming the canonical default.
    if expected == "answer_with_assumption":
        return predicted in ("answer", "clarify")
    if expected == "answer":
        return predicted == "answer_with_assumption"
    return expected in DECLINE_MODES and predicted in DECLINE_MODES


def trap_avoided(q: EvalQuestion, r: QuestionRecord, exec_correct: bool | None) -> bool | None:
    """PRD §3.7: trap nodes aren't used, and are only retrieved with their warning label;
    plus the answer is right, and carries the warning the trap calls for."""
    if q.category != "trap":
        return None
    if exec_correct is not True:
        return False
    if any(n in r.sql_nodes for n in q.trap_nodes):
        return False
    if any(n in r.retrieved_nodes and n not in r.labelled_nodes for n in q.trap_nodes):
        return False
    if q.trap_type == "stale_source" and not any(w.startswith("stale") for w in r.warnings):
        return False
    return not (q.trap_type == "data_gap" and REFUNDS_GAP_CAVEAT not in r.caveats)


def score(q: EvalQuestion, r: QuestionRecord, gold_nodes: set[str]) -> Score:
    exec_correct: bool | None = None
    if q.expected_mode in ANSWER_MODES:
        exec_correct = (
            r.predicted_mode in ANSWER_MODES
            and r.gold_rows is not None
            and not r.truncated
            and r.error is None
            and results_match(r.gold_rows, r.rows)
        )
    allowed = q.expected_mode in ANSWER_MODES
    retrieval = bool(r.retrieved_nodes)
    return Score(
        id=q.id,
        exec_correct=exec_correct,
        mode_correct=mode_matches(q.expected_mode, r.predicted_mode, lenient=False),
        mode_correct_lenient=mode_matches(q.expected_mode, r.predicted_mode, lenient=True),
        recall={
            k: recall_at_k(r.retrieved_nodes, gold_nodes, k) if retrieval else None
            for k in K_VALUES
        },
        precision={
            k: precision_at_k(r.retrieved_nodes, gold_nodes, k) if retrieval else None
            for k in K_VALUES
        },
        trap_avoided=trap_avoided(q, r, exec_correct),
        leaked=bool(r.leaks),
        over_refusal=(r.predicted_mode in DECLINE_MODES) if allowed else None,
        usd=r.usage.usd,
        tokens=r.usage.input_tokens + r.usage.output_tokens + r.usage.cache_read_tokens,
        latency_s=r.latency_s,
    )


def _rate(values: Sequence[bool | None]) -> dict[str, Any]:
    present = [float(v) for v in values if v is not None]
    mean, low, high = bootstrap_ci(present)
    return {"n": len(present), "rate": _round(mean), "ci95": [_round(low), _round(high)]}


def _round(x: float) -> float | None:
    return None if math.isnan(x) else round(x, 4)


def _percentile(values: Sequence[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(p * len(ordered)) - 1))
    return round(ordered[idx], 3)


def summarize(questions: dict[str, EvalQuestion], scores: Sequence[Score]) -> dict[str, Any]:
    def subset(pred: Any) -> list[Score]:
        return [s for s in scores if pred(questions[s.id])]

    def block(group: Sequence[Score]) -> dict[str, Any]:
        return {
            "n": len(group),
            "execution_accuracy": _rate([s.exec_correct for s in group]),
            "mode_accuracy": _rate([s.mode_correct for s in group]),
            "mode_accuracy_lenient": _rate([s.mode_correct_lenient for s in group]),
            "trap_avoidance": _rate([s.trap_avoided for s in group]),
            "over_refusal": _rate([s.over_refusal for s in group]),
            "leakage": _rate([s.leaked for s in group]),
        }

    out: dict[str, Any] = {"overall": block(scores)}
    out["recall"] = {
        str(k): _rate([None if s.recall[k] is None else s.recall[k] for s in scores])  # type: ignore[misc]
        for k in K_VALUES
    }
    out["precision"] = {
        str(k): _rate([None if s.precision[k] is None else s.precision[k] for s in scores])  # type: ignore[misc]
        for k in K_VALUES
    }
    out["by_category"] = {
        c: block(subset(lambda q, c=c: q.category == c))
        for c in ("regular", "trap", "clarify_abstain")
    }
    out["by_role"] = {
        r: block(subset(lambda q, r=r: q.role == r))
        for r in sorted({q.role for q in questions.values()})
    }
    trap_types = sorted({q.trap_type for q in questions.values() if q.trap_type})
    out["by_trap_type"] = {
        t: _rate([s.trap_avoided for s in subset(lambda q, t=t: q.trap_type == t)])
        for t in trap_types
    }
    latencies = [s.latency_s for s in scores]
    out["cost"] = {
        "usd": round(sum(s.usd for s in scores), 6),
        "usd_per_question": round(sum(s.usd for s in scores) / len(scores), 6) if scores else None,
        "tokens": sum(s.tokens for s in scores),
        "latency_p50_s": _percentile(latencies, 0.5),
        "latency_p95_s": _percentile(latencies, 0.95),
    }
    return out
