"""Pure functions on the position history shown by the dashboard."""

import math
from datetime import timedelta


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
