import copy
from datetime import UTC, datetime, timedelta

import pytest

import config
from classify import COMMERCIAL
from optimize import plan_berths, prepare


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


# Tests below were written by a test-writing agent from the prepare/free_from contract alone,
# without seeing optimize.py.
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
WEST, EAST = [8.79, 44.42], [8.92, 44.40]  # two port areas ~10 km apart
NEAR_EAST = [8.919, 44.401]


def ship(name, state, category="cargo", length=200, minutes_ago=0, location=EAST):
    return {"name": name, "state": state, "category": category, "length": length,
            "since": NOW - timedelta(minutes=minutes_ago), "location": location}


def berth(name, location, length=300, accepts=("cargo",)):
    return {"name": name, "area": name, "length": length, "accepts": list(accepts),
            "location": location, "source": "test"}


@pytest.mark.parametrize("berths, arrival, start", [
    ([{"name": "A", "length": 300, "accepts": ["cargo"], "free_from": 30}], 0, 30),   # free_from ignored
    ([{"name": "A", "length": 300, "accepts": ["cargo"], "free_from": 30}], 40, 40),  # start forced to free_from even if arrived later
    ([{"name": "A", "length": 300, "accepts": ["cargo"]}], 5, 5),                     # missing free_from not treated as 0
    ([{"name": "A", "length": 300, "accepts": ["cargo"], "free_from": 50},
      {"name": "B", "length": 300, "accepts": ["cargo"]}], 0, 0),                     # picks occupied berth over free one
])
def test_plan_berths_free_from(berths, arrival, start):
    # Bugs: see per-case comments.
    [row] = plan_berths([{"name": "V", "length": 200, "category": "cargo", "arrival": arrival, "service": 60}], berths)
    assert (row["start"], row["end"], row["wait"]) == (start, start + 60, start - arrival)


@pytest.mark.parametrize("state", [
    ship("underway", "underway"),
    ship("moored", "at_berth"),
    ship("pleasure", "anchored", category="pleasure"),
    ship("service", "anchored", category="service"),
    ship("unknown", "anchored", category="unknown"),
    ship("no length", "anchored", length=None),
    ship("too long", "anchored", length=301),
    ship("no tanker berth", "anchored", category="tanker"),
])
def test_prepare_excludes(state):
    # Bug: a non-anchored, non-commercial, unknown-length or fitting-nowhere vessel reaches plan_berths.
    vessels, _ = prepare([state], [berth("A", EAST)], NOW)
    assert vessels == []


@pytest.mark.parametrize("category", ["cargo", "tanker", "passenger"])
def test_prepare_anchored_vessel(category):
    # Bug: wrong arrival (e.g. since-based instead of 0), wrong service lookup, or missing keys.
    berths = [berth("A", EAST, accepts=["cargo", "tanker", "passenger"])]
    vessels, _ = prepare([ship("V", "anchored", category=category, minutes_ago=90)], berths, NOW)
    assert vessels == [{"name": "V", "length": 200, "category": category,
                        "arrival": 0, "service": config.SERVICE_MINUTES[category]}]


@pytest.mark.parametrize("seconds_ago, expected", [
    (600, [config.SERVICE_MINUTES["cargo"] - 10]),
    (630, [config.SERVICE_MINUTES["cargo"] - 11, config.SERVICE_MINUTES["cargo"] - 10]),  # rounding direction left open
    ((config.SERVICE_MINUTES["cargo"] + 100) * 60, [0]),                           # overstayed: clamped, not negative
])
def test_prepare_free_from_arithmetic(seconds_ago, expected):
    # Bug: wrong sign/units in since + service - now, missing clamp at 0, or a float/timedelta result.
    moored = ship("M", "at_berth") | {"since": NOW - timedelta(seconds=seconds_ago)}
    _, [b] = prepare([moored], [berth("A", EAST)], NOW)
    assert type(b["free_from"]) is int and b["free_from"] in expected


def test_prepare_nearest_then_next_free_berth():
    # Bug: moored vessel takes the first listed berth instead of the nearest, or two vessels share one berth.
    s = config.SERVICE_MINUTES["cargo"]
    berths = [berth("W", WEST), berth("E1", EAST), berth("E2", EAST)]
    _, out = prepare([ship("M1", "at_berth", location=NEAR_EAST, minutes_ago=0)], berths, NOW)
    free = {b["name"]: b["free_from"] for b in out}
    assert free["W"] == 0 and sorted([free["E1"], free["E2"]]) == [0, s]

    moored = [ship("M1", "at_berth", location=NEAR_EAST, minutes_ago=0),
              ship("M2", "at_berth", location=NEAR_EAST, minutes_ago=10)]
    _, out = prepare(moored, berths, NOW)
    free = {b["name"]: b["free_from"] for b in out}
    assert free["W"] == 0 and sorted([free["E1"], free["E2"]]) == [s - 10, s]


def test_prepare_occupancy_respects_compatibility():
    # Bug: moored vessel occupies the nearest berth even if wrong category/too short; non-commercial,
    # unknown-length or anchored vessels occupy berths; a vessel with no free compatible berth crashes or double-books.
    berths = [berth("cargo here", EAST), berth("tanker far", WEST, accepts=["tanker"]),
              berth("short here", NEAR_EAST, length=150), berth("long far", WEST, length=400)]
    states = [ship("T", "at_berth", category="tanker", location=EAST),
              ship("big", "at_berth", length=350, location=NEAR_EAST),
              ship("pleasure", "at_berth", category="pleasure", location=EAST),
              ship("unknown length", "at_berth", length=None, location=EAST),
              ship("waiting", "anchored", location=EAST),
              ship("T2", "at_berth", category="tanker", location=WEST, minutes_ago=5)]  # tanker berth already taken
    _, out = prepare(states, berths, NOW)
    free = {b["name"]: b["free_from"] for b in out}
    assert free["cargo here"] == 0 and free["short here"] == 0
    assert free["long far"] == config.SERVICE_MINUTES["cargo"]
    assert free["tanker far"] in (config.SERVICE_MINUTES["tanker"], config.SERVICE_MINUTES["tanker"] - 5)


def test_prepare_berths_not_mutated_and_feed_plan_berths():
    # Bug: prepare writes free_from into the caller's berths (e.g. config.BERTHS), drops berth keys,
    # or its output is not usable by plan_berths.
    before = copy.deepcopy(config.BERTHS)
    vessels, out = prepare([], config.BERTHS, NOW)
    assert config.BERTHS == before and vessels == []
    assert sorted(out, key=lambda b: b["name"]) == sorted((b | {"free_from": 0} for b in config.BERTHS), key=lambda b: b["name"])

    berths = [berth("A", EAST)]
    states = [ship("moored", "at_berth", minutes_ago=0), ship("waiting", "anchored")]
    vessels, out = prepare(states, berths, NOW)
    assert "free_from" not in berths[0]
    [row] = plan_berths(vessels, out)
    assert (row["vessel"], row["start"]) == ("waiting", config.SERVICE_MINUTES["cargo"])
