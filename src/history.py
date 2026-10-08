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
