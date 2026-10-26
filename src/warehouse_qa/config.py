"""Paths to the shared pieces that live in warehouse-graph: graph file, policies, cost ledger."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILE = Path("warehouse-qa.toml")


@dataclass(frozen=True)
class Env:
    graph: Path
    policies: Path
    ledger: Path

    @classmethod
    def load(cls, path: Path = CONFIG_FILE) -> Env:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))["warehouse_graph"]
        base = Path(os.environ.get("WAREHOUSE_GRAPH_DIR", path.parent / raw["dir"]))
        return cls(
            graph=base / raw["graph"],
            policies=base / raw["policies"],
            ledger=base / raw["ledger"],
        )
