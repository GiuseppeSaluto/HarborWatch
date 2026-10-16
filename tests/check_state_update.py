"""Manual check: ingest.state_update must set `since` and `since_seen` as spec
section 13 says (and section 6, `states`).

This needs a real MongoDB, because state_update is a pipeline update that only
MongoDB can run, so it is a standalone script, not a pytest test (the filename
does not match test_*.py and pytest does not collect it). Each scenario writes a
sequence of state_update operations with bulk_write, as the ingestion does, into
a throwaway collection, and after every write compares the stored document with
the expected one. The throwaway collection is always dropped at the end, even
when a case fails or raises; the real `states` collection is never read or
written.

Cases (spec section 13), with "recent" = prev.ts >= fields.ts - STALE_MINUTES:
  - no document before (new vessel): since = fields.ts, since_seen false;
  - same state and recent: since and since_seen kept from prev;
  - different state and recent: since = fields.ts, since_seen true;
  - not recent (same or different state): since = fields.ts, since_seen false;
and the exact boundary: STALE_MINUTES is still recent, STALE_MINUTES plus one
second is not.

Usage (from the repo root):

    HARBORWATCH_CHECK_URI="mongodb+srv://..." python tests/check_state_update.py

Environment:
    HARBORWATCH_CHECK_URI  connection string (falls back to MONGODB_URI)
    HARBORWATCH_CHECK_DB   database name (default "harborwatch": the Atlas user
                           is limited to that database)

Exit code 0 when every case matches, 1 on any mismatch, 2 on bad setup.
The env variables that config.py needs at import time must be set too (for
example by sourcing .env).

Known limit: the `fields` documents are written by hand in the shape of
ingest.state_fields (mmsi, ts, location, sog, state) so that each case can
force the state it needs; state_fields itself is covered by test_ingest.py.
Timestamps are whole seconds, MongoDB's millisecond precision is never at stake.
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config  # noqa: E402
import ingest  # noqa: E402

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


def fields(mmsi, at, state):
    return {"mmsi": mmsi, "ts": T0 + at, "sog": 0.0 if state != "underway" else 8.0,
            "location": {"type": "Point", "coordinates": [8.9, 44.4]}, "state": state}


def scenarios():
    """(name, steps); each step is (offset, state, expected since offset, expected since_seen)."""
    stale = timedelta(minutes=config.STALE_MINUTES)
    second = timedelta(seconds=1)
    m = timedelta(minutes=1)
    zero = timedelta(0)
    return [
        ("new vessel", [
            (zero, "anchored", zero, False),
        ]),
        ("same state and recent keeps since and since_seen (false)", [
            (zero, "anchored", zero, False),
            (5 * m, "anchored", zero, False),
        ]),
        ("same state at exactly STALE_MINUTES keeps since and since_seen (true)", [
            (zero, "anchored", zero, False),
            (10 * m, "at_berth", 10 * m, True),
            (10 * m + stale, "at_berth", 10 * m, True),
        ]),
        ("different state at exactly STALE_MINUTES is a seen change", [
            (zero, "anchored", zero, False),
            (stale, "at_berth", stale, True),
        ]),
        ("same state one second over STALE_MINUTES restarts since, not seen", [
            (zero, "anchored", zero, False),
            (10 * m, "at_berth", 10 * m, True),
            (10 * m + stale + second, "at_berth", 10 * m + stale + second, False),
        ]),
        ("different state one second over STALE_MINUTES restarts since, not seen", [
            (zero, "anchored", zero, False),
            (stale + second, "at_berth", stale + second, False),
        ]),
        ("recent is measured from prev.ts, not from prev.since", [
            (zero, "anchored", zero, False),
            (stale, "anchored", zero, False),
            (2 * stale, "anchored", zero, False),
            (3 * stale, "underway", 3 * stale, True),
        ]),
    ]


def run_scenario(collection, mmsi, name, steps):
    ok = True
    for i, (at, state, since_at, seen) in enumerate(steps):
        f = fields(mmsi, at, state)
        collection.bulk_write([ingest.state_update(f)], ordered=True)
        docs = list(collection.find({"mmsi": mmsi}))
        problems = []
        if len(docs) != 1:
            problems.append(f"{len(docs)} documents for mmsi {mmsi}")
        else:
            doc = docs[0]
            for key, value in f.items():
                if doc.get(key) != value:
                    problems.append(f"{key}={doc.get(key)!r}, expected {value!r}")
            if doc.get("since") != T0 + since_at:
                problems.append(f"since={doc.get('since')}, expected {T0 + since_at}")
            if doc.get("since_seen") is not seen:
                problems.append(f"since_seen={doc.get('since_seen')!r}, expected {seen}")
        if problems:
            ok = False
            print(f"{name}: step {i} MISMATCH: " + "; ".join(problems))
    if ok:
        print(f"{name}: OK ({len(steps)} writes)")
    return ok


def main():
    uri = os.environ.get("HARBORWATCH_CHECK_URI") or os.environ.get("MONGODB_URI")
    if not uri:
        print("Set HARBORWATCH_CHECK_URI (or MONGODB_URI).", file=sys.stderr)
        return 2

    from pymongo import MongoClient

    client = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=10_000)
    db = client[os.environ.get("HARBORWATCH_CHECK_DB", "harborwatch")]
    collection = db[f"check_states_{uuid.uuid4().hex[:12]}"]
    ok = True
    try:
        for i, (name, steps) in enumerate(scenarios()):
            ok &= run_scenario(collection, 300_000_000 + i, name, steps)
        # Writes for one vessel must not touch another one.
        count = collection.count_documents({})
        if count != len(scenarios()):
            ok = False
            print(f"isolation: MISMATCH ({count} documents, expected {len(scenarios())})")
    finally:
        collection.drop()
        client.close()
    print("ALL MATCH" if ok else "MISMATCHES FOUND")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
