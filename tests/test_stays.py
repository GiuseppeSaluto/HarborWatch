"""Black-box tests for stay measurement: measure_stays and service_minutes
(spec section 9, "Durate delle soste", and section 10).

Pure functions on in-memory data; no MongoDB needed.
"""

import statistics
from datetime import datetime, timedelta, timezone

import pytest

import config
import optimize

try:
    import history
except ImportError:  # pragma: no cover
    history = None


def _resolve(name):
    # Assumption: the spec places measure_stays and service_minutes in optimize.py;
    # the project also has a history.py module, so the function is looked up in
    # optimize first and then in history.
    for module in (optimize, history):
        fn = getattr(module, name, None) if module else None
        if callable(fn):
            return fn
    raise AttributeError(f"{name} not found in optimize or history")


T0 = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=1)


# Assumption: measure_stays(positions) takes an iterable of position dicts
# {"mmsi", "ts" (aware datetime), "state"} (the stored `positions` fields of
# spec section 6) and returns a list of complete berth stays, each a dict with
# "mmsi", "start" and "end" (datetimes). A stay is an `at_berth` run.

def measure(positions):
    return _resolve("measure_stays")(positions)


def track(mmsi, segments, start=T0, step=STEP):
    """segments: list of (state, count) or ("gap", minutes). One position per step."""
    out, t = [], start
    for kind, n in segments:
        if kind == "gap":
            t += timedelta(minutes=n)
            continue
        for _ in range(n):
            out.append({"mmsi": mmsi, "ts": t, "state": kind})
            t += step
    return out


def minutes(stay):
    return (stay["end"] - stay["start"]).total_seconds() / 60


def stays_of(result, mmsi):
    return [s for s in result if s["mmsi"] == mmsi]


# --- measure_stays ---------------------------------------------------------------------

def test_complete_stay_is_found():
    pos = track(1, [("anchored", 10), ("at_berth", 120), ("underway", 10)])
    result = measure(pos)
    assert len(result) == 1
    assert result[0]["mmsi"] == 1
    # Arrival/departure resolution is one sample: allow +-2 minutes.
    assert minutes(result[0]) == pytest.approx(120, abs=2)


def test_stay_starts_at_first_berth_position():
    pos = track(1, [("underway", 10), ("at_berth", 90), ("underway", 5)])
    stay = measure(pos)[0]
    assert abs((stay["start"] - (T0 + 10 * STEP)).total_seconds()) <= 120


def test_arrival_not_seen_is_not_a_complete_stay():
    pos = track(1, [("at_berth", 120), ("underway", 10)])
    assert measure(pos) == []


def test_departure_not_seen_is_not_a_complete_stay():
    pos = track(1, [("underway", 10), ("at_berth", 120)])
    assert measure(pos) == []


def test_never_berthed_has_no_stay():
    pos = track(1, [("underway", 30), ("anchored", 200), ("underway", 30)])
    assert measure(pos) == []


def test_gap_longer_than_stale_minutes_breaks_the_stay():
    gap = config.STALE_MINUTES + 15
    pos = track(1, [("underway", 10), ("at_berth", 60), ("gap", gap),
                    ("at_berth", 60), ("underway", 10)])
    assert measure(pos) == []


def test_gap_before_arrival_makes_arrival_unseen():
    gap = config.STALE_MINUTES + 15
    pos = track(1, [("underway", 10), ("gap", gap), ("at_berth", 120), ("underway", 10)])
    assert measure(pos) == []


def test_short_gap_within_stale_minutes_keeps_the_stay():
    gap = config.STALE_MINUTES - 10
    pos = track(1, [("underway", 10), ("at_berth", 60), ("gap", gap),
                    ("at_berth", 60), ("underway", 10)])
    result = measure(pos)
    assert len(result) == 1
    assert minutes(result[0]) == pytest.approx(120 + gap, abs=2)


def test_single_position_state_flip_is_ignored():
    # One stray anchored fix in the middle of a berth stay must not split it.
    pos = track(1, [("underway", 10), ("at_berth", 60), ("anchored", 1),
                    ("at_berth", 60), ("underway", 10)])
    result = measure(pos)
    assert len(result) == 1
    assert minutes(result[0]) == pytest.approx(121, abs=2)


def test_single_position_berth_blip_is_not_a_stay():
    pos = track(1, [("underway", 30), ("at_berth", 1), ("underway", 30)])
    assert measure(pos) == []


def test_single_underway_blip_does_not_end_stay():
    pos = track(1, [("anchored", 10), ("at_berth", 100), ("underway", 1),
                    ("at_berth", 100), ("anchored", 10)])
    result = measure(pos)
    assert len(result) == 1
    assert minutes(result[0]) == pytest.approx(201, abs=2)


def test_two_consecutive_stays_of_same_vessel():
    pos = track(1, [("underway", 10), ("at_berth", 70), ("underway", 20),
                    ("at_berth", 80), ("underway", 10)])
    result = sorted(measure(pos), key=lambda s: s["start"])
    assert len(result) == 2
    assert minutes(result[0]) == pytest.approx(70, abs=2)
    assert minutes(result[1]) == pytest.approx(80, abs=2)


def test_vessels_are_measured_independently():
    a = track(1, [("underway", 10), ("at_berth", 90), ("underway", 10)])
    b = track(2, [("anchored", 30), ("at_berth", 150), ("underway", 10)])
    # Interleave and shuffle order by timestamp only, mmsi mixed.
    pos = sorted(a + b, key=lambda p: (p["ts"], p["mmsi"]))
    result = measure(pos)
    assert len(stays_of(result, 1)) == 1
    assert len(stays_of(result, 2)) == 1
    assert minutes(stays_of(result, 1)[0]) == pytest.approx(90, abs=2)
    assert minutes(stays_of(result, 2)[0]) == pytest.approx(150, abs=2)


def test_one_vessel_gap_does_not_affect_another():
    a = track(1, [("underway", 10), ("at_berth", 60), ("gap", config.STALE_MINUTES + 20),
                  ("at_berth", 60), ("underway", 10)])
    b = track(2, [("underway", 10), ("at_berth", 180), ("underway", 10)])
    result = measure(sorted(a + b, key=lambda p: p["ts"]))
    assert stays_of(result, 1) == []
    assert len(stays_of(result, 2)) == 1


# --- service_minutes -------------------------------------------------------------------
# Assumption: service_minutes(stays, window_minutes) takes a list of stay dicts
# {"category", "length" (metres), "minutes"} plus the length of the continuous
# collection window in minutes, and returns a dict category -> minutes covering
# at least cargo, tanker and passenger. The length (>= 50 m) and duration
# (>= 60 min) filters are assumed to be applied here (the spec states them next
# to service_minutes); if they live in measure_stays instead, the filter tests
# below will fail for that reason.

DEFAULTS = config.SERVICE_MINUTES
BIG_WINDOW = 10 * max(DEFAULTS.values())


def service(stays, window=BIG_WINDOW):
    return _resolve("service_minutes")(stays, window)


def stays(category, durations, length=150):
    return [{"category": category, "length": length, "minutes": d} for d in durations]


def test_no_stays_uses_defaults():
    result = service([])
    for cat in ("cargo", "tanker", "passenger"):
        assert result[cat] == DEFAULTS[cat]


def test_enough_stays_uses_median():
    durs = [300, 600, 900, 1200, 2000]
    assert service(stays("cargo", durs))["cargo"] == statistics.median(durs)


def test_median_not_mean():
    durs = [100, 110, 120, 130, 5000]
    assert service(stays("cargo", durs))["cargo"] == 120


def test_exactly_min_stays_is_enough():
    durs = [200 + 10 * i for i in range(config.MIN_STAYS)]
    assert service(stays("tanker", durs))["tanker"] == statistics.median(durs)


def test_fewer_than_min_stays_uses_default():
    durs = [200 + 10 * i for i in range(config.MIN_STAYS - 1)]
    assert service(stays("tanker", durs))["tanker"] == DEFAULTS["tanker"]


def test_categories_are_independent():
    data = stays("passenger", [100, 200, 300, 400, 500]) + stays("cargo", [600, 700])
    result = service(data)
    assert result["passenger"] == 300
    assert result["cargo"] == DEFAULTS["cargo"]
    assert result["tanker"] == DEFAULTS["tanker"]


def test_other_category_stays_do_not_count():
    data = stays("cargo", [100, 200]) + stays("passenger", [300, 300, 300])
    assert service(data)["cargo"] == DEFAULTS["cargo"]


def test_small_vessels_are_excluded():
    # Harbour boats under 50 m dragged the passenger median to 8 minutes.
    data = stays("passenger", [480, 500, 520], length=150) + \
        stays("passenger", [70, 70, 70, 70], length=30)
    assert service(data)["passenger"] == DEFAULTS["passenger"]


def test_vessel_of_exactly_50_m_counts():
    data = stays("passenger", [100, 200, 300, 400, 500], length=50)
    assert service(data)["passenger"] == 300


def test_vessel_just_under_50_m_does_not_count():
    data = stays("passenger", [100, 200, 300, 400, 500], length=49)
    assert service(data)["passenger"] == DEFAULTS["passenger"]


def test_stays_shorter_than_one_hour_are_excluded():
    data = stays("cargo", [600, 700, 800]) + stays("cargo", [8, 10, 30, 59])
    assert service(data)["cargo"] == DEFAULTS["cargo"]


def test_stay_of_exactly_one_hour_counts():
    durs = [60, 60, 60, 300, 400]
    assert service(stays("cargo", durs))["cargo"] == 60


def test_short_stays_do_not_shift_median():
    long_ = [500, 600, 700, 800, 900]
    data = stays("cargo", long_) + stays("cargo", [5, 5, 5, 5, 5, 5, 5])
    assert service(data)["cargo"] == 700


def test_short_window_uses_default_even_with_enough_stays():
    # Only short stays are complete in a few hours: a cargo median of 1 h must not
    # replace the 24 h default.
    window = config.STAY_WINDOW_FACTOR * DEFAULTS["cargo"] - 1
    data = stays("cargo", [60, 65, 70, 75, 80])
    assert service(data, window)["cargo"] == DEFAULTS["cargo"]


def test_window_exactly_factor_times_default_is_enough():
    window = config.STAY_WINDOW_FACTOR * DEFAULTS["cargo"]
    data = stays("cargo", [60, 65, 70, 75, 80])
    assert service(data, window)["cargo"] == 70


def test_window_is_checked_per_category_default():
    # Long enough for passenger (2 x 12 h) but not for cargo (2 x 24 h).
    window = config.STAY_WINDOW_FACTOR * DEFAULTS["passenger"]
    data = stays("passenger", [100, 200, 300, 400, 500]) + \
        stays("cargo", [100, 200, 300, 400, 500])
    result = service(data, window)
    assert result["passenger"] == 300
    assert result["cargo"] == DEFAULTS["cargo"]
