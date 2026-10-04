# HarborWatch

[![tests](https://github.com/GiuseppeSaluto/HarborWatch/actions/workflows/tests.yml/badge.svg)](https://github.com/GiuseppeSaluto/HarborWatch/actions/workflows/tests.yml)

Real-time port congestion monitor for the Port of Genoa, built on AIS data (AISStream + MongoDB Atlas + Streamlit), evolving into a berth allocation optimizer.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env           # then fill in the values
```

## Run

```bash
./run.sh
```

Starts the ingestion in the background and the dashboard at http://localhost:8501. Ctrl+C stops both.

## Status

Work in progress: phase 1 (congestion monitor).

## Data

`data/coastline.json` is the OpenStreetMap coastline around the port, © OpenStreetMap contributors, available under the [ODbL](https://www.openstreetmap.org/copyright).
