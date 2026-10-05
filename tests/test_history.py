from datetime import UTC, datetime, timedelta

import pytest

from history import continuous_hours, hourly_series, longest_run

T0 = datetime(2026, 10, 5, 8, tzinfo=UTC)
H = lambda n: T0 + timedelta(hours=n)


def test_gaps_stay_empty_and_lone_hours_are_flagged():
    # Data at 0, 1, 2 and 5: hours 3-4 are gaps (None, not 0), hour 5 has no neighbour.
    covered = {H(0), H(1), H(2), H(5)}
    rows = hourly_series(covered, {H(0): 6, H(2): 8, H(5): 7})
    assert [r["hour"] for r in rows] == [H(n) for n in range(6)]
    assert [r["anchored"] for r in rows] == [6, 0, 8, None, None, 7]  # covered but nobody at anchor = 0
    assert [r["alone"] for r in rows] == [False, False, False, False, False, True]


@pytest.mark.parametrize("covered, alone", [
    ({H(0)}, [True]),                              # a single hour of data
    ({H(0), H(2)}, [True, False, True]),           # two lone hours around a gap
    ({H(0), H(1)}, [False, False]),                # a pair draws a line
    ({H(0), H(4)}, [True, False, False, False, True]),  # an empty hour amid empty ones is no dot
])
def test_alone_at_the_edges(covered, alone):
    assert [r["alone"] for r in hourly_series(covered, {})] == alone


def test_no_data_no_rows():
    assert hourly_series(set(), {}) == []


@pytest.mark.parametrize("values, hours", [
    ([1, None, 2, 3], 2),
    ([1, 2, None], 0),                             # stopped: the last hour has no data
    ([1, float("nan"), 4], 1),                     # NaN, as pandas gives for a missing value
    ([5, 0, 3], 3),                                # 0 vessels at anchor is still data
    ([], 0),
])
def test_continuous_hours(values, hours):
    assert continuous_hours(values) == hours


@pytest.mark.parametrize("values, hours", [
    ([1, 2, None, 3, 4, 5, None, 6], 3),           # the longest run, not the last one
    ([None, None], 0),
    ([1, float("nan"), 2, 3], 2),
    ([0, 0, 0], 3),                                # 0 vessels at anchor is still data
    ([], 0),
])
def test_longest_run(values, hours):
    assert longest_run(values) == hours
