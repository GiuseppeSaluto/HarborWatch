#!/usr/bin/env bash
# Start the ingestion in the background and the dashboard in the foreground.
# Ctrl+C stops both; the ingestion writes its buffers before exiting.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
    echo "Missing .env: cp .env.example .env and fill in the values." >&2
    exit 1
fi
set -a; . ./.env; set +a

.venv/bin/python src/ingest.py &
ingest=$!
# Background jobs in a script ignore Ctrl+C, so stop the ingestion explicitly on exit.
# SIGTERM triggers its final flush; wait lets it finish before the script returns.
trap 'kill "$ingest" 2>/dev/null; wait "$ingest" || true' EXIT

.venv/bin/streamlit run src/app.py "$@"
