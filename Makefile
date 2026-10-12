.PHONY: install check test test-fast lint fmt serve demo doctor models docker

install:
	uv sync

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

fmt:
	uv run ruff check --fix src tests
	uv run ruff format src tests

test:
	mkdir -p .logs
	uv run pytest -q --tb=short > .logs/test.log 2>&1; tail -15 .logs/test.log

test-fast:
	uv run --with pytest-xdist python scripts/test_fast.py

check: lint test

serve:
	uv run onboarding-worker serve

demo:
	uv run onboarding-worker demo

doctor:
	uv run onboarding-worker doctor

models:
	uv run onboarding-worker models

docker:
	docker compose up -d --build
