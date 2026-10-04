import pytest

from optimize import plan_berths

BERTHS = [{"name": "A", "length": 200}, {"name": "B", "length": 300}]
# BIG and HUGE only fit berth B; HUGE arrives at 10 and is quick, so the optimum
# serves it first and lets BIG wait: B = HUGE 10-40, BIG 40-100 (wait 40);
# A = S1 0-60, S2 60-120 (wait 60). Total 100, versus 110 serving BIG first.
VESSELS = [
    {"name": "BIG", "length": 250, "arrival": 0, "service": 60},
    {"name": "S1", "length": 150, "arrival": 0, "service": 60},
    {"name": "S2", "length": 150, "arrival": 0, "service": 60},
    {"name": "HUGE", "length": 280, "arrival": 10, "service": 30},
]


def test_plan_is_valid_and_optimal():
    plan = plan_berths(VESSELS, BERTHS)
    vessels = {v["name"]: v for v in VESSELS}
    berths = {b["name"]: b for b in BERTHS}
    assert sorted(p["vessel"] for p in plan) == sorted(vessels)
    for p in plan:
        assert berths[p["berth"]]["length"] >= vessels[p["vessel"]]["length"]
        assert p["start"] >= vessels[p["vessel"]]["arrival"]
    for name in berths:
        on_berth = sorted((p for p in plan if p["berth"] == name), key=lambda p: p["start"])
        assert all(a["end"] <= b["start"] for a, b in zip(on_berth, on_berth[1:])), name
    assert sum(p["wait"] for p in plan) == 100


def test_vessel_longer_than_every_berth():
    with pytest.raises(ValueError, match="GIANT"):
        plan_berths([{"name": "GIANT", "length": 400, "arrival": 0, "service": 60}], BERTHS)
