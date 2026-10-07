"""Black-box tests for classify.py (spec section 8.2 and section 10).

Pure functions only; no external services needed.
"""

import math

import pytest

import classify
import config

UNDERWAY, AT_BERTH, ANCHORED = "underway", "at_berth", "anchored"
MOORED = 5        # AIS navigational status "moored"
UNDER_WAY = 0     # AIS navigational status "under way using engine"
AT_ANCHOR = 1     # AIS navigational status "at anchor"

FAR = 1000.0      # metres, typical anchorage distance from the coast (850-1100 m)
NEAR = 20.0       # metres, typical moored distance from the coast (2-45 m)


# Interface (spec section 13): classify.classify(sog, nav_status, coast_distance_m)
# returns "underway", "at_berth" or "anchored".
def state(sog, nav_status, distance_m):
    return classify.classify(sog, nav_status, distance_m)


# --- thresholds come from config ----------------------------------------------------

def test_thresholds_match_spec():
    assert config.SPEED_THRESHOLD_KN == 0.5
    assert config.QUAY_DISTANCE_M == 100


# --- underway ---------------------------------------------------------------------

def test_fast_vessel_far_from_coast_is_underway():
    assert state(12.0, UNDER_WAY, FAR) == UNDERWAY


def test_speed_exactly_at_threshold_is_underway():
    # ">= threshold" means the threshold itself already counts as moving.
    assert state(config.SPEED_THRESHOLD_KN, UNDER_WAY, FAR) == UNDERWAY


def test_speed_just_below_threshold_is_not_underway():
    assert state(config.SPEED_THRESHOLD_KN - 0.01, UNDER_WAY, FAR) != UNDERWAY


def test_speed_wins_over_stale_moored_status():
    # A vessel leaving the quay often still declares "moored".
    assert state(3.0, MOORED, NEAR) == UNDERWAY


def test_speed_at_threshold_wins_over_moored_status():
    assert state(config.SPEED_THRESHOLD_KN, MOORED, NEAR) == UNDERWAY


def test_moving_vessel_near_coast_is_underway():
    assert state(5.0, UNDER_WAY, 10.0) == UNDERWAY


# --- at_berth ---------------------------------------------------------------------

def test_stopped_vessel_near_coast_is_at_berth():
    assert state(0.0, UNDER_WAY, NEAR) == AT_BERTH


def test_distance_exactly_at_quay_threshold_is_at_berth():
    assert state(0.1, UNDER_WAY, float(config.QUAY_DISTANCE_M)) == AT_BERTH


def test_distance_just_over_quay_threshold_is_not_at_berth():
    assert state(0.1, UNDER_WAY, config.QUAY_DISTANCE_M + 0.5) == ANCHORED


def test_moored_status_far_from_coast_is_at_berth():
    assert state(0.0, MOORED, FAR) == AT_BERTH


def test_position_wins_over_at_anchor_status_near_coast():
    # Many berthed vessels declare a wrong status; position decides.
    assert state(0.0, AT_ANCHOR, NEAR) == AT_BERTH


def test_zero_distance_is_at_berth():
    assert state(0.0, UNDER_WAY, 0.0) == AT_BERTH


# --- anchored ---------------------------------------------------------------------

def test_stopped_vessel_far_from_coast_is_anchored():
    assert state(0.1, AT_ANCHOR, FAR) == ANCHORED


def test_stopped_vessel_far_with_under_way_status_is_anchored():
    assert state(0.0, UNDER_WAY, FAR) == ANCHORED


def test_just_below_speed_threshold_far_is_anchored():
    assert state(config.SPEED_THRESHOLD_KN - 0.01, UNDER_WAY, FAR) == ANCHORED


# --- distance from the coastline -------------------------------------------------------
# Interface (spec section 13): classify.coast_distance_m(lon, lat, lines), where
# `lines` is a GeoJSON MultiLineString coordinate list (list of lines, each a
# list of [lon, lat]); returns metres.

_dist_fn = classify.coast_distance_m

LAT0 = 44.40
SEGMENT = [[[8.90, LAT0], [8.92, LAT0]]]          # one east-west segment
M_PER_DEG_LAT = 111_195.0                          # mean-earth-radius metres per degree
M_PER_DEG_LON = M_PER_DEG_LAT * math.cos(math.radians(LAT0))


def dist(lon, lat, lines):
    return _dist_fn(lon, lat, lines)


def test_point_on_segment_has_zero_distance():
    assert dist(8.91, LAT0, SEGMENT) == pytest.approx(0.0, abs=1.0)


def test_perpendicular_distance_to_segment_interior():
    d = dist(8.91, LAT0 + 0.001, SEGMENT)       # ~111 m north of the middle
    assert d == pytest.approx(0.001 * M_PER_DEG_LAT, rel=0.02)


def test_distance_beyond_endpoint_is_distance_to_endpoint():
    # Point east of the segment end on the same latitude: the closest point is the
    # endpoint, not the infinite line (which would give ~0).
    d = dist(8.93, LAT0, SEGMENT)
    assert d == pytest.approx(0.01 * M_PER_DEG_LON, rel=0.02)


def test_distance_beyond_endpoint_diagonal():
    d = dist(8.93, LAT0 + 0.001, SEGMENT)
    expected = math.hypot(0.01 * M_PER_DEG_LON, 0.001 * M_PER_DEG_LAT)
    assert d == pytest.approx(expected, rel=0.02)


def test_distance_is_minimum_over_all_lines():
    far_line = [[8.80, 44.30], [8.81, 44.30]]
    near_line = [[8.90, LAT0], [8.92, LAT0]]
    d = dist(8.91, LAT0 + 0.0005, [far_line, near_line])
    assert d == pytest.approx(0.0005 * M_PER_DEG_LAT, rel=0.02)


def test_distance_uses_inner_vertices_of_polyline():
    # Polyline with a corner: the closest part is the second segment.
    line = [[8.90, LAT0], [8.92, LAT0], [8.92, LAT0 + 0.02]]
    d = dist(8.921, LAT0 + 0.01, [line])
    assert d == pytest.approx(0.001 * M_PER_DEG_LON, rel=0.02)


def test_moored_scale_and_anchored_scale_fall_on_either_side_of_quay_threshold():
    near = dist(8.91, LAT0 + 0.0003, SEGMENT)    # ~33 m
    far = dist(8.91, LAT0 + 0.009, SEGMENT)      # ~1000 m
    assert near <= config.QUAY_DISTANCE_M < far


# --- ship_category --------------------------------------------------------------------
# Named in the spec: classify.ship_category(ship_type) groups AIS type codes.
# AIS codes: 60-69 passenger, 70-79 cargo, 80-89 tanker, 0 = not available.


@pytest.mark.parametrize("code", [70, 71, 74, 79])
def test_cargo_codes(code):
    assert classify.ship_category(code) == "cargo"


@pytest.mark.parametrize("code", [80, 84, 89])
def test_tanker_codes(code):
    assert classify.ship_category(code) == "tanker"


@pytest.mark.parametrize("code", [60, 69])
def test_passenger_codes(code):
    assert classify.ship_category(code) == "passenger"


@pytest.mark.parametrize("code", [52, 50, 37, 36, 30, 90])
def test_port_life_is_not_commercial(code):
    # Tugs (52), pilots (50), pleasure craft / yachts (37), sailing (36),
    # fishing (30), other (90) are not congestion.
    assert classify.ship_category(code) not in {"cargo", "tanker", "passenger"}


def test_type_zero_is_unknown():
    # Spec section 12 speaks of "navi `unknown`" for AIS type 0.
    assert classify.ship_category(0) == "unknown"


def test_missing_type_is_unknown():
    # Assumption: a missing ship_type (None, static data not yet received) is
    # treated like type 0.
    assert classify.ship_category(None) == "unknown"


def test_category_boundaries():
    assert classify.ship_category(59) not in {"cargo", "tanker", "passenger"}
    assert classify.ship_category(60) == "passenger"
    assert classify.ship_category(69) == "passenger"
    assert classify.ship_category(70) == "cargo"
    assert classify.ship_category(79) == "cargo"
    assert classify.ship_category(80) == "tanker"
    assert classify.ship_category(89) == "tanker"
