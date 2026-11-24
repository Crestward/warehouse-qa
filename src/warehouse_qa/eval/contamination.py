"""Contamination check (task 4.6, PRD §2.2 "QueryExample hygiene").

Retrieval mustn't be able to look up a test answer. Every eval question and red-team prompt is
compared with every QueryExample question and caveat text:
- semantic: cosine similarity of the graph's own local embeddings (same model as retrieval)
- lexical: Jaccard overlap of content words
and with each other (near-duplicate eval questions inflate one behaviour's weight).
Hits above the thresholds are listed for manual review; the gate is "no unreviewed hits".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from warehouse_graph.graph.artifact import GraphArtifact
from warehouse_graph.graph.embed import Embedder, Matrix
from warehouse_graph.graph.schema import CaveatNode, QueryExampleNode

SEMANTIC_THRESHOLD = 0.85
LEXICAL_THRESHOLD = 0.6
_STOP = {
    *[
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "by",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "in",
        "is",
        "it",
        "many",
        "much",
    ],
    *[
        "of",
        "on",
        "or",
        "our",
        "per",
        "the",
        "their",
        "this",
        "to",
        "was",
        "we",
        "were",
        "what",
        "which",
        "who",
        "with",
    ],
}


@dataclass(frozen=True)
class Hit:
    item: str
    other: str
    cosine: float
    jaccard: float
    item_text: str
    other_text: str


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP}


def jaccard(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if wa | wb else 0.0


def find_hits(
    items: dict[str, str], graph: GraphArtifact, embedder: Embedder
) -> tuple[list[Hit], list[Hit]]:
    """(hits against QueryExamples/caveats, near-duplicates within the eval items)."""
    refs = {
        n.id: n.description
        for n in graph.nodes.values()
        if isinstance(n, QueryExampleNode | CaveatNode)
    }
    item_ids, ref_ids = sorted(items), sorted(refs)
    iv = embedder.embed([items[i] for i in item_ids])
    rv = embedder.embed([refs[r] for r in ref_ids])
    against = _pairs(item_ids, items, iv, ref_ids, refs, rv, same=False)
    within = _pairs(item_ids, items, iv, item_ids, items, iv, same=True)
    return against, within


def _pairs(
    a_ids: list[str],
    a_text: dict[str, str],
    av: Matrix,
    b_ids: list[str],
    b_text: dict[str, str],
    bv: Matrix,
    *,
    same: bool,
) -> list[Hit]:
    sims = av @ bv.T
    hits: list[Hit] = []
    for i, a in enumerate(a_ids):
        for j, b in enumerate(b_ids):
            if same and j <= i:
                continue
            cos = float(sims[i, j])
            jac = jaccard(a_text[a], b_text[b])
            if cos >= SEMANTIC_THRESHOLD or jac >= LEXICAL_THRESHOLD:
                hits.append(Hit(a, b, round(cos, 3), round(jac, 3), a_text[a], b_text[b]))
    return sorted(hits, key=lambda h: (-h.cosine, h.item, h.other))
