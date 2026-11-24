"""Context packer (task 5.4): retrieved nodes -> the token-budgeted text an LLM sees.

Order: answer-level warnings first, then nodes in rank order, each table with its columns
(type, description, masked flag). Deprecated, trap, non-canonical and stale nodes are kept
**with their warning label** rather than dropped, so the model can say why it avoided them.
Every piece of documentation text is passed as data inside a <data> block, never as instructions,
and can't close its block: angle brackets in it are escaped (task 7.2).

Tokens are estimated as characters / 4; the same estimator is applied to config 1's schema
dump, so the two context sizes are comparable.
"""

from __future__ import annotations

from dataclasses import dataclass

from warehouse_qa.retrieval.retrievers import Retrieved
from warehouse_qa.retrieval.tools import GraphTools

DEFAULT_BUDGET = 3000


def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


@dataclass
class PackedContext:
    text: str
    tokens: int
    included: list[str]  # node ids that made it into the text
    dropped: list[str]  # retrieved but over budget


def _one_line(text: str, limit: int = 300) -> str:
    return " ".join((text or "").split())[:limit]


def _data(text: str, limit: int = 300) -> str:
    """Documentation text as delimited data. It can't contain (or fake) a delimiter."""
    return f"<data>{_one_line(text, limit).replace('<', '&lt;').replace('>', '&gt;')}</data>"


async def pack(
    retrieved: Retrieved, tools: GraphTools, budget: int = DEFAULT_BUDGET
) -> PackedContext:
    parts: list[str] = []
    if retrieved.warnings:
        parts.append("WARNINGS:\n" + "\n".join(f"- {w}" for w in retrieved.warnings))
    included: list[str] = []
    dropped: list[str] = []
    seen_columns: set[str] = set()
    used = estimate_tokens("\n\n".join(parts))

    for nid in retrieved.nodes:
        if nid in seen_columns:
            continue
        block = await _block(nid, retrieved, tools, seen_columns)
        if block is None:
            continue
        cost = estimate_tokens(block) + 1
        if used + cost > budget:
            dropped.append(nid)
            continue
        parts.append(block)
        included.append(nid)
        used += cost
    text = "\n\n".join(parts)
    return PackedContext(
        text=text, tokens=estimate_tokens(text), included=included, dropped=dropped
    )


async def _block(
    nid: str, retrieved: Retrieved, tools: GraphTools, seen_columns: set[str]
) -> str | None:
    labels = retrieved.labels.get(nid, [])
    tag = f"  [WARNING: {'; '.join(labels)}]" if labels else ""
    kind = nid.split(".", 1)[0]
    if kind in ("model", "source"):
        rec = await tools.node(nid)
        if rec is None:
            return None
        node = rec["node"]
        lines = [f"TABLE {node['relation']}{tag}", _data(node["description"])]
        for col in rec["columns"]:
            seen_columns.add(col["id"])
            full = await tools.node(col["id"])
            c = (full or {}).get("node", {})
            flags = []
            if c.get("masked"):
                flags.append("masked")
            if c.get("deprecated"):
                flags.append(f"deprecated, use {c.get('replaced_by')}")
            flag = f" [{'; '.join(flags)}]" if flags else ""
            desc = _data(c["description"], 120) if c.get("description") else ""
            lines.append(
                f"  {col['name']} {c.get('data_type') or ''}{flag}" + (f" {desc}" if desc else "")
            )
        return "\n".join(lines)
    if kind == "column":
        rec = await tools.node(nid)
        if rec is None:
            return None
        c = rec["node"]
        head = f"COLUMN {c['parent']}.{c['name']} {c.get('data_type') or ''}{tag}"
        return f"{head}\n{_data(c['description'])}"
    if kind == "metric":
        rec = await tools.metric(nid)
        if rec is None:
            return None
        m = rec["metric"]
        canonical = "canonical" if m.get("canonical") else "NOT canonical"
        return (
            f"METRIC {m['name']} ({canonical}){tag}\n{_data(m['description'])}\n"
            f"  definition: {m['expression']}\n  sql: {_one_line(rec['sql'], 400)}"
        )
    if kind == "caveat":
        rec = await tools.node(nid)
        if rec is None:
            return None
        c = rec["node"]
        head = f"CAVEAT {c['name']} (severity {c['severity']})"
        return f"{head}\n{_data(c['description'], 500)}"
    if kind == "query_example":
        rec = await tools.node(nid)
        if rec is None:
            return None
        q = rec["node"]
        return f"EXAMPLE {_data(q['description'])}\n  sql: {_one_line(q['sql'], 600)}"
    if kind == "exposure":
        summary = retrieved.summaries.get(nid, {})
        return f"DASHBOARD {summary.get('name', nid)}{tag}"
    return None
