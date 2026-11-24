"""Small-cell suppression (task 7.1, PRD §3.4): no group of fewer than 5 people is shown.

A query is in scope when its result is **aggregated by a person dimension**: a GROUP BY key (or,
for an ungrouped aggregate, a filter on a PII column) that reads
    - a PII column (`meta.pii`), masked or not, or
    - another attribute of a customer table (city, sign-up date, ...): quasi-identifiers, which
      single a person out in combination.
Then `HAVING count(distinct <person key>) >= 5` is added to the SELECT that does the grouping, so
suppressed groups never leave the database: not to the user, and not into the stats an LLM sees.

Out of scope, by the labelling protocol (docs/eval.md §3): plain lists of people (one row per
person, or grouped by the person key without PII columns) show what the column policy allows,
with masked values.

If the number of people per group can't be determined, the query is **rejected** (fail closed),
with feedback the repair loop can act on.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope
from warehouse_graph.graph.artifact import GraphArtifact
from warehouse_graph.graph.schema import ColumnNode, ModelNode

from warehouse_qa.eval.labels import SqlNodeResolver

MIN_CELL = 5
UNDETERMINED = (
    "this groups people by personal attributes, and the number of people per group can't be"
    " counted. Group in one step, directly over the tables that hold the person key"
    " (e.g. customer_id), without pre-aggregating it away."
)
NUMERIC = ("bigint", "integer", "numeric")

Base = tuple[str, str]  # (relation node id, column name)


class SmallCellError(ValueError):
    """The query must not run: its group sizes can't be checked. `str(e)` is safe feedback."""


@dataclass(frozen=True)
class SmallCellPlan:
    applies: bool
    sql: str  # with the HAVING added (unchanged when it doesn't apply)
    suppressed_sql: str | None = None  # returns the number of suppressed groups


class SmallCells:
    def __init__(self, graph: GraphArtifact, resolver: SqlNodeResolver) -> None:
        self.resolver = resolver
        columns = [n for n in graph.nodes.values() if isinstance(n, ColumnNode)]
        self.pii: set[Base] = {(c.parent, c.name) for c in columns if c.pii}
        with_pii = {parent for parent, _ in self.pii}
        key_names = {
            c.name
            for c in columns
            if c.parent in with_pii and "unique" in c.tests and c.name.endswith("_id")
        }
        # Where each person key appears (dim_customers.customer_id, fct_orders.customer_id, ...).
        self.keys: set[Base] = {
            (c.parent, c.name)
            for c in columns
            if c.name in key_names and isinstance(graph.nodes.get(c.parent), ModelNode)
        }
        # Attributes of customers, in every table with one row per customer. An employee's store
        # or job title describes the organisation, not the person: only PII columns count there.
        per_customer = {
            c.parent for c in columns if c.name == "customer_id" and "unique" in c.tests
        }
        self.quasi: set[Base] = {
            (c.parent, c.name)
            for c in columns
            if c.parent in per_customer
            and c.name != "customer_id"
            and (c.data_type or "").split("(")[0] not in NUMERIC
        }
        self.person_dims = self.pii | self.quasi

    # -- planning --------------------------------------------------------------------------------

    def plan(self, sql: str) -> SmallCellPlan:
        try:
            tree = qualify(
                _named(sqlglot.parse_one(sql, dialect="postgres")),
                schema=self.resolver.schema,
                dialect="postgres",
                validate_qualify_columns=False,
            )
        except SqlglotError:
            if self._mentions_person_table(sql):  # can't analyse it: fail closed
                raise SmallCellError(UNDETERMINED) from None
            return SmallCellPlan(False, sql)
        root = build_scope(tree)
        if root is None:
            return SmallCellPlan(False, sql)
        targets: list[tuple[exp.Select, str]] = []
        for scope in self._deciding(root):
            key = self._person_key(scope)
            if key is not None and isinstance(scope.expression, exp.Select):
                targets.append((scope.expression, key))
        if not targets:
            return SmallCellPlan(False, sql)
        suppressed_sql = None
        if len(targets) == 1:
            select, key = targets[0]
            having = select.args.get("having")
            select.having(f"count(distinct {key}) < {MIN_CELL}", copy=False, dialect="postgres")
            inner = tree.copy()
            inner.set("limit", None)
            suppressed_sql = f"select count(*) from ({inner.sql(dialect='postgres')}) as s"
            select.set("having", having)
        for select, key in targets:
            select.having(f"count(distinct {key}) >= {MIN_CELL}", copy=False, dialect="postgres")
        return SmallCellPlan(True, tree.sql(dialect="postgres"), suppressed_sql)

    def _mentions_person_table(self, sql: str) -> bool:
        lowered = sql.lower()
        return any(p.split(".", 1)[1] in lowered for p, _ in self.person_dims | self.keys)

    def _deciding(self, scope: Scope) -> list[Scope]:
        """The SELECTs whose grouping the user sees: the outermost aggregating ones."""
        if scope.set_operation_scopes:
            return [s for u in scope.set_operation_scopes for s in self._deciding(u)]
        select = scope.expression
        if not isinstance(select, exp.Select):
            return []
        self._reject_hidden_aggregates(scope)
        if select.args.get("group") or any(_aggregates(e) for e in select.expressions):
            return [scope]
        return [
            s
            for source in scope.sources.values()
            if isinstance(source, Scope)
            for s in self._deciding(source)
        ]

    def _reject_hidden_aggregates(self, scope: Scope) -> None:
        """Aggregates by person dimensions that HAVING can't reach: windows and subqueries."""
        select = scope.expression
        for projection in select.expressions:
            for window in projection.find_all(exp.Window):
                if not isinstance(window.this, exp.AggFunc):
                    continue  # ranking functions aggregate nothing
                self._reject_if_personal(
                    {
                        b
                        for p in window.args.get("partition_by") or []
                        for b in self._bases(p, scope)
                    }
                )
        for sub in scope.subquery_scopes:
            inner = sub.expression
            if not isinstance(inner, exp.Select) or not any(
                _aggregates(e) for e in inner.expressions
            ):
                continue
            where = inner.args.get("where")
            # A correlated scalar aggregate is a GROUP BY in disguise.
            if sub.is_correlated_subquery and where is not None:
                self._reject_if_personal(self._bases(where, sub))

    def _reject_if_personal(self, dims: set[Base]) -> None:
        if dims & self.person_dims and not dims & self.keys:
            raise SmallCellError(UNDETERMINED)

    def _person_key(self, scope: Scope) -> str | None:
        """SQL for the person key to count, or None when the rule doesn't apply here."""
        select = scope.expression
        group = select.args.get("group")
        if group:
            dims = self._bases(group, scope)
        else:  # an ungrouped total is one cell; only a filter on PII narrows it to people
            where = select.args.get("where")
            dims = (self._bases(where, scope) if where is not None else set()) & self.pii
        if not dims & self.person_dims:
            return None
        if dims & self.keys and not dims & self.pii:
            return None  # one row per person and no PII column: a list
        for alias, source in scope.sources.items():
            for name in self._names(source):
                col = exp.column(name, table=alias, quoted=True)
                if self._bases(col, scope) & self.keys:
                    return col.sql(dialect="postgres")
        raise SmallCellError(UNDETERMINED)

    def _names(self, source: exp.Expr | Scope) -> list[str]:
        if isinstance(source, Scope):
            return [str(p.alias_or_name) for p in _selects(source)]
        if not isinstance(source, exp.Table):
            return []
        rel = self.resolver.relations.get((source.db.lower(), source.name.lower()))
        return [c for (p, c) in sorted(self.keys) if p == rel]

    def _bases(self, node: exp.Expr, scope: Scope) -> set[Base]:
        """The warehouse columns an expression reads, through CTEs and subqueries. A column that
        is an aggregate of a subquery has no base: it describes many rows, not a person."""
        out: set[Base] = set()
        for col in node.find_all(exp.Column):
            source = scope.sources.get(col.table)
            if source is None and not col.table and len(scope.sources) == 1:
                source = next(iter(scope.sources.values()))
            if isinstance(source, exp.Table):
                rel = self.resolver.relations.get((source.db.lower(), source.name.lower()))
                if rel:
                    out.add((rel, col.name.lower()))
            elif isinstance(source, Scope):
                for inner in source.set_operation_scopes or [source]:
                    for projection in _selects(inner):
                        if projection.alias_or_name.lower() == col.name.lower() and not (
                            _aggregates(projection)
                        ):
                            out |= self._bases(projection, inner)
        return out


def _named(tree: exp.Expr) -> exp.Expr:
    """Give unnamed outputs the name Postgres would (sum, count), not the qualifier's `_col_2`."""
    if isinstance(tree, exp.Select):
        taken: set[str] = set()
        named = []
        for e in tree.expressions:
            if isinstance(e, exp.Func) and e.sql_name().lower() not in taken:
                taken.add(e.sql_name().lower())
                e = exp.alias_(e, e.sql_name().lower())
            named.append(e)
        tree.set("expressions", named)
    return tree


def _selects(scope: Scope) -> list[exp.Expr]:
    return list(scope.expression.selects) if isinstance(scope.expression, exp.Query) else []


def _aggregates(e: exp.Expr) -> bool:
    """True if `e` contains an aggregate call of its own SELECT (not a window, not a subquery)."""
    if isinstance(e, exp.Window | exp.Subquery | exp.Select):
        return False
    if isinstance(e, exp.AggFunc):
        return True
    return any(_aggregates(child) for child in e.iter_expressions())
