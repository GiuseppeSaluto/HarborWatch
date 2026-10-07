"""Black-box tests for history.py: hourly congestion series and continuous
collection windows (spec section 8 point 3, section 9 "Durate delle soste").

Pure functions; no MongoDB needed.
"""

import math
from datetime import datetime, timedelta, timezone

import pytest

import history

H0 = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)


def hour(n):
    return H0 + timedelta(hours=n)


def is_gap(value):
    return value is None or (isinstance(value, float) and math.isnan(value))


# --- hourly_series(covered, counts) ------------------------------------------------------
# Assumption: `covered` is an iterable of hour-truncated aware datetimes in which
# the ingestion stored at least one position; `counts` is a dict hour -> number of
# commercial anchored vessels in that hour (hours with no such vessel may be
# missing). The result covers every hour from the first to the last covered
# hour, in order, as a list of (hour, value) pairs or a dict hour -> value
# (both accepted by `as_map`); an uncovered hour is a gap (None or NaN), never 0.


def as_map(result):
    if isinstance(result, dict):
        return dict(result)
    if hasattr(result, "to_dict") and not hasattr(result, "columns"):   # pandas Series
        return {k.to_pydatetime() if hasattr(k, "to_pydatetime") else k: v
                for k, v in result.to_dict().items()}
    out = {}
    for item in result:
        if isinstance(item, dict):
            keys = list(item)
            out[item[keys[0]]] = item[keys[1]]
        else:
            out[item[0]] = item[1]
    return out


def series(covered, counts):
    return as_map(history.hourly_series(covered, counts))


def test_covered_hour_keeps_its_count():
    s = series([hour(0), hour(1)], {hour(0): 4, hour(1): 7})
    assert s[hour(0)] == 4
    assert s[hour(1)] == 7


def test_covered_hour_without_vessels_is_zero_not_gap():
    # Data was collected and the anchorage was empty: that is a real zero.
    s = series([hour(0), hour(1), hour(2)], {hour(0): 3, hour(2): 5})
    assert s[hour(1)] == 0
    assert not is_gap(s[hour(1)])


def test_uncovered_hour_is_a_gap_not_zero():
    # Ingestion off at hour 1: the chart must show a hole, not an empty anchorage.
    s = series([hour(0), hour(2)], {hour(0): 3, hour(2): 5})
    assert hour(1) in s
    assert is_gap(s[hour(1)])


def test_long_outage_is_all_gaps():
    covered = [hour(0), hour(5)]
    s = series(covered, {hour(0): 1, hour(5): 2})
    for n in range(1, 5):
        assert is_gap(s[hour(n)]), n


def test_series_is_hourly_and_contiguous_between_first_and_last_covered_hour():
    covered = [hour(3), hour(0), hour(6)]          # unordered on purpose
    s = series(covered, {hour(0): 1, hour(3): 1, hour(6): 1})
    keys = sorted(s)
    assert keys[0] == hour(0)
    assert keys[-1] == hour(6)
    assert keys == [hour(n) for n in range(7)]


def test_series_is_returned_in_time_order():
    result = history.hourly_series([hour(2), hour(0), hour(1)], {hour(1): 1})
    # as_map keeps the result's order; a type check on hasattr(result, "index") misfires,
    # because plain lists have an index() method too.
    keys = list(as_map(result))
    assert keys == sorted(keys)


def test_no_coverage_gives_empty_series():
    assert len(series([], {})) == 0


def test_zero_count_in_counts_is_kept_as_zero():
    s = series([hour(0), hour(1)], {hour(0): 0, hour(1): 2})
    assert s[hour(0)] == 0 and not is_gap(s[hour(0)])


# --- continuous_hours(values) and longest_run(values) ------------------------------------
# Assumption: `values` is the ordered list of hourly values of the series above
# (None or NaN = gap, any number including 0 = covered hour).
# continuous_hours returns the number of covered hours in the run ending at the
# last value (the current continuous collection window); longest_run returns
# the length of the longest run of covered hours anywhere in the list. Both
# count hours.

NAN = float("nan")


def test_longest_run_empty():
    assert history.longest_run([]) == 0


def test_longest_run_all_gaps():
    assert history.longest_run([None, None, NAN]) == 0


def test_longest_run_no_gaps():
    assert history.longest_run([1, 2, 3, 4]) == 4


def test_longest_run_picks_the_longest_not_the_last():
    assert history.longest_run([1, 1, 1, 1, None, 2, 2]) == 4


def test_longest_run_picks_the_longest_in_the_middle():
    assert history.longest_run([1, None, 1, 1, 1, None, 1, 1]) == 3


def test_longest_run_counts_zero_as_covered():
    # An empty anchorage is still collected data.
    assert history.longest_run([0, 0, 0, None, 5]) == 3


def test_longest_run_treats_nan_as_gap():
    assert history.longest_run([1, 1, NAN, 1, 1, 1]) == 3


def test_continuous_hours_empty():
    assert history.continuous_hours([]) == 0


def test_continuous_hours_counts_trailing_run():
    assert history.continuous_hours([1, 1, 1, 1, None, 0, 2]) == 2


def test_continuous_hours_zero_after_final_gap():
    assert history.continuous_hours([1, 1, 1, None]) == 0


def test_continuous_hours_all_covered():
    assert history.continuous_hours([0, 3, 0, 1, 2]) == 5


def test_continuous_hours_treats_nan_as_gap():
    assert history.continuous_hours([1, 1, NAN, 1]) == 1


def test_continuous_hours_never_exceeds_longest_run():
    values = [1, 1, 1, None, 1, 1, None, None, 1, 0, 1, 1, 1, 1]
    assert history.continuous_hours(values) <= history.longest_run(values)
    assert history.longest_run(values) == 6


def test_series_values_feed_longest_run():
    # End-to-end: 3 covered hours, 2-hour outage, 4 covered hours.
    covered = [hour(n) for n in (0, 1, 2, 5, 6, 7, 8)]
    s = series(covered, {hour(0): 1})
    values = [s[k] for k in sorted(s)]
    assert history.longest_run(values) == 4
    assert history.continuous_hours(values) == 4


@pytest.mark.parametrize("hours,enough", [(47, False), (48, True)])
def test_cargo_window_threshold_in_hours(hours, enough):
    # Spec: measured stays need a continuous window of at least
    # STAY_WINDOW_FACTOR x default; for cargo 2 x 24 h = 48 h.
    import config
    needed_hours = config.STAY_WINDOW_FACTOR * config.SERVICE_MINUTES["cargo"] / 60
    run = history.longest_run([1] * hours + [None] + [1] * 10)
    assert (run >= needed_hours) is enough
