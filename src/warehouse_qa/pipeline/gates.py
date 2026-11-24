"""Gate A and Gate B (tasks 7.5-7.6, PRD §3.6). Pure: text and recorded signals in, a mode out.

Gate A (before any SQL is generated, so it also saves the call):
    abstain   out-of-scope intent (forecast, cause, advice, a future period), or nothing in the
              graph is close enough to the question (`tau_relevance`)
    clarify   a named thing that matches several rows (or none), an evaluative question with no
              measure, two metrics too close to call (`tau_metric_margin`), or a time-dependent
              question with no period (`clarify_missing_time`)
    assume    a term with competing definitions and a canonical default: carry on, and the
              answer's mode becomes `answer_with_assumption`

Gate B (after verification): confidence from exactly two signals, retrieval margin and the
verification outcome. Below `tau_confidence` -> abstain.

`decide` reads only what a run recorded, so every threshold is calibrated offline on dev with no
LLM calls (`eval calibrate`, task 7.7). The chosen values are in configs/gates.yaml.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict
from warehouse_graph.graph.artifact import GraphArtifact
from warehouse_graph.graph.schema import MetricNode, ModelNode

GATES_CONFIG = Path("configs/gates.yaml")
ANSWERS = ("answer", "answer_with_assumption")
MARGIN_SCALE = 1.5  # the largest rank score: cross-encoder 1 + w_seed 0.5 (retrievers.py)


class GateParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Gate A
    tau_relevance: float = 0.35  # best cosine below this: nothing relevant
    tau_metric_margin: float = 0.0  # two unnamed metrics closer than this: clarify
    clarify_missing_time: bool = False
    clarify_vague: bool = True
    clarify_entity: bool = True
    abstain_intent: bool = True
    assume_metrics: bool = True  # terms with competing metric definitions (revenue, churn)
    assume_models: bool = False  # terms that are the name of a misleadingly named model (sales)
    # Gate B
    retrieval_signal: Literal["margin_10", "table_margin", "ce_top", "best_cosine"] = "table_margin"
    w_retrieval: float = 0.5
    tau_confidence: float = 0.0

    @classmethod
    def load(cls, path: Path = GATES_CONFIG) -> GateParams:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))["params"])


# -- Gate A signals from the question text -------------------------------------------------------

_INTENTS = {
    "forecast": re.compile(
        r"\b(forecast|predict\w*|projected|projection|will|going to|tomorrow|"
        r"next (?:week|month|quarter|year))\b",
        re.IGNORECASE,
    ),
    "cause": re.compile(r"^\s*(?:why|what (?:caused|explains|is causing|drove))\b", re.IGNORECASE),
    "advice": re.compile(
        r"\b(should (?:we|i|the)|recommend\w*|what do you think|is it worth)\b", re.IGNORECASE
    ),
}
_PERFORM = re.compile(
    r"\bhow(?:'s| did| do| does| is| are| was| were| has| have)\b.*"
    r"\b(?:do|doing|done|perform(?:s|ed|ing)?|going|tracking)\b"
    r"|\bperform(?:s|ed|ing)? (?:better|best|worse|worst|well)\b",
    re.IGNORECASE,
)
_BEST = re.compile(
    r"\b(?:best|worst|top)(?: \d+)? (?:customer|store|product|campaign|region|channel|segment|"
    r"categor|employee|staff)\w*",
    re.IGNORECASE,
)
_MEASURED = re.compile(r"\b(?:by|in terms of|highest|lowest|most|least|fewest)\b", re.IGNORECASE)


def intent(question: str) -> str | None:
    """Out-of-scope intent: forecast, cause or advice."""
    return next((name for name, rx in _INTENTS.items() if rx.search(question)), None)


def is_vague(question: str, names_measure: bool) -> bool:
    """Evaluative ("best customers", "how did X do") with nothing that says by what measure."""
    if names_measure or (_MEASURED.search(question) and not _PERFORM.search(question)):
        return False
    return bool(_PERFORM.search(question) or _BEST.search(question))


class Terms:
    """Vocabulary from the graph: metric names, and terms with competing definitions."""

    def __init__(self, graph: GraphArtifact) -> None:
        metrics = [n for n in graph.nodes.values() if isinstance(n, MetricNode) and not n.helper]
        labels = [_words(m.label or m.name) for m in metrics]
        self.measures = [w for w in labels if w]
        by_id = {m.id: m for m in metrics}
        self.competing: dict[str, set[str]] = {}  # term -> qualifiers that make it unambiguous
        for m in metrics:
            canonical = by_id.get(m.superseded_by or "")
            if canonical is None:
                continue
            for term in _words(m.label or m.name) & _words(canonical.label or canonical.name):
                if term in ("rate", "count", "total"):
                    continue
                others = {w for words in labels if term in words for w in words} - {term, "rate"}
                self.competing[term] = others | {"canonical"}
        self.misleading: set[str] = {
            n.name.split("_", 1)[1].replace("_", " ")
            for n in graph.nodes.values()
            if isinstance(n, ModelNode) and n.trap is not None and n.trap.kind == "misleading_name"
        }

    def names_measure(self, question: str) -> bool:
        words = _words(question)
        return any(label <= words for label in self.measures)

    def competing_terms(self, question: str) -> list[str]:
        text = question.lower()
        words = _words(question)
        found = []
        for term, qualifiers in sorted(self.competing.items()):
            # "revenue orders" names a kind of order, not the revenue metric
            bare = re.search(rf"\b{term}(?:ed|s)?\b(?! orders?\b)", text)
            if bare and not words & qualifiers:
                found.append(term)
        return found

    def misleading_terms(self, question: str) -> list[str]:
        text = question.lower()
        return sorted(t for t in self.misleading if re.search(rf"\b{re.escape(t)}\b", text))


def _words(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", text.lower())
    # crude singular/past forms, so "orders" = "order" and "churned" = "churn"
    return {re.sub(r"(?<=[a-z]{3})(?:ed|s)$", "", w) for w in words}


# -- decisions -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    mode: str | None  # None: carry on to SQL generation
    reason: str | None = None
    assume: bool = False


def gate_a(s: dict[str, Any], p: GateParams) -> Decision:
    if p.abstain_intent and s.get("intent"):
        return Decision("abstain", f"out_of_scope: {s['intent']}")
    if p.abstain_intent and s.get("future"):
        return Decision("abstain", "out_of_scope: future period")
    if s.get("best_cosine", 1.0) < p.tau_relevance:
        return Decision("abstain", "nothing_relevant")
    entity = s.get("entity")
    if p.clarify_entity and entity:
        kind = "entity_ambiguous" if len(entity["matches"]) > 1 else "entity_unresolved"
        return Decision("clarify", kind)
    if p.clarify_vague and s.get("vague"):
        return Decision("clarify", "no_measure")
    margin = s.get("metric_margin")
    if margin is not None and margin < p.tau_metric_margin and not s.get("names_measure"):
        return Decision("clarify", "metric_ambiguous")
    if p.clarify_missing_time and s.get("missing_time"):
        return Decision("clarify", "missing_time")
    assume = (p.assume_metrics and bool(s.get("competing_terms"))) or (
        p.assume_models and bool(s.get("misleading_terms"))
    )
    return Decision(None, None, assume)


def verification_score(checks: Sequence[str], repairs: int, warnings: Sequence[str]) -> float:
    """1 for a query that passed first time; less for each repair and for a doubtful result.
    (A stale-data warning says nothing about whether the query is right.)"""
    score = 1.0 - 0.4 * repairs
    if any("NULL" in w for w in warnings):
        score -= 0.2
    return max(0.0, score)


def confidence(
    s: dict[str, Any], checks: Sequence[str], repairs: int, warnings: Sequence[str], p: GateParams
) -> float:
    scale = MARGIN_SCALE if "margin" in p.retrieval_signal else 1.0
    retrieval = min(1.0, max(0.0, float(s.get(p.retrieval_signal) or 0.0) / scale))
    verified = verification_score(checks, repairs, warnings)
    return round(p.w_retrieval * retrieval + (1 - p.w_retrieval) * verified, 4)


def gate_b(mode: str, conf: float, assume: bool, p: GateParams) -> Decision:
    """Only for queries that ran and verified; every other outcome keeps its mode."""
    if conf < p.tau_confidence:
        return Decision("abstain", "low_confidence")
    return Decision("answer_with_assumption" if assume else "answer")
