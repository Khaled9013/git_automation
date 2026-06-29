.DEFAULT_GOAL := help
.PHONY: help setup web desktop test lint fmt check clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup: ## Create the venv and install all dependencies (incl. dev + desktop)
	uv sync --all-extras --group dev

web: ## Run the FastAPI web app
	uv run uvicorn git_automation.web.app:app --reload

desktop: ## Run the desktop (pywebview) shell
	uv run python -m git_automation.desktop

test: ## Run the test suite
	uv run pytest

lint: ## Lint with ruff
	uv run ruff check .

fmt: ## Auto-format with ruff
	uv run ruff format .
	uv run ruff check --fix .

check: lint test ## Lint then test

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache dist build *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
