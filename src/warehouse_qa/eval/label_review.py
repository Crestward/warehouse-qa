"""`warehouse-qa eval label-review`: how much do questionable gold labels change the results?

The labels are frozen once a test result exists, and the headline numbers keep them. This is a
**sensitivity analysis**, done after the test run and reported next to it: questions whose wording
allows more than one right answer (evals/label_review.yaml) also accept the listed alternatives,
for every config alike.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict
from warehouse_graph.adapters import PostgresAdapter
from warehouse_graph.governance import Principal

from warehouse_qa.eval.compare import normalize, results_match
from warehouse_qa.eval.harness import load_records
from warehouse_qa.eval.metrics import ANSWER_MODES
from warehouse_qa.eval.results import CONFIGS, latest
from warehouse_qa.eval.schema import load_questions, write_text_lf
from warehouse_qa.eval.splits import load_split
from warehouse_qa.eval.stats import bootstrap_ci, mcnemar

REVIEW = Path("evals/label_review.yaml")
DOC = Path("docs/label-review.md")
START, END = "<!-- generated: warehouse-qa eval label-review -->", "<!-- end generated -->"


class Alternative(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    defect: str
    sql: str
    why: str


def load_review(path: Path = REVIEW) -> list[Alternative]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [Alternative.model_validate(a) for a in data["alternatives"]]


def alternative_rows(
    adapter: PostgresAdapter, alternatives: list[Alternative]
) -> dict[str, list[list[Any]]]:
    roles = {q.id: q.role for q in load_questions()}
    out: dict[str, list[list[Any]]] = {}
    for a in alternatives:
        session = adapter.session_as(Principal(roles[a.id], ""))
        try:
            rows = adapter.execute(session, a.sql, 1000, 60).rows
        finally:
            session.close()
        if not rows:
            raise RuntimeError(f"{a.id}: the alternative returns no rows")
        out[a.id] = [[normalize(v) for v in r] for r in rows]
    return out


def outcomes(run: Path, alternatives: dict[str, list[list[Any]]]) -> dict[str, tuple[bool, bool]]:
    """question -> (correct under the frozen labels, correct if alternatives count too)."""
    lines = (run / "scores.jsonl").read_text(encoding="utf-8").splitlines()
    frozen = {s["id"]: s["exec_correct"] for s in map(json.loads, lines)}
    out = {}
    for r in load_records(run):
        if frozen[r.id] is None:
            continue
        also = (
            r.id in alternatives
            and r.predicted_mode in ANSWER_MODES
            and r.error is None
            and not r.truncated
            and results_match(alternatives[r.id], r.rows)
        )
        out[r.id] = (bool(frozen[r.id]), bool(frozen[r.id]) or also)
    return out


def render(adapter: PostgresAdapter, split: str) -> str:
    review = [a for a in load_review() if a.id in set(load_split(split))]
    rows = alternative_rows(adapter, review)
    by_config = {c: outcomes(latest(c, split), rows) for c in CONFIGS}
    ids = sorted(by_config["config4"])
    lines = [
        f"### Execution accuracy on {split}, frozen labels vs accepting the alternatives",
        "| Config | Frozen labels (headline) | Alternatives accepted too | Changed |",
        "|---|---|---|---|",
    ]
    for c, label in CONFIGS.items():
        before = [by_config[c][i][0] for i in ids]
        after = [by_config[c][i][1] for i in ids]
        changed = [i for i in ids if by_config[c][i][0] != by_config[c][i][1]]
        cells = []
        for values in (before, after):
            mean, low, high = bootstrap_ci([float(v) for v in values])
            cells.append(
                f"{mean:.1%} [{100 * low:.0f}, {100 * high:.0f}] ({sum(values)}/{len(ids)})"
            )
        lines.append(f"| {label} | {cells[0]} | {cells[1]} | {', '.join(changed) or 'none'} |")
    lines += [
        "",
        "| Comparison | Only first right | Only second right | p, frozen | p, alternatives |",
        "|---|---|---|---|---|",
    ]
    for a, b in (
        ("config4", "baseline1"),
        ("config4", "config2"),
        ("config4", "config6"),
        ("oracle", "config4"),
    ):
        pair = [
            mcnemar([by_config[a][i][k] for i in ids], [by_config[b][i][k] for i in ids])
            for k in (0, 1)
        ]
        lines.append(
            f"| {CONFIGS[a]} vs {CONFIGS[b]} | {pair[1][0]} | {pair[1][1]} | {pair[0][2]:.3g}"
            f" | {pair[1][2]:.3g} |"
        )
    lines += [
        "",
        "### The alternatives",
        "| Question | Defect | Why it is also right |",
        "|---|---|---|",
    ]
    lines += [f"| {a.id} | {a.defect} | {a.why} |" for a in review]
    return "\n".join(lines) + "\n"


def write(adapter: PostgresAdapter, split: str) -> None:
    block = f"{START}\n{render(adapter, split)}{END}"
    text = DOC.read_text(encoding="utf-8") if DOC.exists() else f"{START}\n{END}\n"
    head, rest = text.split(START, 1)
    write_text_lf(DOC, head + block + rest.split(END, 1)[1])
