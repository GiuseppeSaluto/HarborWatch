"""Pure functions on the position history shown by the dashboard."""

import itertools
import math
from datetime import timedelta

import config
from classify import M_PER_DEG_LAT, M_PER_DEG_LON_AT_EQUATOR


def hourly_series(covered, counts):
    """One row per hour, from the first to the last hour with any data.

    covered: hours (UTC datetimes truncated to the hour) in which the ingestion stored anything.
    counts: hour -> commercial vessels seen at anchor in it.
    Rows are {hour, anchored, alone}: anchored is None for hours without data (the chart
    breaks the line there instead of diving to zero), and alone marks an hour with data and
    no neighbour with data, which a line alone would not draw (e.g. the first after a restart).
    """
    if not covered:
        return []
    hours, hour = [], min(covered)
    while hour <= max(covered):
        hours.append(hour)
        hour += timedelta(hours=1)
    values = [counts.get(h, 0) if h in covered else None for h in hours]
    has = [v is not None for v in values]
    return [{"hour": h, "anchored": v,
             "alone": has[i] and not (i > 0 and has[i - 1]) and not (i + 1 < len(has) and has[i + 1])}
            for i, (h, v) in enumerate(zip(hours, values))]


def _missing(value):
    return value is None or (isinstance(value, float) and math.isnan(value))


def continuous_hours(values):
    """Hours with data in a row at the end of a series (None or NaN marks an hour without)."""
    hours = 0
    for value in reversed(values):
        if _missing(value):
            break
        hours += 1
    return hours


def longest_run(values):
    """The longest run of hours with data anywhere in a series (None or NaN marks an hour without)."""
    best = run = 0
    for value in values:
        run = 0 if _missing(value) else run + 1
        best = max(best, run)
    return best


def _metres(a, b):
    """Distance in metres between two [lon, lat] points, on the same local plane as coast_distance_m."""
    kx = M_PER_DEG_LON_AT_EQUATOR * math.cos(math.radians(a[1]))
    return math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * M_PER_DEG_LAT)


def vessel_tracks(positions, now):
    """The tracks to draw on the map: [{mmsi, paths}], one entry per vessel that moved.

    positions: position documents (mmsi, ts, location GeoJSON Point), in any order. Only the
    last TRACK_HOURS up to now count. A vessel is drawn if its farthest position from its
    first one in the window is at least TRACK_MIN_MOVE_M away; its paths are split wherever
    two consecutive positions are more than STALE_MINUTES apart, so a gap in the data is not
    drawn as a straight line nobody sailed, and paths of a single position are dropped.
    """
    start = now - timedelta(hours=config.TRACK_HOURS)
    gap = timedelta(minutes=config.STALE_MINUTES)
    recent = sorted((p for p in positions if start <= p["ts"] <= now), key=lambda p: (p["mmsi"], p["ts"]))
    tracks = []
    for mmsi, group in itertools.groupby(recent, key=lambda p: p["mmsi"]):
        fixes = list(group)
        first = fixes[0]["location"]["coordinates"]
        # The farthest position counts even if it ends up in a dropped single-position path.
        if max(_metres(first, f["location"]["coordinates"]) for f in fixes) < config.TRACK_MIN_MOVE_M:
            continue
        paths, current = [], [fixes[0]]
        for previous, fix in zip(fixes, fixes[1:]):
            if fix["ts"] - previous["ts"] > gap:
                paths.append(current)
                current = []
            current.append(fix)
        paths.append(current)
        paths = [[f["location"]["coordinates"] for f in path] for path in paths if len(path) > 1]
        if paths:
            tracks.append({"mmsi": mmsi, "paths": paths})
    return tracks


def stays_pipeline(max_gap_minutes):
    """measure_stays as a MongoDB aggregation on positions: only the stays leave the server.

    Returns documents {mmsi, start, end}. measure_stays stays the tested definition; this
    pipeline must give the same stays (checked against it on real and random histories).
    """
    # explain(): the first window reads the (mmsi, ts) index already in order; MongoDB re-sorts
    # in memory before each later window.
    # That sort grows with the history and is capped at 32 MB on Atlas M0: the $project below
    # keeps it small, and the dashboard runs the pipeline per group of vessels to bound it.
    gap_ms = max_gap_minutes * 60_000  # date minus date is in milliseconds
    by_vessel = {"partitionBy": "$mmsi", "sortBy": {"ts": 1}}
    shift = lambda field, by: {"$shift": {"output": field, "by": by}}
    return [
        {"$match": {"state": {"$exists": True}}},
        # 1. Each position sees its neighbours: same vessel, in time order.
        {"$setWindowFields": {**by_vessel, "output": {
            "prev_state": shift("$state", -1), "next_state": shift("$state", 1), "prev_ts": shift("$ts", -1)}}},
        # 2. Noise: a one-position blip takes its neighbours' state when they agree; gap since the previous one.
        {"$set": {
            "state": {"$cond": [{"$and": [{"$ne": ["$prev_state", None]}, {"$eq": ["$prev_state", "$next_state"]}]},
                                "$prev_state", "$state"]},
            "gap": {"$subtract": ["$ts", "$prev_ts"]}}},
        # The next windows re-sort in memory, and Atlas M0 caps that sort at 32 MB and ignores
        # allowDiskUse: whole documents (location, sog...) broke it at 45k positions (2026-10-09).
        {"$project": {"_id": 0, "mmsi": 1, "ts": 1, "state": 1, "gap": 1}},
        # 3. Number the runs of equal state: a running sum of "state changed here".
        {"$setWindowFields": {**by_vessel, "output": {"prev_smoothed": shift("$state", -1)}}},
        {"$set": {"new_run": {"$cond": [{"$eq": ["$state", "$prev_smoothed"]}, 0, 1]}}},
        {"$setWindowFields": {**by_vessel, "output": {
            "run": {"$sum": "$new_run", "window": {"documents": ["unbounded", "current"]}}}}},
        # 4. One document per run; the gap before it is the one of its first position.
        {"$group": {
            "_id": {"mmsi": "$mmsi", "run": "$run"}, "state": {"$first": "$state"},
            "start": {"$min": "$ts"}, "last": {"$max": "$ts"},
            "gap_before": {"$max": {"$cond": ["$new_run", "$gap", None]}},
            "gap_inside": {"$max": {"$cond": ["$new_run", None, "$gap"]}}}},
        # 5. Each run sees the next run of the same vessel: when it starts and the gap to it.
        {"$setWindowFields": {"partitionBy": "$_id.mmsi", "sortBy": {"_id.run": 1}, "output": {
            "next_start": shift("$start", 1), "gap_after": shift("$gap_before", 1)}}},
        # 6. Complete berth stays: arrival and departure seen, no gap over max_gap_minutes.
        {"$match": {
            "state": "at_berth",
            "gap_before": {"$ne": None, "$lte": gap_ms}, "gap_after": {"$ne": None, "$lte": gap_ms},
            "$or": [{"gap_inside": None}, {"gap_inside": {"$lte": gap_ms}}]}},
        {"$project": {"_id": 0, "mmsi": "$_id.mmsi", "start": 1, "end": "$next_start"}},
        {"$sort": {"mmsi": 1, "start": 1}},
    ]


def vessel_groups(counts, cap):
    """Vessels in groups of at most cap positions, in the given order, for stays_pipeline.

    counts: {"_id": mmsi, "n": positions} per vessel. A vessel is never split, so one with
    more than cap positions forms a group of its own.
    """
    groups, group, size = [], [], 0
    for c in counts:
        if group and size + c["n"] > cap:
            groups.append(group)
            group, size = [], 0
        group.append(c["_id"])
        size += c["n"]
    if group:
        groups.append(group)
    return groups


def archive_since(now):
    """Start of the first whole hour inside the TTL window: older hours are partly deleted."""
    start = now - timedelta(days=config.POSITIONS_TTL_DAYS)
    hour = start.replace(minute=0, second=0, microsecond=0)
    return hour if hour == start else hour + timedelta(hours=1)


STATES = ("anchored", "at_berth", "underway")


def congestion_pipeline(since, commercial):
    """Distinct commercial vessels per state and UTC hour, one document per hour with data.

    Hours with only non-commercial vessels give zeros: the hour was covered, not empty.
    """
    return [
        {"$match": {"ts": {"$gte": since}, "state": {"$ne": None}}},
        # One document per vessel, state and hour: a vessel seen in two states counts in both.
        {"$group": {"_id": {"hour": {"$dateTrunc": {"date": "$ts", "unit": "hour"}},
                            "mmsi": "$mmsi", "state": "$state"}}},
        {"$group": {"_id": "$_id.hour", **{
            s: {"$sum": {"$cond": [{"$and": [{"$eq": ["$_id.state", s]}, {"$in": ["$_id.mmsi", commercial]}]}, 1, 0]}}
            for s in STATES}}},
    ]
