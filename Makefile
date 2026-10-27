# Load .env if present so make targets see DATABASE_URL etc. .env is git-ignored.
-include .env
export

.PHONY: check lint typecheck test test-integration vectors secrets db-up db-schema db-down

check: lint typecheck test secrets

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

test:
	uv run pytest --cov --cov-report=term-missing

# Needs Docker. Starts Postgres from docker-compose.yml and runs tests marked integration.
test-integration: db-schema
	uv run pytest -m integration --no-cov

vectors:
	uv run python scripts/make_vectors.py

secrets:
	gitleaks dir . --no-banner --redact

db-up:
	docker compose up -d --wait postgres

# Re-applies ledger/schema.sql (idempotent) so an existing volume picks up new tables.
db-schema: db-up
	docker compose exec -T -e PGOPTIONS='-c client_min_messages=warning' postgres sh -c 'psql -v ON_ERROR_STOP=1 -q -U postgres -d "$$POSTGRES_DB" -f /schema/ledger.sql'

db-down:
	docker compose down -v
