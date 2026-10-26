"""Reranking: a local cross-encoder on CPU (PRD §7: no reranking API, no cost).

`CrossEncoderReranker` scores (question, node text) pairs with
cross-encoder/ms-marco-MiniLM-L-6-v2. Unit tests use `OverlapReranker`, which needs no download.
Scores are memoised per (question, text), so parameter sweeps re-use them.
"""

from __future__ import annotations

import math
import re
from functools import cache
from typing import Protocol

DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


class Reranker(Protocol):
    name: str

    def scores(self, question: str, texts: list[str]) -> list[float]:
        """Relevance in [0, 1], one per text."""
        ...


@cache
def _load(name: str):  # type: ignore[no-untyped-def]  # torch types stay out of mypy
    from sentence_transformers import CrossEncoder

    return CrossEncoder(name, device="cpu")


class CrossEncoderReranker:
    def __init__(self, name: str = DEFAULT_MODEL) -> None:
        self.name = name
        self._memo: dict[tuple[str, str], float] = {}

    def scores(self, question: str, texts: list[str]) -> list[float]:
        todo = [t for t in dict.fromkeys(texts) if (question, t) not in self._memo]
        if todo:
            logits = _load(self.name).predict([(question, t) for t in todo], batch_size=64)
            for t, logit in zip(todo, logits, strict=True):
                self._memo[(question, t)] = 1 / (1 + math.exp(-float(logit)))
        return [self._memo[(question, t)] for t in texts]


class OverlapReranker:
    """Word-overlap stand-in for tests: deterministic, no model download."""

    name = "overlap"

    def scores(self, question: str, texts: list[str]) -> list[float]:
        q = set(re.findall(r"[a-z0-9]+", question.lower()))
        out = []
        for t in texts:
            w = set(re.findall(r"[a-z0-9]+", t.lower()))
            out.append(len(q & w) / len(q | w) if q | w else 0.0)
        return out
