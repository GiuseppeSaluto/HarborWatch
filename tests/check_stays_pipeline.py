"""Check that stays_pipeline (MongoDB) finds exactly the stays measure_stays (Python) finds.

Needs Atlas, so pytest does not collect it (no test_ prefix). Run it after touching either:
    set -a; . ./.env; set +a; PYTHONPATH=src python tests/check_stays_pipeline.py
It writes 200 random histories (one-position blips, gaps just under and over the threshold)
to a temporary collection, compares the two, and drops the collection.
"""

import random
from datetime import UTC, datetime, timedelta

from pymongo import MongoClient

import config
from optimize import measure_stays, stays_pipeline

GAP = config.STALE_MINUTES
STATES = ["at_berth", "anchored", "underway"]


def random_positions(histories=200, vessels=6):
    docs, t0 = [], datetime(2026, 1, 1, tzinfo=UTC)
    for seed in range(histories):
        rnd = random.Random(seed)
        for v in range(vessels):
            ts, state = t0, rnd.choice(STATES)
            for _ in range(rnd.randint(1, 40)):
                ts += timedelta(minutes=rnd.choice([1, 2, 5, 10, GAP - 1, GAP, GAP + 1, 45]))
                if rnd.random() < 0.15:  # one-position blip, the state goes on unchanged
                    docs.append({"mmsi": seed * 100 + v, "ts": ts, "state": rnd.choice(STATES)})
                    continue
                if rnd.random() < 0.25:
                    state = rnd.choice(STATES)
                docs.append({"mmsi": seed * 100 + v, "ts": ts, "state": state})
    return docs


def main():
    db = MongoClient(config.MONGODB_URI, tz_aware=True)[config.DB_NAME]
    docs = random_positions()
    key = lambda stays: sorted((m, s.replace(tzinfo=None), e.replace(tzinfo=None)) for m, s, e in stays)
    expected = key(measure_stays(sorted(docs, key=lambda d: (d["mmsi"], d["ts"])), GAP))
    coll = db["stays_check"]
    try:
        coll.insert_many([dict(d) for d in docs])
        got = key((d["mmsi"], d["start"], d["end"]) for d in coll.aggregate(stays_pipeline(GAP)))
    finally:
        coll.drop()
    assert got == expected, f"{len(set(got) ^ set(expected))} stays differ"
    print(f"OK: {len(expected)} stays identical over {len(docs)} random positions")


if __name__ == "__main__":
    main()
