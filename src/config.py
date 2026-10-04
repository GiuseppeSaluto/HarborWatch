"""Port, bounding box, berths, thresholds."""

import json
import os
from pathlib import Path

# Secrets come from the environment (.env, never committed).
# os.environ[...] fails fast with a KeyError if a variable is missing.
AISSTREAM_API_KEY = os.environ["AISSTREAM_API_KEY"]
MONGODB_URI = os.environ["MONGODB_URI"]

DB_NAME = "harborwatch"

# Port of Genoa, from Voltri to Porto Antico plus the southern anchorage.
# AISStream format: two corners as [lat, lon].
# Careful: GeoJSON stored in MongoDB uses the opposite order, [lon, lat]!!!.
BOUNDING_BOX = [[44.33, 8.70], [44.46, 8.98]]

# Keep at most one stored position per vessel every SAMPLE_SECONDS (Atlas M0 limits).
SAMPLE_SECONDS = 60

# Buffered writes go to MongoDB at most every FLUSH_SECONDS (Atlas M0: 100 ops/s).
FLUSH_SECONDS = 10

# TTL on the positions collection, to stay under the 512 MB of Atlas M0.
POSITIONS_TTL_DAYS = 7

# Storage check: Atlas M0 allows 512 MB, indexes included. A warning is logged when the
# current or projected size goes over STORAGE_WARN_RATIO of it; the fix is a lower
# POSITIONS_TTL_DAYS or a higher SAMPLE_SECONDS, then a restart.
STORAGE_LIMIT_MB = 512
STORAGE_WARN_RATIO = 0.8
STATS_SECONDS = 600

# Dashboard: vessels not heard from in this long are considered gone (left the area,
# or AIS switched off). Moored and anchored vessels transmit every few minutes.
STALE_MINUTES = 30

# Vessel state thresholds (see classify.py).
SPEED_THRESHOLD_KN = 0.5
QUAY_DISTANCE_M = 100

# OpenStreetMap coastline around the port: in Genoa it follows the quay edges.
COASTLINE = json.loads((Path(__file__).parent.parent / "data" / "coastline.json").read_text())

# Phase 2: one entry per berth (a quay stretch hosting one vessel at a time), with the
# vessel categories it accepts. "source" says whether numbers are official or observed.
BERTHS = json.loads((Path(__file__).parent.parent / "data" / "berths.json").read_text())

# Expected stay at berth per category, until it can be measured from history (spec section 9).
# ponytail: rough defaults; container and ro-ro calls are often shorter, tankers longer.
SERVICE_MINUTES = {"cargo": 24 * 60, "tanker": 36 * 60, "passenger": 12 * 60}
# Measured stays replace a default once a category has this many complete ones in history.
MIN_STAYS = 5
# Only stays of ships the plan is about count: harbour boats (16-30 m "passenger" craft)
# dock for minutes and dragged the passenger median down to 8 minutes on 2026-10-04.
MIN_STAY_VESSEL_M = 50
MIN_STAY_MINUTES = 60
