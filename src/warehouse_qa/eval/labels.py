"""Which graph nodes a SQL query reads: tables (models/sources) and the columns it uses.

Used twice:
- labelling: the derived part of every question's gold nodes (`warehouse-qa eval label` writes
  them to evals/gold_nodes.json; the hand-written metric/caveat extras stay in questions.yaml)
- scoring: which nodes a system's SQL actually read (trap avoidance, deprecated usage)

Columns are resolved with sqlglot's qualifier against the schema in the graph, so `o.order_id`
maps to `column.fct_orders.order_id` through the alias. Unresolvable references are skipped,
never guessed.
"""

from __future__ import annotations

import json
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope
from warehouse_graph.governance.policies import relation_parts
from warehouse_graph.graph.artifact import GraphArtifact
from warehouse_graph.graph.schema import ColumnNode, ModelNode, SourceNode

from warehouse_qa.eval.schema import write_text_lf

GOLD_NODES = Path("evals/gold_nodes.json")


class SqlNodeResolver:
    def __init__(self, graph: GraphArtifact) -> None:
        self.relations: dict[tuple[str, str], str] = {}
        tables: dict[str, dict[str, dict[str, str]]] = {}
        self.schema: dict[str, object] = {}
        for n in graph.nodes.values():
            if isinstance(n, ModelNode | SourceNode):
                _, schema, table = relation_parts(n.relation)
                self.relations[(schema, table)] = n.id
                tables.setdefault(schema, {})[table] = {}
        for n in graph.nodes.values():
            if isinstance(n, ColumnNode):
                parent = graph.nodes[n.parent]
                assert isinstance(parent, ModelNode | SourceNode)
                _, schema, table = relation_parts(parent.relation)
                tables[schema][table][n.name] = n.data_type or "text"
        self.schema.update(tables)
        self.columns = {n.id for n in graph.nodes.values() if isinstance(n, ColumnNode)}

    def nodes(self, sql: str) -> set[str]:
        """Relation and column node ids the query reads (empty if it doesn't parse)."""
        try:
            tree = sqlglot.parse_one(sql, dialect="postgres")
            tree = qualify(
                tree, schema=self.schema, dialect="postgres", validate_qualify_columns=False
            )
        except SqlglotError:
            return set()
        found: set[str] = set()
        for scope in traverse_scope(tree):
            found |= self._scope_nodes(scope)
        return found

    def _scope_nodes(self, scope: Scope) -> set[str]:
        found: set[str] = set()
        tables: dict[str, str] = {}  # alias -> relation node id
        for alias, source in scope.sources.items():
            if isinstance(source, exp.Table):
                rel = self.relations.get((source.db.lower(), source.name.lower()))
                if rel:
                    tables[alias] = rel
                    found.add(rel)
        for col in scope.columns:
            rel = tables.get(col.table)
            if rel is None and len(tables) == 1:
                rel = next(iter(tables.values()))
            if rel is not None:
                cid = f"column.{rel.split('.', 1)[1]}.{col.name}"
                if cid in self.columns:
                    found.add(cid)
        return found


def load_gold_nodes(path: Path = GOLD_NODES) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    data: dict[str, list[str]] = json.loads(path.read_text(encoding="utf-8"))
    return data


def derive_gold_nodes(sqls: dict[str, str], resolver: SqlNodeResolver) -> dict[str, list[str]]:
    return {qid: sorted(resolver.nodes(sql)) for qid, sql in sorted(sqls.items())}


def write_gold_nodes(nodes: dict[str, list[str]], path: Path = GOLD_NODES) -> None:
    write_text_lf(path, json.dumps(nodes, indent=1, sort_keys=True) + "\n")
