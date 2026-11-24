"""Verification (tasks 6.3-6.5): dry run, execute, and exactly five sanity checks.

Checks (PRD §3.5), and what happens when one fails:
1. empty result: 0 rows, or one row of only NULLs. Repair.
2. join fan-out: joined rows > distinct grain keys x 1.05, under a non-distinct SUM, AVG or
   COUNT. Repair.
3. null blow-up: an output column more than 20% NULL (5+ rows). Warn, and repair once.
4. deprecated usage: the SQL reads a node flagged deprecated. Repair, naming the replacement.
5. staleness: a table read has stale upstream data (warehouse-graph D16). Warn.

Checks run as the asking role (row counts respect RLS). They read flags and keys from the
graph internally; nothing from it is shown to the user or the LLM except the check result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import psycopg
import sqlglot
from sqlglot import exp
from warehouse_graph.adapters import PostgresAdapter, Rows, Session
from warehouse_graph.graph.artifact import GraphArtifact
from warehouse_graph.graph.schema import ColumnNode, ModelNode

from warehouse_qa.eval.labels import SqlNodeResolver

FANOUT_RATIO = 1.05
NULL_SHARE = 0.2
STALE = ("warn", "error", "runtime error")

# Problems that send the query back for repair, and whether they may end in an answer anyway.
REPAIR_HARD = ("empty_result", "join_fanout", "deprecated_usage", "error")
REPAIR_SOFT = ("null_blowup",)


@dataclass
class Problem:
    check: str  # empty_result | join_fanout | null_blowup | deprecated_usage | error
    detail: str


@dataclass
class Verification:
    problems: list[Problem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks_run: list[str] = field(default_factory=list)

    @property
    def needs_repair(self) -> bool:
        return any(p.check in (*REPAIR_HARD, *REPAIR_SOFT) for p in self.problems)

    @property
    def hard(self) -> list[Problem]:
        return [p for p in self.problems if p.check in REPAIR_HARD]


class Verifier:
    def __init__(self, graph: GraphArtifact, adapter: PostgresAdapter, resolver: SqlNodeResolver):
        self.graph = graph
        self.adapter = adapter
        self.resolver = resolver
        self.unique_keys: dict[str, str] = {}
        for n in graph.nodes.values():
            if (
                isinstance(n, ColumnNode)
                and "unique" in n.tests
                and n.parent not in self.unique_keys
            ):
                self.unique_keys[n.parent] = n.name

    def check(self, session: Session, sql: str, rows: Rows, timeout_s: int) -> Verification:
        v = Verification()
        nodes = self.resolver.nodes(sql)
        self._empty(rows, v)
        self._fanout(session, sql, timeout_s, v)
        self._nulls(rows, v)
        self._deprecated(nodes, v)
        self._stale(nodes, v)
        return v

    # 1
    def _empty(self, rows: Rows, v: Verification) -> None:
        v.checks_run.append("empty_result")
        if not rows.rows or (len(rows.rows) == 1 and all(x is None for x in rows.rows[0])):
            v.problems.append(
                Problem(
                    "empty_result",
                    "the query returned no rows (or only NULLs). Check filters, joins and dates:"
                    " data runs 2024-01-01 to 2026-09-30. If the asker can't see the rows the"
                    " question needs, say so instead of guessing.",
                )
            )

    # 2
    def _fanout(self, session: Session, sql: str, timeout_s: int, v: Verification) -> None:
        v.checks_run.append("join_fanout")
        try:
            tree = sqlglot.parse_one(sql, dialect="postgres")
        except sqlglot.errors.ParseError:
            return
        if not isinstance(tree, exp.Select) or not tree.args.get("joins"):
            return
        joins = tree.args["joins"]
        # A CROSS JOIN (or a join without a condition) is a deliberate cartesian product, e.g.
        # "count each refund once per category": not the missing-key bug this check is for.
        if all(
            j.args.get("kind", "").upper() == "CROSS"
            or not (j.args.get("on") or j.args.get("using"))
            for j in joins
        ):
            return
        from_ = tree.args.get("from_") or tree.args.get("from")  # renamed in sqlglot 30
        source = from_.this if from_ is not None else None
        if not isinstance(source, exp.Table):
            return
        aggregates = [
            a
            for a in tree.find_all(exp.Sum, exp.Avg, exp.Count)
            if not isinstance(a.this, exp.Distinct)
        ]
        if not aggregates:
            return
        model = self.resolver.relations.get((source.db.lower(), source.name.lower()))
        key = self.unique_keys.get(model or "")
        if key is None:
            return
        alias = source.alias_or_name
        probe = tree.copy()
        for arg in ("group", "having", "order", "limit", "distinct", "qualify"):
            probe.set(arg, None)
        probe.set(
            "expressions",
            [
                sqlglot.parse_one("count(*)", dialect="postgres").as_("n"),
                sqlglot.parse_one(f'count(distinct "{alias}"."{key}")', dialect="postgres").as_(
                    "d"
                ),
            ],
        )
        try:
            result = self.adapter.execute(session, probe.sql(dialect="postgres"), 1, timeout_s)
        except psycopg.Error:
            return
        n, d = result.rows[0] if result.rows else (0, 0)
        if d and n > FANOUT_RATIO * d:
            v.problems.append(
                Problem(
                    "join_fanout",
                    f"the joins repeat rows of {source.name}: {n} joined rows for {d} distinct"
                    f" {key} values, so sums and counts are inflated about {n / d:.2f}x. Join on"
                    " the full key, aggregate before joining, or use EXISTS.",
                )
            )

    # 3
    def _nulls(self, rows: Rows, v: Verification) -> None:
        v.checks_run.append("null_blowup")
        if len(rows.rows) < 5:
            return
        for col in rows.stats().columns:
            if col.null_rate > NULL_SHARE:
                v.problems.append(
                    Problem(
                        "null_blowup",
                        f"column {col.name} is {col.null_rate:.0%} NULL. If that's a bad join or"
                        " filter, fix it; if NULLs are expected here, keep the query.",
                    )
                )
                return

    # 4
    def _deprecated(self, nodes: set[str], v: Verification) -> None:
        v.checks_run.append("deprecated_usage")
        for nid in sorted(nodes):
            node = self.graph.nodes.get(nid)
            if node is not None and node.deprecated:
                instead = f"; use {node.replaced_by} instead" if node.replaced_by else ""
                v.problems.append(Problem("deprecated_usage", f"{nid} is deprecated{instead}."))

    # 5
    def _stale(self, nodes: set[str], v: Verification) -> None:
        v.checks_run.append("staleness")
        for nid in sorted(nodes):
            node = self.graph.nodes.get(nid)
            if isinstance(node, ModelNode) and node.upstream_freshness in STALE:
                note = node.upstream_freshness_note or node.upstream_freshness
                v.warnings.append(f"stale: {nid}: {note}")


def dry_run(adapter: PostgresAdapter, session: Session, sql: str) -> Any:
    """EXPLAIN as the role. Raises psycopg errors (permission errors included) to the caller."""
    return adapter.explain(session, sql)
