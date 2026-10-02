import pytest

from classify import classify, distance_to_coast_m
from config import QUAY_DISTANCE_M, SPEED_THRESHOLD_KN

ANCHORED, MOORED, UNDERWAY = 1, 5, 0


@pytest.mark.parametrize("sog, nav_status, dist, expected", [
    (SPEED_THRESHOLD_KN, MOORED, 10, "underway"),       # threshold is exclusive; speed beats stale status
    (12.0, ANCHORED, 900, "underway"),
    (SPEED_THRESHOLD_KN - 0.1, UNDERWAY, 10, "at_berth"),  # slow by the quay, stale status
    (0, ANCHORED, QUAY_DISTANCE_M, "at_berth"),          # distance threshold is inclusive
    (0, MOORED, 900, "at_berth"),                        # off the coastline but declares moored
    (0, UNDERWAY, QUAY_DISTANCE_M + 1, "anchored"),
    (0.1, ANCHORED, 1000, "anchored"),
])
def test_classify(sog, nav_status, dist, expected):
    assert classify(sog, nav_status, dist) == expected


def test_distance_to_coast():
    line = {"coordinates": [[[8.9, 44.4], [8.91, 44.4]]]}  # ~800 m east-west segment
    assert distance_to_coast_m(8.905, 44.4, line) == pytest.approx(0, abs=0.01)
    assert distance_to_coast_m(8.905, 44.401, line) == pytest.approx(110.5, rel=0.01)  # 0.001 deg north
    assert distance_to_coast_m(8.92, 44.4, line) == pytest.approx(796, rel=0.01)       # past the end


def test_real_coastline():
    # Positions from the recorded stream: COSTA TOSCANA (moored) and HS AYSE ANA (at anchor).
    assert distance_to_coast_m(8.91758, 44.4112) < QUAY_DISTANCE_M
    assert distance_to_coast_m(8.84677, 44.39967) > 500
