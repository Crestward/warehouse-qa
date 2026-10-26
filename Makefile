.PHONY: help test lint typecheck check eval-check eval-baseline

help:
	@echo "make test           run unit tests"
	@echo "make check          lint + format check + type-check + tests (what CI runs)"
	@echo "make eval-check     gold queries as their roles, labels, contamination (no LLM)"
	@echo "make eval-baseline  config 1 on dev (Batch API; needs ANTHROPIC_API_KEY)"
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
