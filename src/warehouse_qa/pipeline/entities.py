"""Named things in a question ("the Queen St store", "the Black Friday campaign") -> rows (7.5).

Gate A's "unresolved entity" signal: a singular reference should match exactly one row of its
dimension table. Values are read **as the asking role**, so a store the role can't see simply
isn't there, and the options offered never include one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from warehouse_graph.adapters import WarehouseAdapter, WarehouseError
from warehouse_graph.graph.artifact import GraphArtifact
from warehouse_graph.graph.schema import ColumnNode, ModelNode

_PROPER = r"[A-Z][\w'&.-]*"
_NOT_NAMES = {
    "Which", "What", "Each", "Every", "Any", "Our", "The", "This", "That", "Per", "How", "Online",
    "Is", "Are", "Does", "Did", "List", "Show", "A", "An",
}  # fmt: skip
MAX_OPTIONS = 6


@dataclass
class Dimension:
    noun: str  # store, campaign, product
    relation: str
    name_column: str
    place_column: str | None


@dataclass
class Mention:
    noun: str
    text: str
    matches: list[str]  # names, empty = unresolved
    known: list[str] = field(default_factory=list)  # for an unresolved one: what does exist

    def to_signal(self) -> dict[str, Any]:
        return {"noun": self.noun, "text": self.text, "matches": self.matches, "known": self.known}


def dimensions(graph: GraphArtifact) -> list[Dimension]:
    """dim_<noun>s tables with a non-personal <noun>_name column."""
    out = []
    for n in graph.nodes.values():
        if not isinstance(n, ModelNode) or not n.name.startswith("dim_"):
            continue
        noun = n.name.removeprefix("dim_").removesuffix("s")
        name = graph.nodes.get(f"column.{n.name}.{noun}_name")
        city = graph.nodes.get(f"column.{n.name}.city")
        if isinstance(name, ColumnNode) and not name.pii:
            out.append(Dimension(noun, n.name, name.name, "city" if city else None))
    return out


class Entities:
    def __init__(self, dims: list[Dimension], adapter: WarehouseAdapter) -> None:
        self.dims = {d.noun: d for d in dims}
        self.adapter = adapter
        self._values: dict[tuple[str, str], list[tuple[str, str]]] = {}
        nouns = "|".join(sorted(self.dims))
        self._mention = re.compile(rf"((?:{_PROPER}\s+)+)({nouns})\b(?!s)")

    def values(self, noun: str, session: Any) -> list[tuple[str, str]]:
        key = (session.principal.name, noun)
        if key not in self._values:
            d = self.dims[noun]
            place = f"coalesce({d.place_column}::text, '')" if d.place_column else "''"
            try:
                rows = self.adapter.execute(
                    session,
                    f"select distinct {d.name_column}, {place} from marts.{d.relation}",
                    1000,
                    30,
                ).rows
            except WarehouseError:
                rows = []
            self._values[key] = sorted((str(a), str(b)) for a, b in rows)
        return self._values[key]

    def unclear(self, question: str, session: Any) -> Mention | None:
        """The first singular mention that doesn't identify exactly one row."""
        for m in self._mention.finditer(question):
            words = [w for w in m.group(1).split() if w not in _NOT_NAMES]
            noun = m.group(2)
            if not words:
                continue
            values = self.values(noun, session)
            if not values:
                continue
            tokens = [w.lower() for w in words]
            hits = [v for v in values if all(t in v[0].lower() for t in tokens)]
            # "the Sydney store" is the one named Sydney, if any, not every one located there
            hits = hits or [v for v in values if all(t in " ".join(v).lower() for t in tokens)]
            hits = _narrow(hits, tokens, question)
            if len(hits) != 1:
                return Mention(
                    noun,
                    " ".join(words),
                    [name for name, _ in hits][:MAX_OPTIONS],
                    [] if hits else [name for name, _ in values][:MAX_OPTIONS],
                )
        return None


def _narrow(hits: list[tuple[str, str]], tokens: list[str], question: str) -> list[tuple[str, str]]:
    """Use the rest of the question ("... in Brisbane", "... 2025") to pick between matches."""
    asked = set(re.findall(r"\w+", question.lower())) - set(tokens)
    extra = [len(asked & set(re.findall(r"\w+", f"{n} {p}".lower()))) for n, p in hits]
    if not hits or max(extra) == 0:
        return hits
    return [h for h, e in zip(hits, extra, strict=True) if e == max(extra)]
