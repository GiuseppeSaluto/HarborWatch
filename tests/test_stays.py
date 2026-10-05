# Written by a test-writing agent from the measure_stays/service_minutes contract alone,
# without seeing optimize.py.
from datetime import datetime, timedelta, timezone

import pytest

from optimize import measure_stays, service_minutes

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
MAX_GAP = 30
U, B, A = "underway", "at_berth", "anchored"


def t(minutes):
    return T0 + timedelta(minutes=minutes)


def track(mmsi, *points):
    # points are (minute, state); extra keys must be ignored
    return [{"mmsi": mmsi, "ts": t(m), "state": s, "sog": 0.0} for m, s in points]


@pytest.mark.parametrize("points, expected", [
    # clean stay: end is the first position after the run, not the last at_berth one
    ([(0, U), (10, B), (40, B), (50, A)], [(1, t(10), t(50))]),
    # history begins at berth: no position before the run
    ([(0, B), (10, B), (20, U)], []),
    # still at berth: no position after the run
    ([(0, U), (10, B), (20, B)], []),
    # gaps of exactly max_gap before, inside and after are allowed (off-by-one on >=)
    ([(0, U), (30, B), (60, B), (90, U)], [(1, t(30), t(90))]),
    # gap before the run too big
    ([(0, U), (31, B), (40, B), (50, U)], []),
    # gap after the run too big
    ([(0, U), (10, B), (20, B), (51, U)], []),
    # gap inside the run too big: the whole run is dropped, not split or kept
    ([(0, U), (10, B), (41, B), (50, U)], []),
])
def test_stay_completeness(points, expected):
    # catches missing/wrong completeness checks and wrong start/end timestamps
    assert measure_stays(track(1, *points), MAX_GAP) == expected


def test_underway_blip_inside_stay_does_not_split_it():
    # catches missing noise smoothing: one bad "underway" fix would split one stay in two
    points = [(0, U), (10, B), (20, B), (30, U), (40, B), (50, B), (60, U)]
    assert measure_stays(track(1, *points), MAX_GAP) == [(1, t(10), t(60))]


def test_at_berth_blip_between_underway_is_not_a_stay():
    # catches missing noise smoothing the other way: a lone at_berth fix becoming a stay
    points = [(0, U), (10, U), (20, B), (30, U), (40, U)]
    assert measure_stays(track(1, *points), MAX_GAP) == []


def test_first_and_last_positions_are_not_smoothed():
    # catches smoothing an edge position using only its single neighbour
    points = [(0, U), (10, B), (20, B), (30, U)]
    assert measure_stays(track(1, *points), MAX_GAP) == [(1, t(10), t(30))]


def test_vessels_do_not_mix_and_order_is_mmsi_then_start():
    # catches using another vessel's position as neighbour, and wrong output order
    positions = (
        track(1, (0, U), (10, B), (20, B))  # still at berth: vessel 2's first fix must not close it
        + track(2, (15, U), (25, B), (30, B), (35, U), (45, U), (55, B), (60, B), (65, A))
        + track(3, (0, B), (5, U))  # starts at berth: vessel 2's last fix must not open it
    )
    assert measure_stays(positions, MAX_GAP) == [(2, t(25), t(35)), (2, t(55), t(65))]


def stays(mmsi, *lengths):
    return [(mmsi, t(i * 1000), t(i * 1000 + n)) for i, n in enumerate(lengths)]


@pytest.mark.parametrize("lengths, expected", [
    ((61, 62), 61),             # even count: median 61.5 rounded down
    ((100, 10, 30, 20), 25),    # even count, unsorted: median, not mean (40) or middle element
    ((30, 10, 20), 20),         # odd count, unsorted: middle of the sorted values
])
def test_service_minutes_median(lengths, expected):
    # catches mean instead of median, no sorting, rounding up instead of down
    minutes, counts = service_minutes(stays(1, *lengths), {1: "cargo"}, {"cargo": 999}, 2)
    assert minutes == {"cargo": expected}
    assert counts == {"cargo": len(lengths)}


def test_service_minutes_threshold_unknowns_and_counts():
    # catches off-by-one on min_stays, unknown vessels/categories leaking in, missing keys
    data = (
        stays(1, 40, 50)        # cargo, vessel 1
        + stays(2, 60)          # cargo, vessel 2 -> 3 cargo stays == min_stays: measured
        + stays(3, 5, 5)        # tanker: 2 stays < min_stays -> default
        + stays(4, 1, 1, 1)     # vessel without a category: ignored
        + stays(5, 1, 1, 1)     # category not in defaults: ignored
    )
    category_of = {1: "cargo", 2: "cargo", 3: "tanker", 5: "fishing"}
    defaults = {"cargo": 120, "tanker": 240, "passenger": 90}
    minutes, counts = service_minutes(data, category_of, defaults, 3)
    assert minutes == {"cargo": 50, "tanker": 240, "passenger": 90}
    assert counts == {"cargo": 3, "tanker": 2, "passenger": 0}


# The tests below are not from the test agent: they cover the collection-window rule added later.
def five_short_cargo_stays():
    return [(1, t(i * 100), t(i * 100 + 60)) for i in range(5)], {1: "cargo"}  # five 1-hour stays


def test_short_window_keeps_the_default():
    # 5 one-hour stays seen in a 7 h window: a 24 h default must not drop to 1 h.
    stays, category_of = five_short_cargo_stays()
    minutes, counts = service_minutes(stays, category_of, {"cargo": 1440}, 5, window_minutes=7 * 60)
    assert minutes == {"cargo": 1440} and counts == {"cargo": 5}


@pytest.mark.parametrize("window, expected", [
    (2 * 1440 - 1, 1440),   # just short of twice the default: still the default
    (2 * 1440, 60),         # exactly twice: the measured median is trusted
    (None, 60),             # no window given: only the stay count matters
])
def test_window_threshold(window, expected):
    stays, category_of = five_short_cargo_stays()
    assert service_minutes(stays, category_of, {"cargo": 1440}, 5, window_minutes=window)[0] == {"cargo": expected}
