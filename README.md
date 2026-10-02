# HarborWatch

Real-time port congestion monitor for the Port of Genoa, built on AIS data (AISStream + MongoDB Atlas + Streamlit), evolving into a berth allocation optimizer.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env           # then fill in the values
set -a && . ./.env && set +a   # load the variables into the shell
```

## Run

```bash
python src/ingest.py
streamlit run src/app.py
```

## Status

Work in progress: phase 1 (congestion monitor).
