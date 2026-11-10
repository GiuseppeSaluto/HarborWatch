# HarborWatch

[![tests](https://github.com/GiuseppeSaluto/HarborWatch/actions/workflows/tests.yml/badge.svg)](https://github.com/GiuseppeSaluto/HarborWatch/actions/workflows/tests.yml)

Real-time port congestion monitor and berth allocation optimizer for the Port of Genoa, built on live AIS ship-tracking data.

![Dashboard: data status, vessels by state, port map with vessel tracks, vessels waiting at anchor and hourly congestion](docs/dashboard.png)

## What it does

- **Monitors the port live.** Every vessel in the port area is classified as *at berth*, *at anchor* or *underway*, and the dashboard shows the counts, a map where each vessel's icon tells its type (container ship, tanker, ferry, tug, sailing boat) and its color the state, with the paths of the vessels that moved in the last 3 hours, and how long commercial vessels have been waiting at anchor. Each state card can hide or show its vessels on the map.
- **Tracks congestion over time.** An hourly series of commercial vessels at anchor, with gaps where no data was collected rather than misleading zeros.
- **Proposes a berth plan.** A constraint-programming model (OR-Tools CP-SAT) assigns every waiting vessel a compatible berth and a mooring time, minimizing the total wait, around the vessels already moored.
- **Says how far to trust itself.** A status badge and panel show data freshness, continuous collection time, which stay durations are measured and which are still defaults, and database usage. Vessels the plan cannot place are listed, not dropped.
- **Explains any vessel.** Clicking a vessel on the map, or searching it by name or MMSI, opens its card: type and length, state and how long it has been in it, the last signal, its proposed berth or the reason it is not in the plan, and its route over the last 3 hours, 24 hours or 7 days with the observed state changes and data gaps.
- **Replays the recorded week.** A Live/Replay switch shows the port as it was at any past moment of the stored positions: a timeline under the map, with a helm for a thumb, moves the vessels in the browser while dragging, and on release the counters, the anchorage list, the chart, the berth plan and the vessel card follow, using only data up to that moment.
- **Diagnoses its own outages.** When data stop, the Status panel tells which link broke: the database, the ingestion, or the AIS feed itself (no coverage over the port, or the service down).

Hovering the map shows each vessel's name, type, length, state and speed, and each berth area's berths and accepted vessel types:

![Map tooltip on a tanker waiting at anchor](docs/map-tooltip.png)

Below the map and the vessels waiting at anchor, the proposed berth plan, with its table view opened:

![Proposed berth plan as a Gantt chart, and its table view](docs/berth-plan.png)

## How it works

```
AISStream (WebSocket) ──> ingest.py ──> MongoDB Atlas ──> app.py (Streamlit)
                          validate        positions          map, counters,
                          classify        vessels            congestion chart,
                          sample, batch   states             berth plan
                                              │
                                              └──> optimize.py (OR-Tools CP-SAT)
```

- **Ingestion** (`src/ingest.py`): an asyncio client subscribes to the port's bounding box on [AISStream](https://aisstream.io) and, with a token, on [Open Waters AIS](https://openwaters.io/ais/) (two readers feeding one queue and a single writer), validates every message (AIS uses sentinel values such as latitude 91 for "not available") and writes to MongoDB with PyMongo's async API. It reconnects on its own and flushes its buffers on shutdown.
- **Classification** (`src/classify.py`): a vessel is *underway* above 0.5 knots; otherwise it is *at berth* when within 100 m of the coastline, whose OpenStreetMap trace follows the quay edges in Genoa, and *at anchor* when further out. The AIS ship type separates commercial traffic (cargo, tanker, passenger) from tugs, pilot boats and yachts: during the 2026 Genoa Boat Show dozens of 50-90 m superyachts were moored in port, and length alone could not tell them from cargo ships.
- **Storage under free-tier limits**: MongoDB Atlas M0 allows 512 MB and 100 operations per second. Positions are sampled to one per vessel per minute, written in batches every 10 seconds, and expire after 7 days through a TTL index. The ingestion logs the used and projected storage (about 57 MB at steady state, measured).
- **State history**: each vessel's current state is upserted with the time it began, computed inside MongoDB with a pipeline update so that it survives restarts. After a gap in the data the start time is reset and flagged as "not seen", instead of silently counting the gap as waiting time.
- **Berth plan** (`src/optimize.py`): berths come from `data/berths.json`, 29 berths with their length and accepted vessel categories, each marked as official or observed. Each waiting vessel gets an optional interval on every compatible berth; `NoOverlap` per berth keeps one vessel at a time, including the vessels already moored. Expected stays are measured from complete stays in the history once a category has enough of them, seen in a continuous collection window at least twice its default stay (in a few-hour window only short stays can be seen from start to end), and fall back to per-category defaults until then.

## Run it

Requirements: Python 3.12+, a free [AISStream](https://aisstream.io) API key, and a MongoDB Atlas cluster (the free M0 tier is enough) with your IP in its access list.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env           # then fill in AISSTREAM_API_KEY and MONGODB_URI (OPENWATERS_TOKEN optional)
./run.sh
```

`run.sh` asks what to start: `0` the ingestion in the background plus the dashboard at http://localhost:8501, or `1` the dashboard alone, when the ingestion runs on another machine. There is no default, so an Enter pressed by habit cannot start a second ingestion. Ctrl+C stops everything.

The database is `harborwatch` unless `MONGODB_DB` in `.env` names another one, for instance a local copy of the data (`MONGODB_URI=mongodb://localhost:27017`) to develop on. It moves the dashboard and the ingestion started by `run.sh` alike, so change it only on a machine that does not ingest. Some public networks block outbound port 27017, which makes Atlas unreachable.

## Tests

```bash
pytest
```

The suite runs in about five seconds, without network or database, on every push (GitHub Actions, Python 3.12 and 3.14). It covers message validation, classification thresholds and the coastline distance, the berth data, the optimizer (expected optima checked by brute force on small cases), berth occupancy, stay measurement, the hourly history behind the charts, the replay (the port's state at a past moment and the thinned positions behind it), the outage diagnosis, the map icons, and the dashboard itself through Streamlit's AppTest (vessel card, Live and Replay, Atlas unreachable). The tests are written by a separate agent from the specification alone, without reading the implementation; the code adapts to them. Three integration scripts in `tests/integration/` run by hand against a real MongoDB, on a throwaway collection: the aggregation finding the stays must agree with the tested Python definition, the hourly congestion aggregation with a count done in Python, and the pipeline update that tracks when each vessel's state began must handle new vessels, state changes and gaps.

## Known limitations

- **The berth plan is a deterministic model.** No tides, drafts, pilotage windows or commercial priorities (a tanker may wait at anchor for a cargo window even with a berth free); berths are one point per terminal area. Berth data is official where published (the six Multedo oil berths, PSA terminals, cruise piers) and observed elsewhere; observed values can only be lower bounds, so a vessel that fits no known berth is listed as not planned rather than dropped.
- **Stay durations are defaults until enough history exists.** Container and tanker calls last one or two days, so measuring them needs days of uninterrupted collection.
- **Vessels moored before the ingestion started** are assumed to be halfway through their stay.
- **Classification is heuristic.** A slow manoeuvring vessel can be misclassified, a vessel waiting near a quay counts as at berth, and AIS coverage has gaps. These corners are marked `Known limit:` in the code, each with its upgrade path.

## Project layout

```
src/        ingest.py       AIS client: validation, sampling, batched writes
            classify.py     vessel state and category
            history.py      stays, hourly series, archives, replay state at a moment
            optimize.py     berth plan (OR-Tools CP-SAT)
            diagnostics.py  why data stopped
            icons.py        map icons per vessel category
            app.py          Streamlit dashboard; app_charts.py, app_replay.py,
                            app_vessel.py, app_theme.py: its map, charts, replay
                            timeline, vessel card and theme
            config.py       thresholds and settings
data/       coastline.json (OSM coastline), berths.json (berths of the port)
  sample/   one week of collected data, readable without a database
tests/      one test file per module with non-trivial logic
  integration/  checks against a real MongoDB, run by hand
run.sh      ingestion + dashboard, or the dashboard alone
```

## Data

- Vessel positions: [AISStream](https://aisstream.io) live AIS feed and, in parallel when `OPENWATERS_TOKEN` is set, [Open Waters AIS (https://openwaters.io/ais/)](https://openwaters.io/ais/), an open network of volunteer receivers and aggregators. Each position keeps its source, and the dashboard credits every source it shows data from, with the attribution that source asks for.
- `data/sample/` holds one week of collected data (hourly congestion, stays, vessels, and one day of positions) for demonstration, non-commercial use; see its [README](data/sample/README.md).
- `data/coastline.json` is the OpenStreetMap coastline around the port, © OpenStreetMap contributors, available under the [ODbL](https://www.openstreetmap.org/copyright).
- `data/berths.json` combines quay lengths published by the [Port Authority of Genoa](https://www.portsofgenoa.com) with values observed in the collected data, as noted in each entry.

## License

The code is released under the [MIT License](LICENSE). The data files keep their own terms: `data/coastline.json` is under the ODbL (see Data above).
