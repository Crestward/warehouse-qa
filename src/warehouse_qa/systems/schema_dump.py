"""Config 1, the naive baseline (PRD §3.7, task 4.10): the whole role-visible schema in context.

One LLM call per question, no retrieval, no verification, no gates:

    role-visible DDL (from the role-filtered graph) + question -> {sql, assumptions}
    -> single-SELECT check -> execute AS the role -> answer

The schema dump is identical for every question of a role, so it sits in the cached system
prefix (PRD §5.2 lever 12). It still goes through role-filtered metadata: every config gets the
same governance, so baselines can't leak and comparisons stay fair (PRD §3.7).
Phase 6 replaces this thin path with the full pipeline.
"""

from __future__ import annotations

import json
import re
from typing import Any

from warehouse_graph.governance.metadata import filter_graph
from warehouse_graph.governance.policies import PolicySpec, relation_parts
from warehouse_graph.graph.artifact import GraphArtifact
from warehouse_graph.graph.schema import ColumnNode, ModelNode
from warehouse_graph.llm import LLMRequest, LLMResponse

AS_OF = "2026-09-30"

INSTRUCTIONS = f"""You answer questions about the Kiwi Retail Co. data warehouse (PostgreSQL) by
writing one SQL query. The data runs to {AS_OF}; treat that date as today.

Rules:
- Write exactly one read-only SELECT statement for PostgreSQL, using only the tables below,
  always schema-qualified (marts.<table>).
- If the question can't be answered from these tables, or needs data you weren't given, don't
  guess: return "sql": null with a short reason.
- If you had to interpret the question (a definition, a time range, a default), list each
  interpretation in "assumptions".

Reply with JSON only, no prose:
{{"sql": "<query or null>", "tables_used": ["marts.<table>", ...],
 "assumptions": ["..."], "reason": "<only when sql is null>"}}

Tables you can query:
"""


def schema_dump(graph: GraphArtifact, spec: PolicySpec, role: str) -> str:
    """CREATE TABLE statements for every marts relation the role can query, in name order."""
    visible = filter_graph(graph, spec.access[role], None)
    columns: dict[str, list[ColumnNode]] = {}
    for n in visible.nodes.values():
        if isinstance(n, ColumnNode):
            columns.setdefault(n.parent, []).append(n)
    blocks: list[str] = []
    models = sorted(
        (n for n in visible.nodes.values() if isinstance(n, ModelNode)),
        key=lambda m: m.name,
    )
    for m in models:
        _, schema, table = relation_parts(m.relation)
        if schema != "marts":
            continue
        lines = [f"-- {' '.join(m.description.split())}"] if m.description else []
        lines.append(f"create table {schema}.{table} (")
        cols = columns.get(m.id, [])
        for i, c in enumerate(cols):
            comma = "," if i < len(cols) - 1 else ""
            desc = " ".join(c.description.split())
            comment = f"  -- {desc}" if desc else ""
            lines.append(f"  {c.name} {c.data_type or 'text'}{comma}{comment}")
        lines.append(");")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


class SchemaDumpSystem:
    name = "schema_dump"

    def __init__(
        self,
        graph: GraphArtifact,
        spec: PolicySpec,
        *,
        model: str,
        max_tokens: int,
        effort: Any,
    ) -> None:
        self.graph = graph
        self.spec = spec
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self._systems: dict[str, str] = {}

    def system_prompt(self, role: str) -> str:
        if role not in self._systems:
            self._systems[role] = INSTRUCTIONS + "\n" + schema_dump(self.graph, self.spec, role)
        return self._systems[role]

    def request(self, role: str, question: str) -> LLMRequest:
        return LLMRequest(
            model=self.model,
            system=self.system_prompt(role),
            messages=[{"role": "user", "content": question}],
            max_tokens=self.max_tokens,
            effort=self.effort,
            call_site="p1.config1.sql_gen",
        )


_JSON = re.compile(r"\{.*\}", re.DOTALL)


def parse_reply(response: LLMResponse) -> dict[str, Any]:
    """The model's JSON reply; a reply that isn't JSON counts as no SQL."""
    match = _JSON.search(response.text)
    if not match:
        return {"sql": None, "reason": "reply was not JSON"}
    try:
        data: dict[str, Any] = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"sql": None, "reason": "reply was not valid JSON"}
    return data
