"""Shared test setup: fake environment variables so `config` imports without a .env.

No live services are used anywhere in the suite (no MongoDB, no AISStream).
"""

import os

# Assumption: config.py reads the Atlas connection string and the AISStream key
# from environment variables at import time and fails if they are missing. The
# spec (section 4, section 10 "conftest.py: fake env vars to import config") does
# not name the variables, so the most likely names are all set here. setdefault
# keeps any real value the developer already has in the shell.
for _name, _value in {
    "MONGODB_URI": "mongodb://localhost:27017/harborwatch-test",
    "MONGO_URI": "mongodb://localhost:27017/harborwatch-test",
    "ATLAS_URI": "mongodb://localhost:27017/harborwatch-test",
    "MONGODB_URL": "mongodb://localhost:27017/harborwatch-test",
    "AISSTREAM_API_KEY": "test-key",
    "AISSTREAM_KEY": "test-key",
    "AIS_API_KEY": "test-key",
}.items():
    os.environ.setdefault(_name, _value)
