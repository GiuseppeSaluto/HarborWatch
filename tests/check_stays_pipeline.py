"""Manual check: optimize.stays_pipeline must find the same stays as
optimize.measure_stays (spec sections 6 and 9).

This needs a real MongoDB, so it is a standalone script, not a pytest test (the
filename does not match test_*.py and pytest does not collect it). It writes
synthetic histories (handcrafted edge cases plus random ones) into a throwaway
collection, runs the aggregation pipeline on it, runs measure_stays on the same
documents in Python, and compares the two sets of (mmsi, start, end). The
throwaway collection is always dropped at the end; the real `positions`
collection is never read or written.

Usage (from the repo root):

    HARBORWATCH_CHECK_URI="mongodb+srv://..." python tests/check_stays_pipeline.py
    python tests/check_stays_pipeline.py --rounds 20 --vessels 30 --seed 7

Environment:
    HARBORWATCH_CHECK_URI  connection string (falls back to MONGODB_URI)
    HARBORWATCH_CHECK_DB   database name (default "harborwatch": the Atlas user
                           is limited to that database)

Exit code 0 when every round matches, 1 on any mismatch, 2 on bad setup.
The env variables that config.py needs at import time must be set too (for
example by sourcing .env).

Known limit: the random generator only produces the three states with
one-minute sampling and whole-second timestamps; it does not imitate real
traffic patterns, so a pass means the two implementations agree on these
shapes, not that both are right (pytest's test_stays.py covers correctness of
measure_stays).
"""

import argparse
import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config  # noqa: E402
import optimize  # noqa: E402

STATES = ("underway", "anchored", "at_berth")
T0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


def track(mmsi, segments, start=T0):
    """segments: list of (state, count) or ("gap", minutes); one fix per minute."""
    out, t = [], start
    for kind, n in segments:
        if kind == "gap":
            t += timedelta(minutes=n)
            continue
        for _ in range(n):
            out.append({"mmsi": mmsi, "ts": t, "state": kind})
            t += timedelta(minutes=1)
    return out


def handcrafted():
    stale = config.STALE_MINUTES
    cases = [
        [("underway", 10), ("at_berth", 120), ("underway", 10)],                 # complete
        [("at_berth", 120), ("underway", 10)],                                    # arrival unseen
        [("underway", 10), ("at_berth", 120)],                                    # departure unseen
        [("underway", 10), ("at_berth", 60), ("gap", stale + 15), ("at_berth", 60), ("underway", 5)],
        [("underway", 10), ("at_berth", 60), ("gap", stale - 10), ("at_berth", 60), ("underway", 5)],
        [("underway", 10), ("at_berth", 60), ("gap", stale), ("at_berth", 60), ("underway", 5)],
        [("underway", 10), ("at_berth", 60), ("anchored", 1), ("at_berth", 60), ("underway", 5)],
        [("underway", 30), ("at_berth", 1), ("underway", 30)],
        [("anchored", 10), ("at_berth", 100), ("underway", 1), ("at_berth", 100), ("anchored", 10)],
        [("underway", 10), ("at_berth", 70), ("underway", 20), ("at_berth", 80), ("underway", 10)],
        [("underway", 5), ("gap", stale + 15), ("at_berth", 120), ("underway", 10)],
    ]
    docs = []
    for i, segs in enumerate(cases):
        docs += track(100_000_000 + i, segs)
    return docs


def random_history(rng, vessels):
    stale = config.STALE_MINUTES
    docs = []
    for v in range(vessels):
        segs = []
        for _ in range(rng.randint(3, 15)):
            roll = rng.random()
            if roll < 0.12:
                segs.append(("gap", rng.choice([rng.randint(2, stale), rng.randint(stale + 1, 4 * stale)])))
            elif roll < 0.25:
                segs.append((rng.choice(STATES), 1))        # single-fix noise
            else:
                segs.append((rng.choice(STATES), rng.randint(2, 300)))
        start = T0 + timedelta(minutes=rng.randint(0, 600), seconds=rng.randint(0, 59))
        docs += track(200_000_000 + v, segs, start)
    return docs


def key(stay):
    def ms(t):
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(timezone.utc).replace(microsecond=t.microsecond // 1000 * 1000)
    return (int(stay["mmsi"]), ms(stay["start"]), ms(stay["end"]))


def compare(collection, docs, label):
    collection.delete_many({})
    if docs:
        collection.insert_many([dict(d) for d in docs])
    expected = {key(s) for s in optimize.measure_stays([dict(d) for d in docs])}
    got = {key(s) for s in collection.aggregate(optimize.stays_pipeline(config.STALE_MINUTES))}
    if expected == got:
        print(f"{label}: OK ({len(expected)} stays, {len(docs)} positions)")
        return True
    print(f"{label}: MISMATCH ({len(expected)} expected, {len(got)} from pipeline)")
    for s in sorted(expected - got)[:10]:
        print("  only in measure_stays:", s)
    for s in sorted(got - expected)[:10]:
        print("  only in pipeline:     ", s)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--vessels", type=int, default=20)
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
    collection = db[f"check_stays_{uuid.uuid4().hex[:12]}"]
    ok = True
    try:
        ok &= compare(collection, handcrafted(), "handcrafted")
        for r in range(args.rounds):
            ok &= compare(collection, random_history(rng, args.vessels), f"random round {r}")
    finally:
        collection.drop()
        client.close()
    print("ALL MATCH" if ok else f"MISMATCHES FOUND (rerun with --seed {seed})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
