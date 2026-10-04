import pytest

import config
from classify import COMMERCIAL
from optimize import plan_berths


def V(name, length, arrival, service, category="cargo"):
    return {"name": name, "length": length, "category": category, "arrival": arrival, "service": service}


def B(name, length, accepts=("cargo",)):
    return {"name": name, "length": length, "accepts": list(accepts)}


def check_plan(plan, vessels, berths):
    """Contract every plan must respect; returns the total wait."""
    vs = {v["name"]: v for v in vessels}
    bs = {b["name"]: b for b in berths}
    assert sorted(p["vessel"] for p in plan) == sorted(vs)  # every vessel exactly once
    assert [p["start"] for p in plan] == sorted(p["start"] for p in plan)
    for p in plan:
        v = vs[p["vessel"]]
        assert v["length"] <= bs[p["berth"]]["length"]
        assert v["category"] in bs[p["berth"]]["accepts"]
        assert p["start"] >= v["arrival"]
        assert p["end"] == p["start"] + v["service"]
        assert p["wait"] == p["start"] - v["arrival"]
    for a in plan:
        for b in plan:
            if a is not b and a["berth"] == b["berth"]:
                assert a["end"] <= b["start"] or b["end"] <= a["start"]  # no overlap on a berth
    return sum(p["wait"] for p in plan)


# Expected optima brute-forced (all berth assignments x all orders, earliest start each);
# a test-writing agent derived them from the spec alone, without seeing optimize.py.
@pytest.mark.parametrize("vessels, berths, optimum", [
    # Serving the late but quick HUGE first beats first-come-first-served (100 vs 110).
    ([V("BIG", 250, 0, 60), V("S1", 150, 0, 60), V("S2", 150, 0, 60), V("HUGE", 280, 10, 30)],
     [B("A", 200), B("B", 300)], 100),
    # Leaving the berth idle 1 min for QUICK is optimal (2 vs 99 for any no-idle greedy).
    ([V("BIG", 100, 0, 100), V("QUICK", 100, 1, 1)], [B("Q1", 200)], 2),
    # "First free berth" greedy puts SMALL on LONG and makes LARGE wait 60.
    ([V("SMALL", 100, 0, 60), V("LARGE", 250, 0, 60)], [B("LONG", 300), B("SHORT", 150)], 0),
    # A berth exactly as long as the vessel is usable.
    ([V("FIT", 200, 0, 30)], [B("Q1", 200)], 0),
    # A late vessel starts at its arrival, not when the berth frees up.
    ([V("EARLY", 100, 0, 10), V("LATE", 100, 50, 10)], [B("Q1", 150)], 0),
    # Mixed instance where first-come-first-served gives 160.
    ([V("A", 280, 0, 120), V("B", 120, 5, 30), V("C", 150, 10, 20),
      V("D", 290, 15, 60), V("E", 90, 20, 10), V("F", 200, 25, 45)],
     [B("LONG", 300), B("MID", 200), B("SHORT", 120)], 95),
    # Tankers only use the oil berth even when the cargo berth is idle: T2 waits 60 (0 if
    # the category were ignored).
    ([V("T1", 100, 0, 60, "tanker"), V("T2", 100, 0, 60, "tanker")],
     [B("OIL", 300, ["tanker"]), B("QUAY", 300)], 60),
])
def test_plan_is_valid_and_optimal(vessels, berths, optimum):
    assert check_plan(plan_berths(vessels, berths), vessels, berths) == optimum


def test_too_long_vessels_are_all_named():
    vessels = [V("TUGBOAT", 100, 0, 10), V("HUGE", 301, 0, 10), V("GIANT", 400, 5, 10)]
    with pytest.raises(ValueError) as e:
        plan_berths(vessels, [B("LONG", 300), B("SHORT", 150)])
    assert "HUGE" in str(e.value) and "GIANT" in str(e.value) and "TUGBOAT" not in str(e.value)


def test_vessel_with_no_berth_for_its_category():
    with pytest.raises(ValueError, match="CRUISER"):
        plan_berths([V("CRUISER", 100, 0, 10, "passenger")], [B("QUAY", 300)])


def test_berths_file():
    names = [b["name"] for b in config.BERTHS]
    assert len(names) == len(set(names))  # berth names are plan keys
    for b in config.BERTHS:
        assert b["length"] > 0 and b["accepts"] and set(b["accepts"]) <= COMMERCIAL, b["name"]
