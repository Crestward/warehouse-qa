"""The PR eval gate (task 8.5): a run on the 20-question `ci` split against a committed baseline.

Fails on any leak, or when execution accuracy drops by more than one question. One question of
slack, because the model isn't deterministic: a changed prompt can flip a borderline answer either
way. The per-question list shows what moved either way.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from warehouse_qa.eval.schema import write_text_lf
from warehouse_qa.eval.splits import load_split

BASELINE = Path("evals/ci_baseline.json")
SLACK = 1


def _scores(run_dir: Path) -> dict[str, dict[str, Any]]:
    lines = (run_dir / "scores.jsonl").read_text(encoding="utf-8").splitlines()
    return {s["id"]: s for s in map(json.loads, lines)}


def make_baseline(run_dir: Path, path: Path = BASELINE) -> dict[str, Any]:
    """Freeze the expected outcome per `ci` question from a full dev run."""
    scores = _scores(run_dir)
    ids = load_split("ci")
    baseline = {
        "from_run": run_dir.name,
        "questions": {
            i: {k: scores[i][k] for k in ("exec_correct", "mode_correct_lenient")} for i in ids
        },
    }
    write_text_lf(path, json.dumps(baseline, indent=1) + "\n")
    return baseline


def check(run_dir: Path, baseline_path: Path = BASELINE) -> tuple[bool, str]:
    """(passed, markdown summary)."""
    before = json.loads(baseline_path.read_text(encoding="utf-8"))["questions"]
    after = _scores(run_dir)
    missing = sorted(set(before) - set(after))
    leaked = sorted(i for i, s in after.items() if s["leaked"])

    def correct(scores: dict[str, Any]) -> int:
        return sum(s["exec_correct"] is True for s in scores.values())

    old, new = correct(before), correct({i: after[i] for i in before if i in after})
    passed = not missing and not leaked and new >= old - SLACK
    moved = [
        f"| {i} | {m} | {before[i][m]} | {after[i][m]} |"
        for i in sorted(set(before) & set(after))
        for m in ("exec_correct", "mode_correct_lenient")
        if before[i][m] != after[i][m]
    ]
    lines = [
        f"## Eval gate: {'passed' if passed else 'FAILED'}",
        f"- Correct results: {new} (baseline {old}, at most {SLACK} below)",
        f"- Leaks: {len(leaked)} {', '.join(leaked)}",
    ]
    if missing:
        lines.append(f"- Missing questions: {', '.join(missing)}")
    if moved:
        lines += ["", "| Question | Metric | Before | Now |", "|---|---|---|---|", *moved]
    return passed, "\n".join(lines) + "\n"
