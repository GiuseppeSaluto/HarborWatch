#!/usr/bin/env bash
# Start HarborWatch: the ingestion and the dashboard together, or the dashboard alone when
# the ingestion already runs on another machine.
# Ctrl+C stops everything; the ingestion writes its buffers before exiting.
# Extra arguments go to streamlit, e.g. ./run.sh --server.port 8502
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
    echo "Missing .env: cp .env.example .env and fill in the values." >&2
    exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
    echo "Missing .venv: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi
set -a; . ./.env; set +a

cat <<'MENU'
HarborWatch

  0  Ingestion + dashboard   collect live AIS data on this machine and show it
  1  Dashboard only          show the data collected by an ingestion running elsewhere

MENU
# No default: an Enter pressed by habit must not start a second ingestion (every position
# would be written twice). Ask again until the answer is 0 or 1.
choice=""
until [[ $choice == 0 || $choice == 1 ]]; do
    read -rp "Choose 0 or 1: " choice
done
case "$choice" in
    0)
        # Only a python running the script: an editor or a pager on src/ingest.py must not match.
        if pgrep -f "python[0-9.]* src/ingest\.py$" >/dev/null; then
            echo "An ingestion is already running on this machine: choose 1." >&2
            exit 1
        fi
        echo "Starting the ingestion in the background (its log lines appear below)."
        .venv/bin/python src/ingest.py &
        ingest=$!
        # Background jobs in a script ignore Ctrl+C, so stop the ingestion explicitly on exit.
        # SIGTERM triggers its final flush; wait lets it finish before the script returns.
        trap 'kill "$ingest" 2>/dev/null; wait "$ingest" || true' EXIT
        ;;
    1) ;;
esac

echo "Starting the dashboard. Press Ctrl+C to stop."
.venv/bin/streamlit run src/app.py "$@"
