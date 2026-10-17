# Sample data

A week of data collected by HarborWatch over the Port of Genoa, from 2026-10-09 20:00 to 2026-10-16 20:00 UTC, for exploring the project without a database or an API key. One JSON object per line; timestamps are UTC.

| File | Content |
|---|---|
| `congestion_hourly.jsonl` | Commercial vessels (cargo, tanker, passenger) per state and hour, the series behind the congestion chart: `hour`, `anchored`, `at_berth`, `underway`. All 168 hours. |
| `stays.jsonl` | Complete stays at berth found in the history: `mmsi`, `start`, `end`. |
| `vessels.jsonl` | The vessels in the other files: `mmsi`, `name`, `ship_type` (AIS code), `category`, `length` and `width` in metres. Fields the vessel never broadcast are null. |
| `positions-2026-10-15.jsonl.gz` | Every stored position of one day, 2026-10-15 in local time (Europe/Rome): `ts`, `mmsi`, `lon`, `lat`, `sog` (knots), `nav_status` (AIS code), `state` (`underway`, `at_berth`, `anchored`). |

Notes:

- The stays include harbour boats and service craft, often a few minutes long. The dashboard measures expected stays only from vessels of at least 100 m and stays of at least 1 hour (`MIN_STAY_VESSEL_M` and `MIN_STAY_MINUTES` in `src/config.py`).
- A vessel that sent positions but never its static data has no entry in `vessels.jsonl`.

Reading a file with pandas:

```python
import pandas as pd

stays = pd.read_json("data/sample/stays.jsonl", lines=True)
positions = pd.read_json("data/sample/positions-2026-10-15.jsonl.gz", lines=True)
```

AIS data received via [aisstream.io](https://aisstream.io); sample provided for demonstration, non-commercial use.
