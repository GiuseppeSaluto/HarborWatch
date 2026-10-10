#!/usr/bin/env bash
# Start HarborWatch: ingestion + dashboard, or the dashboard alone when the ingestion runs
# on another machine.
# Ctrl+C stops everything; the ingestion writes its buffers before exiting.
# Extra arguments go to streamlit, e.g. ./run.sh --server.port 8502
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
    echo "Missing .env: cp .env.example .env and fill in the values." >&2
    exit 1
fi
set -a; . ./.env; set +a

echo "0) ingestion + dashboard"
echo "1) dashboard only (the ingestion runs on another machine)"
# Dashboard only by default: two ingestions would write every position twice.
read -rp "Choice [1]: " choice
case "${choice:-1}" in
    0)
        # Only a python running the script: an editor or a pager on src/ingest.py must not match.
        if pgrep -f "python[0-9.]* src/ingest\.py$" >/dev/null; then
            echo "An ingestion is already running on this machine: choose 1." >&2
            exit 1
        fi
        .venv/bin/python src/ingest.py &
        ingest=$!
        # Background jobs in a script ignore Ctrl+C, so stop the ingestion explicitly on exit.
        # SIGTERM triggers its final flush; wait lets it finish before the script returns.
        trap 'kill "$ingest" 2>/dev/null; wait "$ingest" || true' EXIT
        ;;
    1) ;;
    *)
        echo "Unknown choice: $choice" >&2
        exit 1
        ;;
esac

.venv/bin/streamlit run src/app.py "$@"
