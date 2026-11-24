"""Command-line entry point: `warehouse-qa ask`, `demo-record` and `eval <command>`."""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from typing import Any

from warehouse_qa.config import Env

ROLES = ("admin", "finance", "marketing", "analyst_au", "analyst_nz")


def _graph_env() -> Env:
    from warehouse_graph.cli import load_dotenv

    env = Env.load()
    load_dotenv(env.graph.parents[1] / ".env")  # API key and DB settings live in warehouse-graph
    return env


def ask(role: str, question: str) -> int:
    """One question as one role: answer, rows, SQL, tables, caveats."""
    from warehouse_qa.service import Assistant, NotRecordedError

    _graph_env()
    try:
        answer = Assistant().ask(role, question)
    except NotRecordedError as e:
        print(e)
        return 1
    r = answer.record
    print(f"[{answer.outcome}]")
    if answer.answered:
        print(f"\n{_table(r.columns, r.rows)}\n")
    print(r.answer_text)
    if answer.answered:
        print(f"\nSQL (run as {role}):\n{r.sql}")
        if answer.lineage:
            edges = ", ".join(
                f"{a.split('.', 1)[1]} -> {b.split('.', 1)[1]}" for a, b in answer.lineage
            )
            print(f"\nLineage: {edges}")
    if r.usage.usd:
        print(f"\nUS${r.usage.usd:.4f}")
    return 0


def _table(columns: list[str], rows: list[list[Any]], limit: int = 20) -> str:
    def cell(v: Any) -> str:
        if isinstance(v, float):
            return f"{v:,.2f}".rstrip("0").rstrip(".")
        return "" if v is None else str(v)

    body = [[cell(v) for v in row] for row in rows[:limit]]
    widths = [max(len(x) for x in col) for col in zip(columns, *body, strict=True)]
    lines = ["  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)) for row in body]
    head = "  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True))
    more = [f"... {len(rows) - limit} more"] if len(rows) > limit else []
    return "\n".join([head, "  ".join("-" * w for w in widths), *lines, *more])


def demo_record() -> int:
    """Record the model replies of the demo questions, so the demo runs without an API key."""
    from warehouse_qa.service import RECORDED, Assistant, demo_questions

    _graph_env()
    bot = Assistant(cache_dir=RECORDED)
    for q in demo_questions():
        a = bot.ask(q.role, q.question)
        print(f"{q.role:<11} {a.outcome:<28} {a.record.reason_code or '':<24} {q.question}")
    return 0


def gate(run_dir: Path, write_baseline: bool) -> int:
    """The PR gate: a ci-split run vs the committed baseline."""
    from warehouse_qa.eval.ci_gate import BASELINE, check, make_baseline

    if write_baseline:
        make_baseline(run_dir)
        print(f"wrote {BASELINE}")
        return 0
    passed, summary = check(run_dir)
    print(summary)
    return 0 if passed else 1


def validate() -> int:
    """Run every gold query as its role; fail on errors or empty results."""
    from warehouse_graph.adapters import PostgresAdapter
    from warehouse_graph.graph import GraphArtifact

    from warehouse_qa.eval.harness import gold_results
    from warehouse_qa.eval.schema import load_questions, load_redteam

    env = _graph_env()
    questions = load_questions()
    redteam = load_redteam()
    results = gold_results(PostgresAdapter(GraphArtifact.load(env.graph)), questions, 1000)
    bad = 0
    for q in questions:
        if q.gold_sql is None:
            continue
        rows, error = results[q.id]
        if error or not rows:
            bad += 1
            print(f"FAIL {q.id} ({q.role}): {error or 'no rows'}")
    with_sql = sum(1 for q in questions if q.gold_sql)
    print(f"{len(questions)} questions ({with_sql} with gold SQL), {len(redteam)} red-team prompts")
    print("all gold queries return rows as their role" if not bad else f"{bad} gold queries failed")
    return 1 if bad else 0


def label(check: bool) -> int:
    """Derive the tables/columns each gold query reads -> evals/gold_nodes.json."""
    from warehouse_graph.graph import GraphArtifact

    from warehouse_qa.eval.labels import (
        SqlNodeResolver,
        derive_gold_nodes,
        load_gold_nodes,
        write_gold_nodes,
    )
    from warehouse_qa.eval.schema import load_questions

    env = _graph_env()
    resolver = SqlNodeResolver(GraphArtifact.load(env.graph))
    sqls = {q.id: q.gold_sql for q in load_questions() if q.gold_sql}
    derived = derive_gold_nodes(sqls, resolver)
    if check:
        same = derived == load_gold_nodes()
        print(
            "gold_nodes.json is up to date" if same else "gold_nodes.json is stale; run eval label"
        )
        return 0 if same else 1
    write_gold_nodes(derived)
    print(f"wrote derived gold nodes for {len(derived)} questions")
    return 0


def split(force: bool) -> int:
    from warehouse_qa.eval.schema import load_questions
    from warehouse_qa.eval.splits import SPLIT_DIR, make_splits, write_splits

    if (SPLIT_DIR / "manifest.json").exists() and not force:
        print("splits are frozen; pass --force only before any test-split result exists")
        return 1
    splits = make_splits(load_questions())
    write_splits(splits)
    print(", ".join(f"{k}={len(v)}" for k, v in splits.items()))
    return 0


def contamination(write: Path | None) -> int:
    """Near-duplicates vs QueryExamples/caveats and within the set; unreviewed hits fail."""
    import yaml
    from warehouse_graph.graph import GraphArtifact
    from warehouse_graph.graph.embed import get_embedder

    from warehouse_qa.eval.contamination import (
        LEXICAL_THRESHOLD,
        SEMANTIC_THRESHOLD,
        find_hits,
    )
    from warehouse_qa.eval.schema import load_questions, load_redteam

    env = _graph_env()
    graph = GraphArtifact.load(env.graph)
    items = {q.id: q.question for q in load_questions()} | {r.id: r.prompt for r in load_redteam()}
    against, within = find_hits(items, graph, get_embedder(graph.meta.embedding_model))
    n_refs = sum(1 for n in graph.nodes if n.startswith(("query_example.", "caveat.")))
    reviewed_path = Path("evals/contamination_reviewed.yaml")
    reviewed = yaml.safe_load(reviewed_path.read_text("utf-8")) if reviewed_path.exists() else {}
    ok = {(r["item"], r["other"]) for r in (reviewed or {}).get("reviewed", [])}
    unreviewed = [h for h in [*against, *within] if (h.item, h.other) not in ok]
    lines = [
        "# Contamination check",
        "",
        "Generated by `warehouse-qa eval contamination`. Thresholds: cosine >="
        f" {SEMANTIC_THRESHOLD} ({graph.meta.embedding_model}) or word Jaccard >="
        f" {LEXICAL_THRESHOLD}.",
        f"{len(items)} items checked against {n_refs} QueryExamples and caveats, and against"
        " each other.",
        "",
        f"**Hits vs QueryExamples/caveats: {len(against)} · within the set: {len(within)} ·"
        f" unreviewed: {len(unreviewed)}**",
        "",
        "Every hit was reviewed by hand; the reasons are in `evals/contamination_reviewed.yaml`.",
        "",
        "| item | other | cosine | jaccard | item text | other text |",
        "|---|---|---|---|---|---|",
    ]
    for h in [*against, *within]:
        lines.append(
            f"| {h.item} | {h.other} | {h.cosine} | {h.jaccard} | {h.item_text[:70]} |"
            f" {h.other_text[:70]} |"
        )
    if write:
        from warehouse_qa.eval.schema import write_text_lf

        write_text_lf(write, "\n".join(lines) + "\n")
        print(f"wrote {write}")
    for h in unreviewed:
        print(f"UNREVIEWED {h.item} ~ {h.other} cos={h.cosine} jac={h.jaccard}")
    print(f"{len(against)} + {len(within)} hits, {len(unreviewed)} unreviewed")
    return 1 if unreviewed else 0


def run(config: Path, split_name: str, skip_preflight: bool, gates: str | None) -> int:
    from warehouse_qa.eval.harness import RunConfig
    from warehouse_qa.eval.harness import run as run_eval

    _graph_env()
    cfg = RunConfig.load(config)
    if gates:
        cfg = RunConfig.model_validate({**cfg.model_dump(), "gates": gates})
    out = run_eval(cfg, split_name, skip_preflight=skip_preflight)
    print(f"wrote {out}")
    print((out / "report.md").read_text(encoding="utf-8").split("## By category")[0])
    return 0


def redteam(config: Path, skip_preflight: bool) -> int:
    """The 50 attacks on one config. Fails on any leak."""
    from warehouse_qa.eval.harness import RunConfig
    from warehouse_qa.eval.redteam import run as run_redteam

    _graph_env()
    out = run_redteam(RunConfig.load(config), skip_preflight=skip_preflight)
    print(f"wrote {out}")
    print((out / "report.md").read_text(encoding="utf-8").split("## Per prompt")[0])
    return 1 if json.loads((out / "summary.json").read_text("utf-8"))["leaked"] else 0


def calibrate(run_dir: Path, write: bool) -> int:
    """Gate A/B thresholds from a recorded observe run on dev (offline, $0)."""
    import yaml

    from warehouse_qa.eval.calibration_report import render
    from warehouse_qa.eval.harness import load_records
    from warehouse_qa.eval.metrics import score
    from warehouse_qa.eval.reliability import calibrate as run_calibration
    from warehouse_qa.eval.schema import load_questions, write_text_lf
    from warehouse_qa.pipeline.gates import GATES_CONFIG

    config = yaml.safe_load((run_dir / "config.yaml").read_text(encoding="utf-8"))
    if config["split"] != "dev" or config.get("gates") != "observe":
        print("calibrate needs a `gates: observe` run on dev (never test)")
        return 1
    questions = {q.id: q for q in load_questions()}
    records = load_records(run_dir)
    correct = {r.id: score(questions[r.id], r, set()).exec_correct is True for r in records}
    params, evidence = run_calibration(records, questions, correct)
    report = render(run_dir.name, params, evidence)
    print(report)
    if write:
        body = {"tuned_on": run_dir.name, "params": params.model_dump()}
        write_text_lf(
            GATES_CONFIG,
            "# Gate A/B, chosen by `eval calibrate` on dev (docs/gates.md).\n"
            + yaml.safe_dump(body, sort_keys=False),
        )
        write_text_lf(Path("docs/gates.md"), report)
        print(f"wrote {GATES_CONFIG} and docs/gates.md")
    return 0


def rescore(run_dir: Path) -> int:
    from warehouse_qa.eval.harness import rescore as rescore_run

    summary = rescore_run(run_dir)
    o = summary["metrics"]["overall"]
    print(f"rescored {run_dir}: execution accuracy {o['execution_accuracy']['rate']}")
    return 0


def retrieve(retriever: str, split_name: str) -> int:
    """One retrieval run (no LLM): recall, trap labels, context size, role filtering."""
    from warehouse_qa.eval.retrieval import retrieve_run

    _graph_env()
    out = retrieve_run(retriever, split_name)
    m = json.loads((out / "summary.json").read_text("utf-8"))
    met = m["metrics"]
    print(f"wrote {out}")
    print(
        f"recall@5/10/20 {met['recall']['5']['rate']} / {met['recall']['10']['rate']} /"
        f" {met['recall']['20']['rate']}  trap_ok {met['trap_ok']['rate']}"
        f"  context {met['context_tokens_mean']} tok (schema dump {m['schema_dump_tokens_mean']})"
        f"  violations {met['visibility_violations']}"
    )
    return 1 if met["visibility_violations"] else 0


def sweep(split_name: str, write: bool) -> int:
    """Tune the hybrid retriever on dev; optionally save the best params and a report."""
    import yaml

    from warehouse_qa.eval.retrieval import HYBRID_CONFIG
    from warehouse_qa.eval.retrieval import sweep as run_sweep
    from warehouse_qa.eval.schema import write_text_lf

    _graph_env()
    rows = run_sweep(split_name)
    lines = [
        "# Hybrid retriever tuning",
        "",
        f"Generated by `warehouse-qa eval sweep --split {split_name}`: every combination of the"
        " grid on the dev split only (never test), scored on retrieved nodes, so $0 of LLM calls."
        " Sorted by recall@20, then recall@10, then retrieval trap avoidance, then stale-warning"
        " errors (false alarms + misses, judged by whether the gold SQL reads stale-derived data).",
        "",
        "| rank | params | recall@10 | recall@20 | trap ok | false stale | missed stale |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        params = ", ".join(f"{k}={v}" for k, v in r["params"].items())
        lines.append(
            f"| {i} | {params} | {r['recall@10']} | {r['recall@20']} | {r['trap_ok']}"
            f" | {r['false_stale_warning']} | {r['missed_stale_warning']} |"
        )
    print("\n".join(lines[:16]))
    if write:
        write_text_lf(Path("docs/retrieval-tuning.md"), "\n".join(lines) + "\n")
        best = {"tuned_on": split_name, "params": rows[0]["params"]}
        write_text_lf(
            HYBRID_CONFIG,
            "# Hybrid retriever params, chosen by `eval sweep` on dev (docs/retrieval-tuning.md).\n"
            + yaml.safe_dump(best, sort_keys=False),
        )
        print(f"wrote docs/retrieval-tuning.md and {HYBRID_CONFIG}")
    return 0


def compare_retrieval(run_a: Path, run_b: Path) -> int:
    from warehouse_qa.eval.retrieval import compare_runs

    for k in (10, 20):
        print(json.dumps(compare_runs(run_a, run_b, k)))
    return 0


def compare(run_a: Path, run_b: Path) -> int:
    """Paired McNemar between two eval runs (e.g. config 4 vs config 6)."""
    from warehouse_qa.eval.harness import compare as compare_runs

    print(json.dumps(compare_runs(run_a, run_b), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="warehouse-qa")
    top = parser.add_subparsers(dest="group", required=True)
    ak = top.add_parser("ask", help="ask one question as one role")
    ak.add_argument("--role", required=True, choices=ROLES)
    ak.add_argument("question")
    top.add_parser("demo-record", help="record the demo questions' model replies")
    ev = top.add_parser("eval", help="the P1 eval set and harness").add_subparsers(
        dest="command", required=True
    )
    ev.add_parser("validate", help="run every gold query as its role")
    lab = ev.add_parser("label", help="derive gold nodes from gold SQL")
    lab.add_argument("--check", action="store_true", help="fail if gold_nodes.json is stale")
    sp = ev.add_parser("split", help="make the dev/test/dev-mini splits (once)")
    sp.add_argument("--force", action="store_true")
    co = ev.add_parser("contamination", help="near-duplicate check")
    co.add_argument("--write", type=Path, help="write the markdown report here")
    ru = ev.add_parser("run", help="run a config on a split")
    ru.add_argument("--config", type=Path, required=True)
    ru.add_argument("--split", default="dev", choices=["dev", "test", "dev_mini", "ci"])
    ru.add_argument("--skip-preflight", action="store_true")
    ru.add_argument("--gates", choices=["off", "observe", "enforce"], help="override the config")
    gt = ev.add_parser("gate", help="PR gate: a ci-split run against the committed baseline")
    gt.add_argument("run_dir", type=Path)
    gt.add_argument("--write-baseline", action="store_true", help="from a full dev run")
    rd = ev.add_parser("redteam", help="run the 50 red-team prompts")
    rd.add_argument("--config", type=Path, required=True)
    rd.add_argument("--skip-preflight", action="store_true")
    ca = ev.add_parser("calibrate", help="Gate A/B thresholds from a dev observe run (offline)")
    ca.add_argument("run_dir", type=Path)
    ca.add_argument("--write", action="store_true", help="save configs/gates.yaml + docs/gates.md")
    rs = ev.add_parser("rescore", help="recompute metrics offline from a run's JSONL")
    rs.add_argument("run_dir", type=Path)
    cm = ev.add_parser("compare", help="paired McNemar between two eval runs")
    cm.add_argument("run_a", type=Path)
    cm.add_argument("run_b", type=Path)
    rt = ev.add_parser("retrieve", help="retrieval-only run (no LLM)")
    rt.add_argument("--retriever", choices=["vector", "hybrid"], required=True)
    rt.add_argument("--split", default="dev", choices=["dev", "test", "dev_mini"])
    sw = ev.add_parser("sweep", help="tune the hybrid retriever on dev")
    sw.add_argument("--split", default="dev", choices=["dev", "dev_mini"])
    sw.add_argument("--write", action="store_true", help="save best params + report")
    cr = ev.add_parser("compare-retrieval", help="paired recall test: run A vs run B")
    cr.add_argument("run_a", type=Path)
    cr.add_argument("run_b", type=Path)
    args = parser.parse_args(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):  # Windows consoles default to cp1252
        sys.stdout.reconfigure(encoding="utf-8")

    if args.group == "ask":
        return ask(args.role, args.question)
    if args.group == "demo-record":
        return demo_record()
    if args.command == "gate":
        return gate(args.run_dir, args.write_baseline)

    if args.command == "validate":
        return validate()
    if args.command == "label":
        return label(args.check)
    if args.command == "split":
        return split(args.force)
    if args.command == "contamination":
        return contamination(args.write)
    if args.command == "run":
        return run(args.config, args.split, args.skip_preflight, args.gates)
    if args.command == "redteam":
        return redteam(args.config, args.skip_preflight)
    if args.command == "calibrate":
        return calibrate(args.run_dir, args.write)
    if args.command == "compare":
        return compare(args.run_a, args.run_b)
    if args.command == "retrieve":
        return retrieve(args.retriever, args.split)
    if args.command == "sweep":
        return sweep(args.split, args.write)
    if args.command == "compare-retrieval":
        return compare_retrieval(args.run_a, args.run_b)
    if args.command == "rescore":
        return rescore(args.run_dir)
    return 1


if __name__ == "__main__":
    sys.exit(main())
