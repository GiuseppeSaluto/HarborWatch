"""Port, bounding box, berths, thresholds."""

import os

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

# TTL on the positions collection, to stay under the 512 MB of Atlas M0.
POSITIONS_TTL_DAYS = 7
