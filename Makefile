.PHONY: help test lint typecheck check eval-check eval-baseline redteam demo page

help:
	@echo "make demo           build the demo warehouse, then open the page (Docker; no API key needed)"
	@echo "make page           the page alone, on a warehouse built earlier"
	@echo "make test           run unit tests"
	@echo "make check          lint + format check + type-check + tests (what CI runs)"
	@echo "make eval-check     gold queries as their roles, labels, contamination (no LLM)"
	@echo "make eval-baseline  config 1 on dev (Batch API; needs ANTHROPIC_API_KEY)"
	@echo "make redteam        the 50 red-team prompts on config 4 (fails on any leak)"
	@echo "The warehouse lives in warehouse-graph: 'make policies' there (eval fixture: docs/eval.md)."

test:
	uv run pytest -q

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

check: lint typecheck test

eval-check:
	uv run warehouse-qa eval validate
	uv run warehouse-qa eval label --check
	uv run warehouse-qa eval contamination --write docs/contamination.md

eval-baseline:
	uv run warehouse-qa eval run --config configs/baseline1.yaml --split dev

redteam:
	uv run warehouse-qa eval redteam --config configs/config4.yaml

demo:
	uv run python scripts/demo.py

page:
	uv run python scripts/demo.py --page-only
