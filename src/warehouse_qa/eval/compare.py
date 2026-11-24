"""Execution accuracy: does a predicted result equal the gold result? (docs/eval.md)

Rules:
- Rows are a multiset: order is ignored, duplicates count.
- Column names and order are ignored. Every gold column must match a distinct predicted column
  (extra predicted columns are allowed), and the rows must match under that mapping.
- Numbers match within abs 0.005 or rel 5e-4, so a value rounded to 2 decimals still matches.
  A fraction may also come back as a percentage (x100).
- Decimal/float/int compare as numbers; dates and timestamps compare by ISO date; booleans
  only equal booleans; NULL equals NULL.
- A period's start date also matches its month, quarter or year label ('2025-10', 'Q4 2025',
  '2025'), because how a system labels a period isn't what's being tested.
"""

from __future__ import annotations

import datetime as dt
import itertools
import re
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

ABS_TOL = 0.005
REL_TOL = 5e-4
MAX_MAPPINGS = 5000
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

Value = Any


def normalize(v: Value) -> Value:
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, Decimal | int | float):
        return float(v)
    if isinstance(v, dt.datetime):
        return v.date().isoformat() if v.time() == dt.time() else v.isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    return str(v).strip()


def _num_equal(a: float, b: float) -> bool:
    return abs(a - b) <= ABS_TOL or abs(a - b) <= REL_TOL * max(abs(a), abs(b))


def period_labels(iso: str) -> set[str]:
    """Ways a period starting on `iso` may be written: month, quarter or year labels."""
    if not _ISO_DATE.match(iso) or not iso.endswith("-01"):
        return set()
    year, month = iso[:4], int(iso[5:7])
    labels = {iso[:7]}
    if month in (1, 4, 7, 10):
        q = (month - 1) // 3 + 1
        labels |= {f"{year}-Q{q}", f"Q{q} {year}", f"{year} Q{q}", f"Q{q}-{year}", f"{year}Q{q}"}
    if month == 1:
        labels.add(year)
    return labels


def values_equal(gold: Value, pred: Value, scale: float = 1.0) -> bool:
    if isinstance(gold, bool) or isinstance(pred, bool):
        return type(gold) is type(pred) and gold == pred
    if isinstance(gold, str) and isinstance(pred, str) and gold != pred:
        return pred.upper() in {label.upper() for label in period_labels(gold)}
    if isinstance(gold, float) and isinstance(pred, float) and not isinstance(pred, bool):
        return _num_equal(gold * scale, pred)
    return bool(gold == pred)


def _sort_key(v: Value) -> tuple[int, Any]:
    if v is None:
        return (0, 0)
    if isinstance(v, bool):
        return (1, v)
    if isinstance(v, float):
        return (2, v)
    return (3, str(v))


def _column(rows: Sequence[Sequence[Value]], i: int) -> list[Value]:
    return sorted((r[i] for r in rows), key=_sort_key)


def _scales(gold_col: list[Value]) -> list[float]:
    nums = [v for v in gold_col if isinstance(v, float)]
    if nums and all(-1.0 <= v <= 1.0 for v in nums):
        return [1.0, 100.0]
    return [1.0]


def _column_matches(gold_col: list[Value], pred_col: list[Value]) -> list[float]:
    """Scales under which two columns hold the same multiset of values."""
    return [
        s
        for s in _scales(gold_col)
        if all(values_equal(g, p, s) for g, p in zip(gold_col, pred_col, strict=True))
    ]


def results_match(
    gold_rows: Sequence[Sequence[Value]], pred_rows: Sequence[Sequence[Value]]
) -> bool:
    gold = [tuple(normalize(v) for v in r) for r in gold_rows]
    pred = [tuple(normalize(v) for v in r) for r in pred_rows]
    if len(gold) != len(pred):
        return False
    if not gold:
        return True
    n_gold, n_pred = len(gold[0]), len(pred[0])
    if n_pred < n_gold:
        return False

    gold_cols = [_column(gold, i) for i in range(n_gold)]
    pred_cols = [_column(pred, j) for j in range(n_pred)]
    candidates: list[list[tuple[int, float]]] = []
    for gc in gold_cols:
        options = [(j, s) for j, pc in enumerate(pred_cols) for s in _column_matches(gc, pc)]
        if not options:
            return False
        candidates.append(options)

    for count, choice in enumerate(itertools.product(*candidates)):
        if count >= MAX_MAPPINGS:
            return False
        if len({j for j, _ in choice}) != len(choice):
            continue
        projected = sorted(
            (tuple(r[j] for j, _ in choice) for r in pred),
            key=lambda row: [_sort_key(v) for v in row],
        )
        scaled_gold = sorted(gold, key=lambda row: [_sort_key(v) for v in row])
        if all(
            all(values_equal(g, p, s) for g, p, (_, s) in zip(gr, pr, choice, strict=True))
            for gr, pr in zip(scaled_gold, projected, strict=True)
        ):
            return True
    return False
