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

# Dashboard times are the port's local time, whoever looks at it and from wherever.
TIMEZONE = "Europe/Rome"

# Dashboard: vessels not heard from in this long are considered gone (left the area,
# or AIS switched off). Moored and anchored vessels transmit every few minutes.
STALE_MINUTES = 30

# Data count as live while the last position is at most this old: AIS delay (~10 s) plus
# FLUSH_SECONDS, with margin.
LIVE_MINUTES = 2

# Diagnostics: how long the on-demand AISStream probe listens.
PROBE_SECONDS = 20

# Map tracks: how far back to draw, and how far a vessel must have moved from its first
# position in that window to get one (GPS jitter at berth or an anchor swing stays below).
TRACK_HOURS = 3
TRACK_MIN_MOVE_M = 500

# Vessel state thresholds (see classify.py).
SPEED_THRESHOLD_KN = 0.5
QUAY_DISTANCE_M = 100

# OpenStreetMap coastline around the port: in Genoa it follows the quay edges.
COASTLINE = json.loads((Path(__file__).parent.parent / "data" / "coastline.json").read_text())

# Phase 2: one entry per berth (a quay stretch hosting one vessel at a time), with the
# vessel categories it accepts. "source" says whether numbers are official or observed.
BERTHS = json.loads((Path(__file__).parent.parent / "data" / "berths.json").read_text())

# Expected stay at berth per category, until it can be measured from history (spec section 9).
# Known limit: rough defaults; container and ro-ro calls are often shorter, tankers longer.
SERVICE_MINUTES = {"cargo": 24 * 60, "tanker": 36 * 60, "passenger": 12 * 60}
# Measured stays replace a default once a category has this many complete ones in history.
MIN_STAYS = 5
# ...and once the history holds a continuous collection window of at least this many times the
# default stay: in a few-hour window only short stays can be seen from arrival to departure,
# so their median would drag a 24 h cargo call down to about an hour.
STAY_WINDOW_FACTOR = 2
# Only stays of ships the plan is about count: harbour boats (16-30 m "passenger" craft)
# dragged the passenger median down to 8 minutes on 2026-10-04, and 50-85 m service craft
# with a cargo or tanker AIS type (bunker barges) made every cargo stay ~1.6 h on 2026-10-10.
MIN_STAY_VESSEL_M = 100
MIN_STAY_MINUTES = 60
# The stays pipeline sorts in memory, capped at 32 MB on Atlas M0 (it failed at ~72k positions
# on 2026-10-10): run it on groups of vessels holding at most this many positions each.
STAY_BATCH_POSITIONS = 20_000
