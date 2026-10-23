.PHONY: help test lint typecheck check

help:
	@echo "make test    run unit tests"
	@echo "make check   lint + format check + type-check + tests (what CI runs)"
	@echo "Local Postgres lives in warehouse-graph: run 'make up' there."

test:
	uv run pytest -q

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

check: lint typecheck test
