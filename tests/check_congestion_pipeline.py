"""Manual check: history.congestion_pipeline must count what the spec says
(spec section 6 "congestion_hourly", section 8 point 7, section 13).

The spec gives no pure Python reference for this pipeline, so this script has
its own reference count (`reference`, below) written straight from the
section 13 rule:
  - only positions with ts >= since and a `state` field;
  - one document per UTC hour that has at least one such position;
  - each of anchored / at_berth / underway is the number of distinct mmsi in
    `commercial` seen in that state in that hour;
  - a vessel seen in two states in one hour counts in both;
  - an hour with only non-commercial vessels is all zeros.

This needs a real MongoDB, so it is a standalone script, not a pytest test (the
filename does not match test_*.py and pytest does not collect it). It writes
synthetic positions (handcrafted edge cases plus random ones) into a throwaway
collection, runs the pipeline there and compares the result with the
reference. The throwaway collection is always dropped at the end; the real
`positions` and `congestion_hourly` collections are never read or written.

Usage (from the repo root):

    HARBORWATCH_CHECK_URI="mongodb+srv://..." python tests/check_congestion_pipeline.py
    python tests/check_congestion_pipeline.py --rounds 20 --vessels 40 --seed 7

Environment:
    HARBORWATCH_CHECK_URI  connection string (falls back to MONGODB_URI)
    HARBORWATCH_CHECK_DB   database name (default "harborwatch": the Atlas user
                           is limited to that database)

Exit code 0 when every round matches, 1 on any mismatch, 2 on bad setup.
The env variables that config.py needs at import time must be set too (for
example by sourcing .env).

Known limit: the reference is written from the spec text by the same author as
the script; if both misread the spec in the same way, they agree and the check
passes. The random generator uses whole-millisecond timestamps (MongoDB's
precision) and does not imitate real traffic.
"""

import argparse
import os
import random
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import history  # noqa: E402

STATES = ("anchored", "at_berth", "underway")
T0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


def aware(t):
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def hour_of(t):
    return aware(t).astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def reference(docs, since, commercial):
    """Pure-Python count of the section 13 rule; returns {hour: (anch, berth, under)}."""
    commercial = set(commercial)
    seen = defaultdict(lambda: {s: set() for s in STATES})
    for d in docs:
        if "state" not in d or d["state"] is None or aware(d["ts"]) < since:
            continue
        bucket = seen[hour_of(d["ts"])]            # the hour exists even if no vessel counts
        if d["mmsi"] in commercial and d["state"] in bucket:
            bucket[d["state"]].add(d["mmsi"])
    return {h: tuple(len(b[s]) for s in STATES) for h, b in seen.items()}


def from_pipeline(rows):
    out = {}
    for r in rows:
        out[hour_of(r["_id"])] = tuple(int(r.get(s, 0)) for s in STATES)
    return out


def pos(mmsi, t, state):
    d = {"mmsi": mmsi, "ts": t, "sog": 0.0, "nav_status": 0,
         "location": {"type": "Point", "coordinates": [8.9, 44.4]}}
    if state is not None:
        d["state"] = state
    return d


def handcrafted():
    """Returns (docs, since, commercial) covering the edge cases of the rule."""
    since = T0 + timedelta(hours=2)
    c1, c2, c3 = 100, 101, 102          # commercial
    tug = 900                           # not commercial
    m = timedelta(minutes=1)
    docs = [
        # Before since: ignored entirely (no document for hour 1).
        pos(c1, since - m, "anchored"),
        pos(c1, T0 + timedelta(hours=1), "underway"),
        # Exactly at since: included.
        pos(c1, since, "anchored"),
        # Same vessel, same state, many fixes in one hour: counted once.
        pos(c2, since + 5 * m, "anchored"),
        pos(c2, since + 15 * m, "anchored"),
        pos(c2, since + 59 * m, "anchored"),
        # One vessel in two states in the same hour: counted in both.
        pos(c3, since + 10 * m, "underway"),
        pos(c3, since + 40 * m, "at_berth"),
        # Missing state field: ignored.
        pos(c1, since + 20 * m, None),
        # Non-commercial vessel: does not count.
        pos(tug, since + 30 * m, "underway"),
        # Next hour starts exactly at :00.
        pos(c1, since + timedelta(hours=1), "at_berth"),
        # An hour with only a non-commercial vessel: present, all zeros.
        pos(tug, since + timedelta(hours=3, minutes=10), "anchored"),
        # An hour with only a stateless commercial position: no document.
        pos(c1, since + timedelta(hours=4, minutes=10), None),
        # Hour 5 has no positions at all: no document (a gap, not a zero).
        pos(c2, since + timedelta(hours=6, minutes=59, seconds=59), "underway"),
    ]
    return docs, since, [c1, c2, c3]


def random_case(rng, vessels):
    since = T0 + timedelta(hours=rng.randint(0, 5), minutes=rng.randint(0, 59))
    mmsis = [200_000_000 + v for v in range(vessels)]
    commercial = [m for m in mmsis if rng.random() < 0.6]
    docs = []
    for m in mmsis:
        t = T0 + timedelta(minutes=rng.randint(0, 120))
        state = rng.choice(STATES)
        for _ in range(rng.randint(5, 120)):
            if rng.random() < 0.1:
                state = rng.choice(STATES)
            docs.append(pos(m, t, None if rng.random() < 0.03 else state))
            step = rng.choice([1, 1, 1, 2, 5]) if rng.random() > 0.05 else rng.randint(60, 300)
            t += timedelta(minutes=step, milliseconds=rng.randint(0, 999))
    return docs, since, commercial


def compare(collection, docs, since, commercial, label):
    collection.delete_many({})
    if docs:
        collection.insert_many([dict(d) for d in docs])
    expected = reference(docs, since, commercial)
    got = from_pipeline(collection.aggregate(history.congestion_pipeline(since, list(commercial))))
    if expected == got:
        print(f"{label}: OK ({len(expected)} hours, {len(docs)} positions)")
        return True
    print(f"{label}: MISMATCH ({len(expected)} hours expected, {len(got)} from pipeline)")
    shown = 0
    for h in sorted(set(expected) | set(got)):
        if expected.get(h) != got.get(h):
            print(f"  {h.isoformat()}  expected {expected.get(h)}  pipeline {got.get(h)}"
                  f"  (anchored, at_berth, underway)")
            shown += 1
            if shown >= 10:
                break
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--vessels", type=int, default=30)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    uri = os.environ.get("HARBORWATCH_CHECK_URI") or os.environ.get("MONGODB_URI")
    if not uri:
        print("Set HARBORWATCH_CHECK_URI (or MONGODB_URI).", file=sys.stderr)
        return 2
    seed = args.seed if args.seed is not None else random.randrange(1_000_000)
    print(f"seed={seed}")
    rng = random.Random(seed)

    from pymongo import MongoClient

    client = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=10_000)
    db = client[os.environ.get("HARBORWATCH_CHECK_DB", "harborwatch")]
    collection = db[f"check_congestion_{uuid.uuid4().hex[:12]}"]
    ok = True
    try:
        ok &= compare(collection, *handcrafted(), "handcrafted")
        for r in range(args.rounds):
            ok &= compare(collection, *random_case(rng, args.vessels), f"random round {r}")
    finally:
        collection.drop()
        client.close()
    print("ALL MATCH" if ok else f"MISMATCHES FOUND (rerun with --seed {seed})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
