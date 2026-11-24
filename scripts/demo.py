"""`make demo`: build the governed demo warehouse, then open the demo page.

    uv run python scripts/demo.py               everything (first run: a few minutes)
    uv run python scripts/demo.py --page-only   the page alone, on a warehouse built earlier

Needs Docker and uv. No API key: the example questions replay recorded model replies.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GRAPH_URL = "https://github.com/Crestward/warehouse-graph"


def run(step: str, *cmd: str, cwd: Path, check: bool = True) -> None:
    print(f"\n== {step}", flush=True)
    started = time.perf_counter()
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}  # each repo has its own
    done = subprocess.run(cmd, cwd=cwd, env=env, check=False)
    print(f"== {step}: {time.perf_counter() - started:.0f} s", flush=True)
    if check and done.returncode != 0:
        sys.exit(f"{step} failed")


def graph_dir() -> Path:
    config = tomllib.loads((ROOT / "warehouse-qa.toml").read_text(encoding="utf-8"))
    default = ROOT / config["warehouse_graph"]["dir"]
    return Path(os.environ.get("WAREHOUSE_GRAPH_DIR", default)).resolve()


def pinned_ref() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    source = project["tool"]["uv"]["sources"]["warehouse-graph"]
    return str(source.get("rev") or source["tag"])


def build(graph: Path) -> None:
    if not graph.exists():
        run("clone warehouse-graph", "git", "clone", "-q", GRAPH_URL, str(graph), cwd=ROOT)
        run("pin it", "git", "checkout", "-q", pinned_ref(), cwd=graph)
    run("start Postgres", "docker", "compose", "up", "-d", "--wait", cwd=graph)
    run("install warehouse-graph", "uv", "sync", "--locked", "-q", cwd=graph)
    wg = ("uv", "run", "warehouse-graph")
    # ad_spend is loaded late on purpose, so there is a stale source to warn about.
    run("synthetic data", *wg, "demo-data", "--stale", "ad_spend", cwd=graph)
    run("dbt build (freshness fails on ad_spend on purpose)", *wg, "demo-build", cwd=graph)
    run("metadata graph", *wg, "build", cwd=graph)
    run("access policies", *wg, "policies", cwd=graph)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--page-only", action="store_true")
    parser.add_argument("--no-page", action="store_true", help="build the warehouse and stop")
    args = parser.parse_args()
    if not args.page_only:
        build(graph_dir())
    if args.no_page:
        return
    page = str(ROOT / "src" / "warehouse_qa" / "app.py")
    run("demo page: http://localhost:8501", "uv", "run", "streamlit", "run", page, cwd=ROOT)


if __name__ == "__main__":
    main()
