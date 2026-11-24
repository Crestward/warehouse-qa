"""Fixed dev/test splits (task 4.7): 40/60, stratified, committed with hashes.

Strata are (category, expected_mode), so traps, refusals, clarify and abstain questions all
land in both splits in proportion. dev-mini is a stratified 30 from dev for day-to-day work.
Splitting is seeded and deterministic; `eval split` refuses to overwrite committed splits
unless forced, because tuning on test or reshuffling would invalidate results (PRD §3.6).
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path

from warehouse_qa.eval.schema import (
    QUESTIONS,
    REDTEAM,
    EvalQuestion,
    file_sha256,
    write_text_lf,
)

SPLIT_DIR = Path("evals/splits")
SEED = 20261026
DEV_SHARE = 0.4
DEV_MINI = 30
SPLITS = ("dev", "test", "dev_mini")


def _stratified(items: list[EvalQuestion], n: int, rng: random.Random) -> list[EvalQuestion]:
    """Pick n items, each stratum contributing in proportion (largest remainder)."""
    strata: dict[tuple[str, str], list[EvalQuestion]] = defaultdict(list)
    for q in sorted(items, key=lambda q: q.id):
        strata[(q.category, q.expected_mode)].append(q)
    quotas = {k: len(v) * n / len(items) for k, v in strata.items()}
    counts = {k: int(v) for k, v in quotas.items()}
    for k in sorted(quotas, key=lambda k: (-(quotas[k] - counts[k]), k))[
        : n - sum(counts.values())
    ]:
        counts[k] += 1
    picked: list[EvalQuestion] = []
    for k in sorted(strata):
        group = strata[k][:]
        rng.shuffle(group)
        picked += group[: counts[k]]
    return sorted(picked, key=lambda q: q.id)


def make_splits(questions: list[EvalQuestion], seed: int = SEED) -> dict[str, list[str]]:
    rng = random.Random(seed)
    dev = _stratified(questions, round(len(questions) * DEV_SHARE), rng)
    dev_ids = {q.id for q in dev}
    test = [q for q in questions if q.id not in dev_ids]
    mini = _stratified(dev, DEV_MINI, rng)
    return {
        "dev": [q.id for q in dev],
        "test": sorted(q.id for q in test),
        "dev_mini": [q.id for q in mini],
    }


def _ids_hash(ids: list[str]) -> str:
    return hashlib.sha256("\n".join(ids).encode()).hexdigest()


def write_splits(splits: dict[str, list[str]], directory: Path = SPLIT_DIR) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, ids in splits.items():
        write_text_lf(directory / f"{name}.txt", "\n".join(ids) + "\n")
    manifest = {
        "seed": SEED,
        "questions_sha256": file_sha256(QUESTIONS),
        "redteam_sha256": file_sha256(REDTEAM),
        "splits": {name: {"n": len(ids), "sha256": _ids_hash(ids)} for name, ids in splits.items()},
    }
    write_text_lf(directory / "manifest.json", json.dumps(manifest, indent=2) + "\n")


def load_split(name: str, directory: Path = SPLIT_DIR) -> list[str]:
    if name not in SPLITS:
        raise ValueError(f"unknown split {name}; one of {SPLITS}")
    ids = (directory / f"{name}.txt").read_text(encoding="utf-8").split()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if _ids_hash(ids) != manifest["splits"][name]["sha256"]:
        raise ValueError(f"split {name} doesn't match its committed hash")
    return ids


def verify_manifest(directory: Path = SPLIT_DIR) -> list[str]:
    """Problems with the committed splits (empty = all good)."""
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    problems = []
    if manifest["questions_sha256"] != file_sha256(QUESTIONS):
        problems.append("questions.yaml changed after the splits were frozen")
    if manifest["redteam_sha256"] != file_sha256(REDTEAM):
        problems.append("redteam.yaml changed after the splits were frozen")
    for name in SPLITS:
        try:
            load_split(name, directory)
        except ValueError as e:
            problems.append(str(e))
    return problems
