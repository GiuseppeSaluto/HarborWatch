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


# --- vessel_tracks(positions, now) ---------------------------------------------------------
# Spec section 8 point 5 and section 13. config.TRACK_HOURS and
# config.TRACK_MIN_MOVE_M are read inside the tests only, so the rest of this
# file still imports while they do not exist yet.
# Assumption: config.TRACK_HOURS is a number of hours (spec: "TRACK_HOURS (3)
# ore") and config.TRACK_MIN_MOVE_M a number of metres.
# Assumption: each point in `paths` is a [lon, lat] list or tuple holding the
# input coordinates unchanged, so points are compared exactly after tuple().
# Window edges (ts exactly now - TRACK_HOURS, or exactly now) are left
# untested because the spec does not say whether they are inclusive.

import config  # noqa: E402

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
LON0, LAT0 = 8.90, 44.40
M_PER_DEG_LAT = 111_195.0
M_PER_DEG_LON = M_PER_DEG_LAT * math.cos(math.radians(LAT0))


def at(east_m=0.0, north_m=0.0):
    """[lon, lat] of a point east_m / north_m metres from the reference point."""
    return [LON0 + east_m / M_PER_DEG_LON, LAT0 + north_m / M_PER_DEG_LAT]


def fix(mmsi, minutes_ago, point):
    return {"mmsi": mmsi, "ts": NOW - timedelta(minutes=minutes_ago),
            "location": {"type": "Point", "coordinates": list(point)}}


def window_minutes():
    return config.TRACK_HOURS * 60


def move_m():
    return config.TRACK_MIN_MOVE_M


def straight(mmsi, start_ago, count, step_min, step_east_m):
    """count fixes, step_min minutes apart, moving step_east_m east each time."""
    return [fix(mmsi, start_ago - i * step_min, at(east_m=i * step_east_m))
            for i in range(count)]


def tracks(positions, now=NOW):
    return {t["mmsi"]: [[tuple(p) for p in path] for path in t["paths"]]
            for t in history.vessel_tracks(positions, now)}


def pts(*points):
    return [tuple(p) for p in points]


def test_tracks_empty_input():
    assert list(history.vessel_tracks([], NOW)) == []


def test_tracks_moving_vessel_is_drawn_as_one_path():
    # 10 fixes 5 minutes apart, 200 m apart: total 1800 m, well over the threshold.
    pos = straight(1, start_ago=60, count=10, step_min=5, step_east_m=move_m() * 0.4)
    result = tracks(pos)
    assert set(result) == {1}
    assert result[1] == [pts(*(p["location"]["coordinates"] for p in pos))]


def test_tracks_one_entry_per_vessel():
    pos = straight(1, 60, 10, 5, move_m() * 0.4)
    entries = [t for t in history.vessel_tracks(pos, NOW) if t["mmsi"] == 1]
    assert len(entries) == 1


def test_tracks_stationary_vessel_is_not_drawn():
    # GPS jitter of a few tens of metres around a berth.
    jitter = [(0, 0), (20, -10), (-15, 25), (30, 5), (-25, -20), (10, 30)]
    pos = [fix(1, 60 - 5 * i, at(e, n)) for i, (e, n) in enumerate(jitter)]
    assert tracks(pos) == {}


def test_tracks_vessel_just_under_min_move_is_not_drawn():
    pos = [fix(1, 60, at(0)), fix(1, 50, at(move_m() * 0.5)), fix(1, 40, at(move_m() * 0.95))]
    assert tracks(pos) == {}


def test_tracks_vessel_just_over_min_move_is_drawn():
    pos = [fix(1, 60, at(0)), fix(1, 50, at(move_m() * 0.5)), fix(1, 40, at(move_m() * 1.05))]
    assert set(tracks(pos)) == {1}


def test_tracks_distance_counts_north_south_movement():
    pos = [fix(1, 60, at(0)), fix(1, 50, at(north_m=move_m() * 0.6)),
           fix(1, 40, at(north_m=move_m() * 1.05))]
    assert set(tracks(pos)) == {1}


def test_tracks_use_farthest_point_not_final_point():
    # Out and back: the final fix is next to the first, the middle one is far.
    pos = [fix(1, 60, at(0)), fix(1, 50, at(move_m() * 1.5)), fix(1, 40, at(20))]
    result = tracks(pos)
    assert set(result) == {1}
    assert result[1] == [pts(at(0), at(move_m() * 1.5), at(20))]


def test_tracks_distance_is_from_first_fix_in_window():
    # Two points both far from the origin but close to each other: a vessel that
    # sits 2 km from the reference point and does not move is not drawn.
    pos = [fix(1, 60, at(2000)), fix(1, 50, at(2010)), fix(1, 40, at(1995))]
    assert tracks(pos) == {}


def test_tracks_unordered_input_gives_time_ordered_path():
    ordered = straight(1, 60, 6, 5, move_m() * 0.5)
    shuffled = [ordered[i] for i in (3, 0, 5, 1, 4, 2)]
    result = tracks(shuffled)
    assert result[1] == [pts(*(p["location"]["coordinates"] for p in ordered))]


def test_tracks_ignore_fixes_older_than_window():
    old = [fix(1, window_minutes() + 30, at(-3000)), fix(1, window_minutes() + 20, at(-2500))]
    recent = straight(1, 60, 6, 5, move_m() * 0.5)
    result = tracks(old + recent)
    assert result[1] == [pts(*(p["location"]["coordinates"] for p in recent))]


def test_tracks_movement_before_window_does_not_count():
    # Sailed 3 km just before the window, then stopped for the whole window.
    old = [fix(1, window_minutes() + 20, at(-3000)), fix(1, window_minutes() + 10, at(-1500))]
    recent = [fix(1, 60, at(0)), fix(1, 50, at(10)), fix(1, 40, at(5))]
    assert tracks(old + recent) == {}


def test_tracks_window_length_follows_config():
    inside = window_minutes() - 5
    pos = [fix(1, inside, at(0)), fix(1, inside - 5, at(move_m() * 0.6)),
           fix(1, inside - 10, at(move_m() * 1.2))]
    assert set(tracks(pos)) == {1}
    outside = window_minutes() + 30
    pos_old = [fix(2, outside, at(0)), fix(2, outside - 5, at(move_m() * 0.6)),
               fix(2, outside - 10, at(move_m() * 1.2))]
    assert tracks(pos_old) == {}


def test_tracks_ignore_fixes_after_now():
    pos = [fix(1, 30, at(0)), fix(1, 25, at(10)),
           fix(1, -10, at(move_m() * 3))]        # 10 minutes in the future
    assert tracks(pos) == {}


def test_tracks_split_on_gap_longer_than_stale_minutes():
    stale = config.STALE_MINUTES
    first = [fix(1, 120, at(0)), fix(1, 115, at(300)), fix(1, 110, at(600))]
    second_start = 110 - stale - 5
    second = [fix(1, second_start, at(1500)), fix(1, second_start - 5, at(1800))]
    result = tracks(first + second)
    assert result[1] == [pts(at(0), at(300), at(600)), pts(at(1500), at(1800))]


def test_tracks_gap_of_exactly_stale_minutes_does_not_split():
    stale = config.STALE_MINUTES
    pos = [fix(1, 120, at(0)), fix(1, 115, at(400)),
           fix(1, 115 - stale, at(800)), fix(1, 110 - stale, at(1200))]
    result = tracks(pos)
    assert result[1] == [pts(at(0), at(400), at(800), at(1200))]


def test_tracks_drop_single_position_segments():
    stale = config.STALE_MINUTES
    a = [fix(1, 170, at(0)), fix(1, 165, at(300)), fix(1, 160, at(600))]
    lone = [fix(1, 160 - stale - 5, at(900))]
    b_start = 160 - 2 * stale - 10
    b = [fix(1, b_start, at(1200)), fix(1, b_start - 5, at(1500))]
    result = tracks(a + lone + b)
    assert result[1] == [pts(at(0), at(300), at(600)), pts(at(1200), at(1500))]


def test_tracks_vessel_with_only_single_position_segments_is_not_drawn():
    # Fixes 40 minutes apart (more than STALE_MINUTES) spread over 3 km: every
    # segment has one position, so nothing is left to draw.
    step = config.STALE_MINUTES + 10
    pos = [fix(1, 10 + i * step, at(i * 1000)) for i in range(4)]
    assert tracks(pos) == {}


def test_tracks_far_point_in_dropped_segment_still_counts_for_distance():
    # Spec: the farthest position from the first one in the window decides,
    # and at least one segment must remain. Here the far fix is alone (dropped),
    # the remaining segment barely moves.
    stale = config.STALE_MINUTES
    seg = [fix(1, 150, at(0)), fix(1, 145, at(20)), fix(1, 140, at(40))]
    far = [fix(1, 140 - stale - 5, at(move_m() * 3))]
    result = tracks(seg + far)
    assert result[1] == [pts(at(0), at(20), at(40))]


def test_tracks_vessels_are_independent():
    mover = straight(1, 60, 6, 5, move_m() * 0.5)
    still = [fix(2, 60 - 5 * i, at(5000 + (i % 2) * 10)) for i in range(6)]
    other_mover = [fix(3, 50 - 5 * i, at(north_m=-i * move_m() * 0.5)) for i in range(5)]
    interleaved = sorted(mover + still + other_mover, key=lambda p: p["ts"])
    result = tracks(interleaved)
    assert set(result) == {1, 3}
    assert result[1] == [pts(*(p["location"]["coordinates"] for p in mover))]
    assert result[3] == [pts(*(p["location"]["coordinates"] for p in other_mover))]


def test_tracks_gap_of_one_vessel_does_not_split_another():
    # Vessel 2's fixes fall inside vessel 1's long gap: gaps are per vessel.
    stale = config.STALE_MINUTES
    v1 = [fix(1, 150, at(0)), fix(1, 145, at(400)),
          fix(1, 145 - stale - 10, at(800)), fix(1, 140 - stale - 10, at(1200))]
    v2 = [fix(2, 145 - 5 * i, at(north_m=i * 300)) for i in range(10)]
    result = tracks(v1 + v2)
    assert len(result[1]) == 2
    assert len(result[2]) == 1
