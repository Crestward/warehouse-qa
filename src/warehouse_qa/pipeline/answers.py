"""What the user is told (task 7.3): fixed wording, so a refusal can't confirm anything.

Non-confirming (PRD §3.2 scenario 3): asking for a hidden column, a column that never existed, or
a table outside the role gets the **same text** (`UNAVAILABLE`), whatever path the pipeline took
to get there (no SQL, permission denied, unknown column, rejected table). The model's own words
never reach the user on a refusal: they said things like "the schema has no salary column".

`refuse` vs `abstain` is therefore internal (audit and scoring); a UI shows `answer_text`.
"""

from __future__ import annotations

import re
from typing import Any

import psycopg
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from warehouse_graph.governance.policies import Access, RowFilter
from warehouse_graph.graph.artifact import GraphArtifact
from warehouse_graph.graph.embed import Embedder
from warehouse_graph.graph.query import GraphQuery
from warehouse_graph.graph.schema import ColumnNode, MetricNode

from warehouse_qa.retrieval.retrievers import Retrieved

UNAVAILABLE = "I can't answer that with the data available to your role."
OUT_OF_SCOPE = (
    "I can't answer that from the warehouse. It holds history to 2026-09-30; it can't forecast,"
    " explain causes or make recommendations."
)
UNVERIFIED = (
    "I couldn't write a query for this that passed every check, so I won't give a number I don't"
    " trust. Try asking for one measure, for a stated period."
)
OUTSIDE_ROWS = "Your role covers {column} = '{value}' only, so I can't answer that."

# DB errors that must read the same as "not permitted": otherwise the wording tells a hidden
# column (permission denied) apart from one that doesn't exist (undefined column).
UNAVAILABLE_ERRORS = (
    psycopg.errors.InsufficientPrivilege,
    psycopg.errors.UndefinedColumn,
    psycopg.errors.UndefinedTable,
    psycopg.errors.InvalidSchemaName,
)


def declined(reason: str | None, related: list[str], row_filter: RowFilter | None) -> str:
    """The message for `refuse` and `abstain`, from the reason code alone."""
    code = (reason or "").split(":", 1)[0]
    if code == "out_of_scope":
        return OUT_OF_SCOPE
    if code in ("repair_exhausted", "low_confidence", "small_cells", "sql_error"):
        return UNVERIFIED
    if code == "outside_row_scope" and row_filter is not None:
        return OUTSIDE_ROWS.format(column=row_filter.column, value=row_filter.equals)
    if related:
        return f"{UNAVAILABLE} You can ask me about: {', '.join(related)}."
    return UNAVAILABLE


def related_topics(retrieved: Retrieved, graph: GraphArtifact, limit: int = 3) -> list[str]:
    """Tables and metrics the role can see that are closest to the question (the alternative)."""
    out = []
    for nid in retrieved.nodes:
        kind, _, name = nid.partition(".")
        if retrieved.labels.get(nid):
            continue  # don't offer what carries a warning
        node = graph.nodes.get(nid)
        if isinstance(node, MetricNode) and not node.helper:
            out.append(name.replace("_", " "))
        elif kind == "model" and not name.startswith(("stg_", "int_")):
            out.append(name)
    return out[:limit]


def clarifying(reason: str | None, entity: dict[str, Any] | None, options: list[str]) -> str:
    """One multiple-choice question (PRD §3.6)."""
    if entity and reason == "entity_ambiguous":
        head = f'Which {entity["noun"]} do you mean by "{entity["text"]}"?'
    elif entity and reason == "entity_unresolved":
        noun = entity["noun"]
        head = f'I can\'t find a {noun} called "{entity["text"]}". Which {noun} do you mean?'
    elif reason == "missing_time":
        head = "Which period should I cover?"
    else:
        head = "That could mean several things. Which measure do you want, and for which period?"
    return "\n".join([head, *(f"- {o}" for o in options)])


def clarify_options(
    reason: str | None,
    entity: dict[str, Any] | None,
    retrieved: Retrieved,
    graph: GraphArtifact,
) -> list[str]:
    """Options built from what the role can see, never from the model's text."""
    if entity and reason in ("entity_ambiguous", "entity_unresolved"):
        return list(entity["matches"] or entity["known"])
    if reason == "missing_time":
        return ["the last 90 days", "2026 so far", "2025"]
    metrics = [
        n.removeprefix("metric.")
        for n in retrieved.nodes
        if isinstance(m := graph.nodes.get(n), MetricNode)
        and not m.helper
        and not retrieved.labels.get(n)
    ]
    measures = []
    for nid in retrieved.nodes:
        node = graph.nodes.get(nid)
        if (
            isinstance(node, ColumnNode)
            and not retrieved.labels.get(nid)
            and not node.name.endswith("_id")
            and (node.data_type or "").split("(")[0] in ("numeric", "bigint")
        ):
            measures.append(node.name)
    names = dict.fromkeys(n.replace("_", " ") for n in [*metrics, *measures])
    return list(names)[:4]


# Assumptions say how the question was read. Anything about access, roles or the documentation
# isn't that, and could repeat an injected instruction or hint at what the role can't see.
_META = re.compile(
    r"\b(mask\w*|unmask\w*|access|permission\w*|privilege\w*|roles?|admin|hidden|restricted|"
    r"instruction\w*|documentation|polic(?:y|ies)|set role|a note|note inside|enforced|override|"
    r"context|marts_base)\b",
    re.IGNORECASE,
)


class OutputGuard:
    """Filters the model's free text (assumptions), the only model text a user sees."""

    def __init__(self, access: Access, graph: GraphArtifact) -> None:
        terms: set[str] = set()
        for cid in access.hidden_columns:
            col = graph.nodes[cid]
            assert isinstance(col, ColumnNode)
            terms.add(col.name)
            terms |= {t for t in col.name.split("_") if len(t) > 3}
            if col.pii:
                terms.add(col.pii)
        self.terms = terms - {"first", "last", "name", "annual", "work"}
        self._rx = (
            re.compile(rf"\b(?:{'|'.join(sorted(map(re.escape, self.terms)))})s?\b", re.IGNORECASE)
            if self.terms
            else None
        )

    def clean(self, assumptions: list[str]) -> list[str]:
        kept = [a for a in assumptions if not _META.search(a)]
        return [a for a in kept if self._rx is None or not self._rx.search(a)]


class PolicyMatcher:
    """Did a declined question ask for something hidden from the role? For the audit log and the
    internal mode only: the answer is worded the same either way."""

    def __init__(self, graph: GraphArtifact, embedder: Embedder) -> None:
        self.graph = graph
        self.query = GraphQuery(graph, embedder)

    def hidden_target(self, question: str, access: Access, k: int = 10) -> str | None:
        kinds = {
            (c.pii, c.sensitivity)
            for cid in access.hidden_columns
            if isinstance(c := self.graph.nodes[cid], ColumnNode) and (c.pii or c.sensitivity)
        }
        if not kinds:
            return None
        for hit in self.query.search_nodes(question, types=["column"], k=k, mode="hybrid"):
            col = self.graph.nodes[hit.id]
            if isinstance(col, ColumnNode) and (col.pii, col.sensitivity) in kinds:
                return f"{col.pii or col.sensitivity}"
        return None


def outside_row_scope(sql: str, row_filter: RowFilter | None) -> bool:
    """True if the query explicitly asks for rows the role's row filter excludes."""
    if row_filter is None:
        return False
    try:
        tree = sqlglot.parse_one(sql, dialect="postgres")
    except SqlglotError:
        return False
    for node in tree.find_all(exp.EQ, exp.In):
        columns = {c.name.lower() for c in node.this.find_all(exp.Column)}
        if row_filter.column not in columns or node.find_ancestor(exp.Not, exp.Case):
            continue
        others = node.expressions if isinstance(node, exp.In) else [node.expression]
        literals = [lit.this for o in others for lit in o.find_all(exp.Literal) if lit.is_string]
        if any(v.upper() != row_filter.equals.upper() for v in literals):
            return True
    return False
