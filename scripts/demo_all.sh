#!/usr/bin/env bash
# Every demo beat from a clean stream, headless (make demo), or a clean stream ready for
# recording with the Console and the Approvals app left running (make video-seed).
#
# Wipes the local demo database and witness state first: development machines only. Asks
# before wiping unless YES=1. Stop any running ledger, witnesses, Gate and web apps first.
# Models: DEMO_PROFILE=mock (default; offline, labelled mock/scripted) or DEMO_PROFILE=cloud
# (Muse through the Meta Model API, needs META_API_KEY in .env).
#
# Usage: scripts/demo_all.sh [--video]
set -euo pipefail
set -m  # each service in its own process group, so stopping one never reaches this script

cd "$(dirname "$0")/.."
MODE="${1:-demo}"
PROFILE="${DEMO_PROFILE:-mock}"
LOGS=var/demo-logs
PORTS=(8070 8080 8090 8091 8092 8093 8094)
PIDS=()

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

stop_all() {
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] && kill -- "-$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}

wait_for() {  # url, name: up to 60 s, then fail closed with the service's log
  for _ in $(seq 120); do
    curl -fsS "$1" >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  echo "$2 did not start; see $LOGS/$2.log" >&2
  exit 1
}

start() {  # name, make target and arguments
  local name="$1"; shift
  make "$@" >"$LOGS/$name.log" 2>&1 &
  PIDS+=("$!")
}

[ -f .env ] || { echo "No .env: cp .env.example .env and set KEYS_DIR (outside the repo)" >&2; exit 1; }
for port in "${PORTS[@]}"; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $port is in use: stop the running services first" >&2
    exit 1
  fi
done
if [ "${YES:-}" != "1" ]; then
  read -r -p "This deletes the local ledger, escalations and witness state. Continue? [y/N] " answer
  [ "$answer" = "y" ] || exit 1
fi

trap stop_all EXIT
mkdir -p "$LOGS"

say "Clean stream"
docker compose down -v >/dev/null 2>&1
rm -rf var/checkpoints var/witness-1 var/witness-2 var/witness-3
make db-schema >/dev/null
make dev-keys >/dev/null

say "Services (models: $PROFILE)"
WITNESS_URLS=witness-1=http://127.0.0.1:8091,witness-2=http://127.0.0.1:8092,witness-3=http://127.0.0.1:8093
start witnesses witnesses
wait_for http://127.0.0.1:8091/witness/v1/health witnesses
wait_for http://127.0.0.1:8093/witness/v1/health witnesses
# TSAS empty: the seeded week is backdated, and timestamps would report it as late_timestamp
start ledger serve WITNESSES="$WITNESS_URLS" TSAS= OTS_CALENDARS=
wait_for http://127.0.0.1:8080/ledger/v1/health ledger
start gate gate DEMO_MODE=true MODEL_PROFILE="$PROFILE"
wait_for http://127.0.0.1:8070/gate/v1/health gate
export MODEL_PROFILE="$PROFILE" DEMO_MODE=true

say "A week of decisions"
uv run python scripts/seed_week.py

say "Beat 1: policy to rules, the planted bad rule caught"
uv run python scripts/demo_policy.py

if [ "$MODE" = "--video" ]; then
  start web web DEMO_MODE=true MODEL_PROFILE="$PROFILE"
  start approvals approvals DEMO_MODE=true
  wait_for http://127.0.0.1:8090/ web
  wait_for http://127.0.0.1:8094/ approvals
  say "Ready to record: Console http://127.0.0.1:8090, Approvals http://127.0.0.1:8094 (Ctrl-C stops all)"
  wait
  exit 0
fi

say "Beat 2: Gate holds the \$48,000 payment until j.ortiz approves"
uv run python scripts/demo_gate.py

say "Beat 3: the examiner's question, answered with proven claims"
uv run python scripts/demo_audit.py
say "Beat 3: one wrong claim, rejected"
uv run python scripts/demo_audit.py --inject-fault

say "Beat 4: export, then verify offline"
sleep 12  # one checkpoint interval, so the last records are witnessed
uv run pl-export --out var/demo-logs/export.tar
uv run pl-verify var/demo-logs/export.tar --keys "$KEYS_DIR/trusted.json" \
  --witness-keys "$KEYS_DIR/witnesses.txt" --quorum 2

say "Beat 5: the rogue operator, caught"
uv run python scripts/demo_rogue_operator.py

say "Beat 6: the ledger learns"
uv run python scripts/demo_learn.py

say "All beats ran. Logs in $LOGS"
