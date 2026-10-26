"""Query understanding, deterministic first (task 5.1, PRD §3.3 step 1).

time range   regex date parsing, relative phrases measured to AS_OF (2026-09-30)
metrics      vector search over metric nodes; the cosine margin between the top two
             candidates is the ambiguity signal Gate A uses (Phase 7)
entities     BM25 name/text matches (models, columns) the question names directly
fallback     only when nothing matches: Haiku 4.5 rewrites the question into search terms,
             logged as a call site of its own (PRD §5.2 lever 5)
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from warehouse_graph.llm import LLMClient, LLMRequest

from warehouse_qa.retrieval.tools import GraphTools

AS_OF = dt.date(2026, 9, 30)
FALLBACK_MODEL = "claude-haiku-4-5"
METRIC_MIN_COSINE = 0.35  # below this, a metric "match" is noise
METRIC_MARGIN = 0.05  # top-two cosines this close = ambiguous
NO_MATCH_COSINE = 0.25  # nothing at all above this -> fallback

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTH = r"(january|february|march|april|may|june|july|august|september|october|november|december)"
_YEAR = r"(20\d\d)"
_TIME_WORDS = re.compile(
    r"\b(revenue|sales|orders?|spend|refunds?|churn|growth|last|month|quarter|year|daily|"
    r"weekly|week)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TimeRange:
    start: dt.date  # inclusive
    end: dt.date  # exclusive
    phrase: str


def _month_end(year: int, month: int) -> dt.date:
    return dt.date(year + (month == 12), month % 12 + 1, 1)


def parse_time_range(text: str) -> TimeRange | None:
    """The first time expression in `text`, as a half-open date range."""
    t = text.lower()
    if m := re.search(r"last (\d+) days", t):
        n = int(m.group(1))
        return TimeRange(AS_OF - dt.timedelta(days=n - 1), AS_OF + dt.timedelta(days=1), m.group(0))
    if m := re.search(rf"\b(\d{{1,2}}) {_MONTH} {_YEAR}", t):
        day = dt.date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))
        return TimeRange(day, day + dt.timedelta(days=1), m.group(0))
    if m := re.search(rf"{_MONTH} (?:to|and|-|through) {_MONTH} {_YEAR}", t):
        y = int(m.group(3))
        start = dt.date(y, _MONTHS[m.group(1)], 1)
        return TimeRange(start, _month_end(y, _MONTHS[m.group(2)]), m.group(0))
    if m := re.search(rf"\bq([1-4]) {_YEAR}", t):
        q, y = int(m.group(1)), int(m.group(2))
        return TimeRange(dt.date(y, 3 * q - 2, 1), _month_end(y, 3 * q), m.group(0))
    if m := re.search(rf"(first|second) half of {_YEAR}", t):
        y = int(m.group(2))
        if m.group(1) == "first":
            return TimeRange(dt.date(y, 1, 1), dt.date(y, 7, 1), m.group(0))
        return TimeRange(dt.date(y, 7, 1), dt.date(y + 1, 1, 1), m.group(0))
    if m := re.search(rf"{_MONTH} {_YEAR}", t):
        y, mo = int(m.group(2)), _MONTHS[m.group(1)]
        return TimeRange(dt.date(y, mo, 1), _month_end(y, mo), m.group(0))
    if m := re.search(rf"{_YEAR} so far|so far (?:in|this year)", t):
        y = int(m.group(1)) if m.group(1) else AS_OF.year
        return TimeRange(dt.date(y, 1, 1), AS_OF + dt.timedelta(days=1), m.group(0))
    if m := re.search(rf"\b{_YEAR}\b", t):
        y = int(m.group(1))
        return TimeRange(dt.date(y, 1, 1), dt.date(y + 1, 1, 1), m.group(0))
    return None


@dataclass
class Understanding:
    question: str
    time_range: TimeRange | None
    metrics: list[tuple[str, float]]  # (metric id, cosine), best first, helpers excluded
    entities: list[str]  # node ids named directly (BM25 top hits)
    ambiguous_metric: bool
    missing_time: bool  # a time-dependent question with no time expression
    search_text: str  # what retrieval searches with (the question, or the fallback's terms)
    fallback_used: bool = False
    signals: dict[str, Any] = field(default_factory=dict)


FALLBACK_SYSTEM = """You turn a business question about a retail data warehouse into search terms
for a metadata catalogue (table, column and metric names and descriptions). Reply with one line of
5-12 lowercase keywords, nothing else."""


async def understand(
    question: str,
    tools: GraphTools,
    *,
    llm: LLMClient | None = None,
) -> Understanding:
    metric_hits = await tools.search(question, k=6, types=["metric"], mode="vector")
    metrics = [
        (h["id"], float(h["score"])) for h in metric_hits if float(h["score"]) >= METRIC_MIN_COSINE
    ]
    # Helper metrics are building blocks, not answers.
    kept = []
    for m, s in metrics:
        detail = await tools.metric(m)
        if detail is not None and not detail["metric"].get("helper"):
            kept.append((m, s))
    metrics = kept
    ambiguous = len(metrics) >= 2 and metrics[0][1] - metrics[1][1] < METRIC_MARGIN
    entity_hits = await tools.search(question, k=5, types=["model", "column"], mode="bm25")
    entities = [h["id"] for h in entity_hits]
    time_range = parse_time_range(question)
    any_hits = await tools.search(question, k=1, mode="vector")
    best = float(any_hits[0]["score"]) if any_hits else 0.0

    search_text = question
    fallback_used = False
    if best < NO_MATCH_COSINE and not entities and llm is not None:
        response = llm.complete(
            LLMRequest(
                model=FALLBACK_MODEL,
                system=FALLBACK_SYSTEM,
                messages=[{"role": "user", "content": question}],
                max_tokens=60,
                call_site="p1.understanding.fallback",
            )
        )
        search_text = f"{question} {response.text.strip()}"
        fallback_used = True

    return Understanding(
        question=question,
        time_range=time_range,
        metrics=metrics,
        entities=entities,
        ambiguous_metric=ambiguous,
        missing_time=time_range is None and bool(_TIME_WORDS.search(question)),
        search_text=search_text,
        fallback_used=fallback_used,
        signals={
            "best_cosine": round(best, 4),
            "metric_margin": round(metrics[0][1] - metrics[1][1], 4) if len(metrics) > 1 else None,
            "needs_fallback": best < NO_MATCH_COSINE and not entities,
        },
    )
