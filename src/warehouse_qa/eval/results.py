"""`warehouse-qa eval results`: the generated tables and plot in docs/results.md (task 8.2).

Reads the saved runs only ($0). The text between the markers in docs/results.md is replaced; the
hand-written analysis around it is left alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from warehouse_qa.eval.harness import RESULTS_DIR, compare
from warehouse_qa.eval.metrics import ANSWER_MODES
from warehouse_qa.eval.schema import write_text_lf

DOC = Path("docs/results.md")
PLOT = Path("docs/img/risk_coverage.svg")
START, END = "<!-- generated: warehouse-qa eval results -->", "<!-- end generated -->"
CONFIGS = {
    "baseline1": "1 · schema dump",
    "config2": "2 · vector-only",
    "config4": "4 · main system",
    "config6": "6 · no verification",
    "oracle": "oracle retrieval",
}
METRICS = [
    ("execution_accuracy", "Execution accuracy"),
    ("trap_avoidance", "Trap avoidance"),
    ("mode_accuracy_lenient", "Mode accuracy, lenient"),
    ("mode_accuracy", "Mode accuracy, strict"),
    ("over_refusal", "Over-refusal"),
    ("false_abstention", "False abstention"),
    ("clarification_precision", "Clarification precision"),
    ("leakage", "Leakage"),
]
PAIRS = [
    ("config4", "baseline1", ("exec_correct", "mode_correct_lenient", "trap_avoided")),
    ("config4", "config2", ("exec_correct", "trap_avoided")),
    ("config4", "config6", ("exec_correct", "trap_avoided")),
    ("oracle", "config4", ("exec_correct",)),
]
METRIC_NAMES = {
    "exec_correct": "execution accuracy",
    "mode_correct_lenient": "lenient mode accuracy",
    "trap_avoided": "trap avoidance",
}


def latest(name: str, split: str, results_dir: Path = RESULTS_DIR) -> Path:
    runs = sorted(results_dir.glob(f"*-{name}-{split}"))
    if not runs:
        raise FileNotFoundError(f"no {split} run of {name}")
    return runs[-1]


def _summary(run: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    return data


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _cell(rate: dict[str, Any]) -> str:
    if rate.get("rate") is None:
        return "n/a"
    low, high = rate["ci95"]
    return f"{rate['rate']:.1%} [{100 * low:.0f}, {100 * high:.0f}] ({rate['n']})"


def _input_tokens(run: Path) -> float:
    used = [r["usage"] for r in _jsonl(run / "questions.jsonl")]
    kinds = ("input_tokens", "cache_read_tokens", "cache_write_tokens")
    return float(sum(u[k] for u in used for k in kinds)) / len(used)


def operating_point(run: Path) -> tuple[float, float]:
    """(coverage, risk) of the answers a run gave."""
    scores = {s["id"]: s for s in _jsonl(run / "scores.jsonl")}
    answered = [
        scores[r["id"]]["exec_correct"] is True
        for r in _jsonl(run / "questions.jsonl")
        if r["predicted_mode"] in ANSWER_MODES
    ]
    return len(answered) / len(scores), 1 - sum(answered) / len(answered)


def tables(split: str) -> str:
    runs = {name: latest(name, split) for name in CONFIGS}
    sums = {name: _summary(run) for name, run in runs.items()}
    n = sums["config4"]["metrics"]["overall"]["n"]
    lines = [
        f"### Headline, {split} split (n = {n})",
        "Rate, 95% bootstrap CI in points, and the number of questions the rate is over.",
        "",
        "| Metric | " + " | ".join(CONFIGS.values()) + " |",
        "|---|" + "---|" * len(CONFIGS),
    ]
    for key, label in METRICS:
        cells = [_cell(sums[c]["metrics"]["overall"][key]) for c in CONFIGS]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    points = {c: operating_point(runs[c]) for c in CONFIGS}
    lines.append(
        "| Wrong, of the questions it answered | "
        + " | ".join(f"{r:.1%} at {c:.0%} coverage" for c, r in points.values())
        + " |"
    )
    lines.append(
        "| Mean input tokens per question (prompt, context, repairs) | "
        + " | ".join(f"{_input_tokens(runs[c]):,.0f}" for c in CONFIGS)
        + " |"
    )
    lines.append(
        "| Nominal cost per question | "
        + " | ".join(f"${sums[c]['metrics']['cost']['usd_per_question']:.4f}" for c in CONFIGS)
        + " |"
    )
    lines += [
        "",
        "### Planned comparisons (paired, exact McNemar)",
        "| Comparison | Metric | n | First | Second | Only first right | Only second right | p |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for a, b, metrics in PAIRS:
        result = compare(runs[a], runs[b])
        for m in metrics:
            x = result[m]
            lines.append(
                f"| {CONFIGS[a]} vs {CONFIGS[b]} | {METRIC_NAMES[m]} | {x['n']} | {x['a']}"
                f" | {x['b']} | {x['only_a']} ({', '.join(x['only_a_ids']) or 'none'})"
                f" | {x['only_b']} ({', '.join(x['only_b_ids']) or 'none'}) | {x['p_value']:.3g} |"
            )
    lines += ["", "### By category and trap type (execution accuracy, trap avoidance)"]
    lines += ["| Slice | " + " | ".join(CONFIGS.values()) + " |", "|---|" + "---|" * len(CONFIGS)]
    for cat in ("regular", "trap"):
        cells = [
            _cell(sums[c]["metrics"]["by_category"][cat]["execution_accuracy"]) for c in CONFIGS
        ]
        lines.append(f"| {cat}: execution | " + " | ".join(cells) + " |")
    for trap in sums["config4"]["metrics"]["by_trap_type"]:
        rates = [sums[c]["metrics"]["by_trap_type"][trap] for c in CONFIGS]
        if rates[0]["rate"] is not None:
            cells = [f"{round(r['rate'] * r['n'])}/{r['n']}" for r in rates]
            lines.append(f"| trap: {trap} | " + " | ".join(cells) + " |")
    rel = sums["config4"]["reliability"]
    sel = rel["selective_accuracy_80"]
    lines += [
        "",
        "### Risk-coverage, main system",
        f"![Risk-coverage](img/{PLOT.name})",
        "",
        f"Selective accuracy at {sel['coverage']:.0%} coverage: **{sel['accuracy']:.1%}**. AURC:"
        f" {rel['aurc']} (a constant confidence would give"
        f" {1 - rel['accuracy_when_answering']:.4f}; lower is better).",
        "",
        "| Coverage | Risk | Confidence at least |",
        "|---|---|---|",
        *(
            f"| {p['coverage']:.1%} | {p['risk']:.1%} | {p['threshold']:.3f} |"
            for p in rel["points"][::4]
        ),
    ]
    return "\n".join(lines) + "\n"


def plot(split: str) -> str:
    """Risk-coverage for config 4, with every config's single operating point. Plain SVG that
    follows the reader's light or dark theme."""
    runs = {name: latest(name, split) for name in CONFIGS}
    curve = _summary(runs["config4"])["reliability"]["points"]
    ops = {c: operating_point(runs[c]) for c in CONFIGS}
    top = max([p["risk"] for p in curve] + [r for _, r in ops.values()])
    y_max = max(0.1, (int(top * 20) + 1) / 20)
    w, h, left, right, up, down = 720, 400, 60, 150, 56, 56

    def x(c: float) -> float:
        return round(left + c * (w - left - right), 1)

    def y(r: float) -> float:
        return round(h - down - r / y_max * (h - up - down), 1)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" role="img"'
        ' aria-label="Risk against coverage for the main system">',
        "<style>.s{fill:#fcfcfb}.t{fill:#0b0b0b}.u{fill:#52514e}.g{stroke:#e4e3de}"
        ".l{stroke:#2a78d6}.m{fill:#2a78d6}.o{fill:#8b8a84}.r{stroke:#fcfcfb}"
        "text{font-family:system-ui,sans-serif;font-size:12px}"
        "@media(prefers-color-scheme:dark){.s{fill:#1a1a19}.t{fill:#fff}.u{fill:#c3c2b7}"
        ".g{stroke:#3a3a37}.l{stroke:#3987e5}.m{fill:#3987e5}.r{stroke:#1a1a19}}</style>",
        f'<rect class="s" width="{w}" height="{h}" rx="8"/>',
        '<text class="t" x="20" y="26" style="font-size:15px;font-weight:600">Share of answers'
        f" that are wrong, by how many questions are answered ({split})</text>",
        '<text class="u" x="20" y="44">Line: main system, most confident answers first.'
        " Dots: each config as it runs.</text>",
    ]
    step = 0.05 if y_max <= 0.3 else 0.1
    for i in range(round(y_max / step) + 1):
        r = i * step
        out.append(f'<line class="g" x1="{left}" x2="{w - right}" y1="{y(r)}" y2="{y(r)}"/>')
        out.append(
            f'<text class="u" x="{left - 8}" y="{y(r) + 4}" text-anchor="end">{r:.0%}</text>'
        )
    for tick in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
        out.append(
            f'<text class="u" x="{x(tick)}" y="{h - down + 20}" text-anchor="middle">'
            f"{tick:.0%}</text>"
        )
    mid = (left + w - right) / 2
    out.append(
        f'<text class="u" x="{mid}" y="{h - 12}" text-anchor="middle">Questions answered</text>'
    )
    path = " ".join(f"{x(p['coverage'])},{y(p['risk'])}" for p in curve)
    out.append(
        f'<polyline class="l" points="{path}" fill="none" stroke-width="2"'
        ' stroke-linejoin="round" stroke-linecap="round"/>'
    )
    label_y: list[float] = []
    for c, (cov, risk) in sorted(ops.items(), key=lambda kv: -kv[1][1]):
        ly = y(risk) + 4
        while any(abs(ly - other) < 15 for other in label_y):
            ly += 15
        label_y.append(ly)
        cls = "m" if c == "config4" else "o"
        out.append(
            f'<line class="g" x1="{x(cov) + 7}" x2="{w - right + 10}"'
            f' y1="{y(risk)}" y2="{ly - 4}"/>'
        )
        out.append(
            f'<circle class="{cls} r" cx="{x(cov)}" cy="{y(risk)}" r="5" stroke-width="2">'
            f"<title>{CONFIGS[c]}: {risk:.1%} wrong at {cov:.0%} coverage</title></circle>"
        )
        out.append(f'<text class="t" x="{w - right + 14}" y="{ly}">{CONFIGS[c]}</text>')
    return "\n".join([*out, "</svg>"]) + "\n"


def write(split: str) -> None:
    PLOT.parent.mkdir(parents=True, exist_ok=True)
    write_text_lf(PLOT, plot(split))
    block = f"{START}\n{tables(split)}{END}"
    text = DOC.read_text(encoding="utf-8") if DOC.exists() else f"# Results\n\n{START}\n{END}\n"
    head, rest = text.split(START, 1)
    tail = rest.split(END, 1)[1]
    write_text_lf(DOC, head + block + tail)
