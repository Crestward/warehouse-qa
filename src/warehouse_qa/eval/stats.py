"""Uncertainty and paired comparisons (PRD §3.7): bootstrap CIs and exact McNemar."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np


def bootstrap_ci(
    values: Sequence[float], *, level: float = 0.95, n: int = 2000, seed: int = 0
) -> tuple[float, float, float]:
    """(mean, low, high) by percentile bootstrap over questions. Empty input -> NaNs."""
    if not values:
        return (math.nan, math.nan, math.nan)
    data = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    means = rng.choice(data, size=(n, len(data)), replace=True).mean(axis=1)
    alpha = (1 - level) / 2
    low, high = np.quantile(means, [alpha, 1 - alpha])
    return (float(data.mean()), float(low), float(high))


def mcnemar(a_correct: Sequence[bool], b_correct: Sequence[bool]) -> tuple[int, int, float]:
    """Exact two-sided McNemar test on paired outcomes.

    Returns (b, c, p): b = A right and B wrong, c = A wrong and B right. Only the discordant
    pairs carry information; under H0 they split 50/50, so p is a two-sided binomial test.
    """
    if len(a_correct) != len(b_correct):
        raise ValueError("paired outcomes must have the same length")
    b = sum(1 for x, y in zip(a_correct, b_correct, strict=True) if x and not y)
    c = sum(1 for x, y in zip(a_correct, b_correct, strict=True) if y and not x)
    n = b + c
    if n == 0:
        return (b, c, 1.0)
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return (b, c, min(1.0, 2 * tail))
