# Load .env if present so make targets see DATABASE_URL etc. .env is git-ignored.
-include .env
export

.PHONY: check lint typecheck test test-integration vectors secrets db-up db-down

check: lint typecheck test secrets

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

test:
	uv run pytest --cov --cov-report=term-missing

# Needs Docker. Starts Postgres from docker-compose.yml and runs tests marked integration.
test-integration: db-up
	uv run pytest -m integration --no-cov

vectors:
	uv run python scripts/make_vectors.py

secrets:
	gitleaks dir . --no-banner --redact

db-up:
	docker compose up -d --wait postgres

db-down:
	docker compose down -v
