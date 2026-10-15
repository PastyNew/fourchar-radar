.PHONY: help install dev test lint fmt typecheck check run build clean

PY ?= python3

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install the package
	$(PY) -m pip install -e .

dev: ## Install with dev + bot extras
	$(PY) -m pip install -e ".[bot,dev]"

test: ## Run the test suite
	$(PY) -m pytest

lint: ## Lint with ruff
	$(PY) -m ruff check src tests

fmt: ## Auto-fix + format with ruff
	$(PY) -m ruff check --fix src tests
	$(PY) -m ruff format src tests

typecheck: ## Static types with mypy
	$(PY) -m mypy

check: lint typecheck test ## Everything CI runs

run: ## Start the radar using .env
	$(PY) -m fourchar_radar run

build: ## Build sdist + wheel
	$(PY) -m build

clean:
	rm -rf build dist .pytest_cache .ruff_cache .mypy_cache src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +