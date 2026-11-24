"""Risk-coverage, selective accuracy and offline gate calibration (tasks 7.7-7.8).

Everything here reads a run's questions.jsonl: no database, no LLM calls. Calibration needs an
`observe` run (every question answered, signals recorded, nothing gated) on **dev**; it refuses
any other split.

Risk-coverage (docs/eval.md §5): rank the questions the system answered by confidence, then walk
down. Coverage = answered / all questions; risk = wrong among those answered. Answering a question
that should have been clarified, refused or abstained counts as wrong.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from warehouse_qa.eval.metrics import ANSWER_MODES, Score, mode_matches
from warehouse_qa.eval.records import QuestionRecord
from warehouse_qa.eval.schema import EvalQuestion
from warehouse_qa.pipeline.gates import GateParams, confidence, gate_a, gate_b

OVER_REFUSAL_LIMIT = 0.05  # the Phase 7 gate


def decide(r: QuestionRecord, p: GateParams) -> tuple[str, str | None, float]:
    """(mode, reason, confidence) the gates would have given this recorded, ungated answer."""
    a = gate_a(r.signals, p)
    if a.mode is not None:
        return a.mode, a.reason, 0.0
    if r.predicted_mode not in ANSWER_MODES:
        return r.predicted_mode, r.reason_code, 0.0
    conf = confidence(r.signals, r.checks, r.repairs, r.warnings, p)
    b = gate_b(r.predicted_mode, conf, a.assume, p)
    return b.mode or r.predicted_mode, b.reason, conf


@dataclass(frozen=True)
class Point:
    coverage: float
    risk: float
    threshold: float


def risk_coverage(answered: Sequence[tuple[float, bool]], total: int) -> list[Point]:
    """Points of the curve, one per distinct confidence (ties can't be split)."""
    points: list[Point] = []
    ranked = sorted(answered, key=lambda x: -x[0])
    wrong = 0
    for i, (conf, ok) in enumerate(ranked, 1):
        wrong += not ok
        if i == len(ranked) or ranked[i][0] != conf:
            points.append(Point(round(i / total, 4), round(wrong / i, 4), conf))
    return points


def aurc(points: Sequence[Point]) -> float | None:
    """Area under risk-coverage over the covered range, normalised (lower is better)."""
    if not points:
        return None
    area, last = 0.0, 0.0
    for p in points:
        area += p.risk * (p.coverage - last)
        last = p.coverage
    return round(area / last, 4)


def selective_accuracy(points: Sequence[Point], coverage: float = 0.8) -> dict[str, Any]:
    """Accuracy at the first operating point that answers at least `coverage` of questions (or
    the highest coverage reached, which is reported)."""
    if not points:
        return {"coverage": 0.0, "accuracy": None}
    point = next((p for p in points if p.coverage >= coverage), points[-1])
    return {"coverage": point.coverage, "accuracy": round(1 - point.risk, 4)}


def reliability(
    records: Sequence[QuestionRecord], scores: Sequence[Score]
) -> dict[str, Any] | None:
    """The reliability block of a run's summary; None when the run recorded no confidence."""
    by_id = {s.id: s for s in scores}
    answered = [
        (r.confidence, by_id[r.id].exec_correct is True)
        for r in records
        if r.predicted_mode in ANSWER_MODES and r.confidence is not None
    ]
    if not answered:
        return None
    points = risk_coverage(answered, len(records))
    return {
        "points": [vars(p) for p in points],
        "aurc": aurc(points),
        "selective_accuracy_80": selective_accuracy(points),
        "full_coverage": points[-1].coverage,
        "accuracy_when_answering": round(1 - points[-1].risk, 4),
    }


# -- calibration ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    utility: float
    strict: int
    lenient: int
    over_refusal: float  # refuse or abstain on an answerable question
    needless_clarify: int  # clarify on an `answer` question
    wrong_answers: int
    stopped: int  # questions the gates declined or clarified


def evaluate(
    records: Sequence[QuestionRecord],
    questions: dict[str, EvalQuestion],
    correct: dict[str, bool],
    p: GateParams,
) -> Outcome:
    """Utility: +1 for a right answer or a right clarify/decline, -1 for answering wrongly (or
    answering what shouldn't be), 0 for anything else. A wrong number costs more than no number."""
    utility = 0.0
    strict = lenient = declined = needless = wrong = stopped = 0
    allowed = 0
    for r in records:
        q = questions[r.id]
        mode, _, _ = decide(r, p)
        answerable = q.expected_mode in ANSWER_MODES
        allowed += answerable
        strict += mode_matches(q.expected_mode, mode, lenient=False)
        lenient += mode_matches(q.expected_mode, mode, lenient=True)
        stopped += mode not in ANSWER_MODES and r.predicted_mode in ANSWER_MODES
        if mode in ANSWER_MODES:
            good = answerable and correct[r.id]
            utility += 1 if good else -1
            wrong += not good
        elif answerable:
            declined += mode in ("refuse", "abstain")
            needless += mode == "clarify" and q.expected_mode == "answer"
        elif mode_matches(q.expected_mode, mode, lenient=True):
            utility += 1
    return Outcome(
        utility=utility,
        strict=strict,
        lenient=lenient,
        over_refusal=round(declined / allowed, 4) if allowed else 0.0,
        needless_clarify=needless,
        wrong_answers=wrong,
        stopped=stopped,
    )


def _key(o: Outcome) -> tuple[Any, ...]:
    """Best first: within the over-refusal limit, then utility, then strict modes, then the
    gentlest gate (fewest questions stopped), so a threshold is only as tight as it earns."""
    return (o.over_refusal >= OVER_REFUSAL_LIMIT, -o.utility, -o.strict, o.stopped)


BOOLS = ("clarify_missing_time", "clarify_vague", "clarify_entity", "abstain_intent",
         "assume_metrics", "assume_models")  # fmt: skip
TAU_RELEVANCE = (0.0, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50)
TAU_MARGIN = (0.0, 0.01, 0.02, 0.05)
SIGNALS = ("margin_10", "table_margin", "ce_top", "best_cosine")
WEIGHTS = (0.0, 0.25, 0.5, 0.75, 1.0)
BOTH = (0.5,)  # PRD §3.6 combines both signals; a handful of dev errors can't rank weights
TAU_SLACK = 0.1  # set the threshold this far below anything that was right on dev
TAU_CONFIDENCE = tuple(round(0.05 * i, 2) for i in range(20))


def calibrate(
    records: Sequence[QuestionRecord],
    questions: dict[str, EvalQuestion],
    correct: dict[str, bool],
) -> tuple[GateParams, dict[str, Any]]:
    """Gate A first (Gate B off), then Gate B given Gate A. Returns params and the evidence."""
    grid_a = [
        GateParams(tau_relevance=tr, tau_metric_margin=tm, **dict(zip(BOOLS, flags, strict=True)))  # type: ignore[arg-type]
        for tr, tm, flags in itertools.product(
            TAU_RELEVANCE, TAU_MARGIN, itertools.product((False, True), repeat=len(BOOLS))
        )
    ]
    off = GateParams(tau_relevance=0.0, tau_metric_margin=0.0, **dict.fromkeys(BOOLS, False))  # type: ignore[arg-type]
    scored_a = [(evaluate(records, questions, correct, p), p) for p in grid_a]
    # Ties: the setting closest to the defaults (the PRD's policy), so that a rule dev can't
    # judge either way keeps its stated behaviour instead of being dropped by accident.
    default = GateParams()
    best_a = min(scored_a, key=lambda x: (*_key(x[0]), _distance(x[1], default)))[1]

    rows_b = []
    for signal, w, tau in itertools.product(SIGNALS, BOTH, TAU_CONFIDENCE):
        p = best_a.model_copy(
            update={"retrieval_signal": signal, "w_retrieval": w, "tau_confidence": tau}
        )
        rows_b.append((evaluate(records, questions, correct, p), p))
    curves = {}
    for signal, w in itertools.product(SIGNALS, WEIGHTS):
        p = best_a.model_copy(update={"retrieval_signal": signal, "w_retrieval": w})
        answered = [
            (conf, correct[r.id])
            for r in records
            for mode, _, conf in [decide(r, p)]
            if mode in ANSWER_MODES
        ]
        curves[(signal, w)] = risk_coverage(answered, len(records))
    best_outcome = min(_key(o) for o, _ in rows_b)
    ties = [(o, p) for o, p in rows_b if _key(o) == best_outcome]
    # Among equal outcomes: the ranking with the lowest AURC, then the highest threshold that
    # still costs nothing, less some slack so it isn't fitted to dev's least confident question.
    best = min(
        ties,
        key=lambda x: (
            aurc(curves[(x[1].retrieval_signal, x[1].w_retrieval)]) or 1.0,
            -x[1].tau_confidence,
        ),
    )[1]
    best = best.model_copy(
        update={"tau_confidence": round(max(0.0, best.tau_confidence - TAU_SLACK), 2)}
    )

    ablation = {"gates off": off, "Gate A + B (chosen)": best, "Gate A only": best_a}
    for flag in BOOLS:
        flipped = not getattr(best, flag)
        ablation[f"{flag} = {str(flipped).lower()}"] = best.model_copy(update={flag: flipped})
    for tau in TAU_RELEVANCE:
        ablation[f"tau_relevance = {tau}"] = best.model_copy(update={"tau_relevance": tau})
    for tau in TAU_MARGIN:
        ablation[f"tau_metric_margin = {tau}"] = best.model_copy(update={"tau_metric_margin": tau})
    for tau in (0.0, 0.5, 0.6, 0.7, 0.8, 0.9):
        ablation[f"tau_confidence = {tau}"] = best.model_copy(update={"tau_confidence": tau})
    evidence = {
        "combinations": len(grid_a) + len(rows_b),
        "ablation": {
            k: vars(evaluate(records, questions, correct, p)) for k, p in ablation.items()
        },
        "aurc": {f"{s}, w={w}": aurc(c) for (s, w), c in curves.items()},
        "curve": [vars(p) for p in curves[(best.retrieval_signal, best.w_retrieval)]],
    }
    return best, evidence


def _distance(p: GateParams, q: GateParams) -> float:
    return float(
        sum(getattr(p, f) != getattr(q, f) for f in BOOLS)
        + abs(p.tau_relevance - q.tau_relevance)
        + abs(p.tau_metric_margin - q.tau_metric_margin)
    )
