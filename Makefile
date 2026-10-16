.PHONY: sync test lint format ui

sync:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

ui:
	uv run litsearch ui

format:
	uv run ruff check --fix .
	uv run ruff format .
