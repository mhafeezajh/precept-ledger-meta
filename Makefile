# Load .env if present so make targets see DATABASE_URL etc. .env is git-ignored.
-include .env
export

.PHONY: check lint typecheck test test-integration vectors secrets db-up db-schema db-down dev-keys serve witnesses gate demo-gate demo-audit demo-rogue ots-upgrade

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

# Re-applies ledger/schema.sql then gate/schema.sql (idempotent) so an existing volume picks up
# new tables. Recreate the container after adding a schema file: it is mounted at start.
db-schema: db-up
	docker compose exec -T -e PGOPTIONS='-c client_min_messages=warning' postgres sh -c 'psql -v ON_ERROR_STOP=1 -q -U postgres -d "$$POSTGRES_DB" -f /schema/ledger.sql -f /schema/gate.sql'

# Creates TEST-grade keys in KEYS_DIR (outside the repo) if they are not there yet.
dev-keys:
	uv run python scripts/dev_keys.py

serve: db-schema
	uv run pl-ledger

# Three witnesses on 127.0.0.1:8091-8093, each with its own key and state directory.
# Ctrl-C stops all three. Point the ledger at them with WITNESSES (see .env.example).
witnesses:
	@trap 'kill 0' INT TERM; \
	for n in 1 2 3; do \
	  WITNESS_NAME=witness-$$n \
	  WITNESS_KEY_FILE="$(KEYS_DIR)/witnesses/witness-$$n.pem" \
	  WITNESS_LOGS_FILE="$(KEYS_DIR)/log-verifiers.txt" \
	  WITNESS_STATE_DIR=var/witness-$$n \
	  WITNESS_PORT=809$$n \
	  uv run pl-witness & \
	done; wait

# Gate on 127.0.0.1:8070, in front of the ledger at GATE_LEDGER_URL (make serve).
gate: db-schema
	uv run pl-gate

# Demo beat 2 against make serve + make gate: a $48,000 payment held until j.ortiz approves.
demo-gate:
	uv run python scripts/demo_gate.py

# Demo beat 3 against make serve: an examiner's question answered with proven claims (needs
# META_API_KEY). ARGS="--inject-fault" shows a wrong claim rejected.
demo-audit:
	uv run python scripts/demo_audit.py $(ARGS)

# Demo beat 5 in one process: the rogue operator rewrites history and is caught.
demo-rogue:
	uv run python scripts/demo_rogue_operator.py

db-down:
	docker compose down -v

# O5: upgrade the pending OpenTimestamps vectors once Bitcoin has confirmed them (hours later).
ots-upgrade:
	uv run python scripts/upgrade_ots_vectors.py
