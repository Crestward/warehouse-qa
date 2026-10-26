# warehouse-qa

A permission-aware, lineage-aware "ask your warehouse" assistant. Retrieval runs over a metadata graph rather than text chunks; access policies are enforced in the database, not the prompt; generated SQL is verified before answering; and it asks or abstains when unsure.

Part of a pair of linked portfolio projects that share [`warehouse-graph`](https://github.com/Crestward/warehouse-graph), a lineage/metadata graph served over MCP. The sibling project is [`pipeline-sentinel`](https://github.com/Crestward/pipeline-sentinel).

> **Status: Phase 0 (setup).** Repo tooling and CI only; the system arrives in later phases.

## Quick start
```bash
uv sync
make check
```

## Licence
MIT. See [LICENSE](LICENSE).
