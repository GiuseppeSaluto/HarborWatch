"""Black-box tests for optimize.plan_berths and optimize.prepare (spec section 9).

Requires OR-Tools (CP-SAT), which plan_berths uses; no network or MongoDB.
Times in plans are integer minutes from "now" (minute 0).
"""

import itertools
from datetime import datetime, timedelta, timezone

import pytest

import config
import optimize

# --- data shape assumptions ---------------------------------------------------------
# Assumption: plan_berths(ships, berths) takes
#   ships:  list of dicts {"mmsi", "length", "category", "arrival", "duration"}
#           (arrival and duration in minutes),
#   berths: list of dicts {"name", "length", "categories", "free_from"}, where
#           "free_from" (minutes, optional / 0 = free) is the end of the fixed block
#           of an already-berthed vessel (spec: "a fixed block from minute 0 to its
#           free_from"),
# and returns a list of dicts {"mmsi", "berth" (berth name), "start", "end"}.
# The spec describes the model but names none of these keys.


def ship(mmsi, length=150, category="cargo", arrival=0, duration=60):
    return {"mmsi": mmsi, "length": length, "category": category,
            "arrival": arrival, "duration": duration}


def berth(name, length=300, categories=("cargo",), free_from=0):
    return {"name": name, "length": length, "categories": list(categories),
            "free_from": free_from}


def plan(ships, berths):
    result = optimize.plan_berths(ships, berths)
    return {p["mmsi"]: p for p in result}


def total_delay(by_mmsi, ships):
    return sum(by_mmsi[s["mmsi"]]["start"] - s["arrival"] for s in ships)


def check_valid(by_mmsi, ships, berths):
    """Every invariant the spec states for a plan."""
    berths_by_name = {b["name"]: b for b in berths}
    assert set(by_mmsi) == {s["mmsi"] for s in ships}, "each ship planned exactly once"
    for s in ships:
        p = by_mmsi[s["mmsi"]]
        b = berths_by_name[p["berth"]]
        assert b["length"] >= s["length"], "berth long enough"
        assert s["category"] in b["categories"], "berth accepts category"
        assert p["start"] >= s["arrival"], "no start before arrival"
        assert p["start"] >= b.get("free_from", 0), "no overlap with berthed vessel"
        assert p["end"] - p["start"] == s["duration"], "interval length = duration"
    for name in berths_by_name:
        ivs = sorted((p["start"], p["end"]) for p in by_mmsi.values() if p["berth"] == name)
        for (s1, e1), (s2, _) in zip(ivs, ivs[1:]):
            assert s2 >= e1, f"overlap on berth {name}"


def brute_force_optimum(ships, berths):
    """Minimum total delay by enumerating berth choice and per-berth order."""
    best = None
    names = [b["name"] for b in berths]
    bmap = {b["name"]: b for b in berths}
    for choice in itertools.product(names, repeat=len(ships)):
        ok = all(bmap[c]["length"] >= s["length"] and s["category"] in bmap[c]["categories"]
                 for s, c in zip(ships, choice))
        if not ok:
            continue
        groups = {n: [s for s, c in zip(ships, choice) if c == n] for n in names}
        total = 0
        for n, group in groups.items():
            best_group = None
            for order in itertools.permutations(group):
                t = bmap[n].get("free_from", 0)
                d = 0
                for s in order:
                    start = max(t, s["arrival"])
                    d += start - s["arrival"]
                    t = start + s["duration"]
                best_group = d if best_group is None else min(best_group, d)
            total += best_group or 0
        best = total if best is None else min(best, total)
    return best


# --- validity --------------------------------------------------------------------------

def test_single_ship_single_free_berth_starts_at_arrival():
    ships = [ship(1, arrival=0)]
    berths = [berth("A")]
    by = plan(ships, berths)
    check_valid(by, ships, berths)
    assert by[1]["start"] == 0
    assert by[1]["berth"] == "A"


def test_start_not_before_late_arrival():
    ships = [ship(1, arrival=30)]
    berths = [berth("A")]
    by = plan(ships, berths)
    check_valid(by, ships, berths)
    assert by[1]["start"] == 30


def test_no_overlap_on_single_berth():
    ships = [ship(i, duration=40) for i in range(1, 5)]
    berths = [berth("A")]
    by = plan(ships, berths)
    check_valid(by, ships, berths)
    assert total_delay(by, ships) == 0 + 40 + 80 + 120


def test_length_constraint_sends_long_ship_to_long_berth():
    ships = [ship(1, length=250), ship(2, length=150)]
    berths = [berth("LONG", length=300), berth("SHORT", length=200)]
    by = plan(ships, berths)
    check_valid(by, ships, berths)
    assert by[1]["berth"] == "LONG"
    assert by[2]["berth"] == "SHORT"
    assert total_delay(by, ships) == 0


def test_long_ship_waits_rather_than_using_short_free_berth():
    ships = [ship(1, length=250, duration=60), ship(2, length=250, duration=60)]
    berths = [berth("LONG", length=300), berth("SHORT", length=200)]
    by = plan(ships, berths)
    check_valid(by, ships, berths)
    assert {by[1]["berth"], by[2]["berth"]} == {"LONG"}
    assert total_delay(by, ships) == 60


def test_berth_length_equal_to_ship_length_is_compatible():
    ships = [ship(1, length=300)]
    berths = [berth("A", length=300)]
    by = plan(ships, berths)
    check_valid(by, ships, berths)
    assert by[1]["berth"] == "A"


def test_category_constraint_tanker_avoids_container_berth():
    ships = [ship(1, category="tanker", duration=60), ship(2, category="tanker", duration=60)]
    berths = [berth("CONTAINER", categories=("cargo",)),
              berth("OIL", categories=("tanker",))]
    by = plan(ships, berths)
    check_valid(by, ships, berths)
    assert by[1]["berth"] == by[2]["berth"] == "OIL"
    assert total_delay(by, ships) == 60


def test_mixed_categories_use_their_own_berths():
    ships = [ship(1, category="passenger"), ship(2, category="cargo"), ship(3, category="tanker")]
    berths = [berth("CRUISE", categories=("passenger",)),
              berth("BOX", categories=("cargo",)),
              berth("OIL", categories=("tanker",))]
    by = plan(ships, berths)
    check_valid(by, ships, berths)
    assert (by[1]["berth"], by[2]["berth"], by[3]["berth"]) == ("CRUISE", "BOX", "OIL")
    assert total_delay(by, ships) == 0


def test_occupied_berth_blocks_until_free_from():
    ships = [ship(1)]
    berths = [berth("A", free_from=100)]
    by = plan(ships, berths)
    check_valid(by, ships, berths)
    assert by[1]["start"] == 100


def test_free_compatible_berth_preferred_over_occupied_one():
    ships = [ship(1)]
    berths = [berth("BUSY", free_from=100), berth("FREE")]
    by = plan(ships, berths)
    check_valid(by, ships, berths)
    assert by[1]["berth"] == "FREE"
    assert by[1]["start"] == 0


# --- optimality ------------------------------------------------------------------------

def test_shortest_job_first_on_one_berth():
    # Durations 30, 10, 20 all arriving at 0: optimum order 10, 20, 30 -> 0+10+30 = 40.
    ships = [ship(1, duration=30), ship(2, duration=10), ship(3, duration=20)]
    berths = [berth("A")]
    by = plan(ships, berths)
    check_valid(by, ships, berths)
    assert total_delay(by, ships) == 40
    assert by[2]["start"] == 0


def test_two_berths_split_load():
    ships = [ship(i, duration=60) for i in range(1, 5)]
    berths = [berth("A"), berth("B")]
    by = plan(ships, berths)
    check_valid(by, ships, berths)
    assert total_delay(by, ships) == 120


def test_late_arrival_does_not_block_earlier_ship():
    # Ship 2 arrives at 50; ship 1 (duration 40) fits before it with no delay.
    ships = [ship(1, arrival=0, duration=40), ship(2, arrival=50, duration=40)]
    berths = [berth("A")]
    by = plan(ships, berths)
    check_valid(by, ships, berths)
    assert total_delay(by, ships) == 0


CASES = [
    # (ships, berths)
    ([ship(1, duration=50, length=250), ship(2, duration=20), ship(3, duration=70, arrival=10)],
     [berth("L", length=300), berth("S", length=200, free_from=30)]),
    ([ship(1, category="tanker", duration=90), ship(2, duration=30), ship(3, duration=30, arrival=5),
      ship(4, category="tanker", duration=20, arrival=15)],
     [berth("MIX", categories=("cargo", "tanker"), free_from=20), berth("BOX")]),
    ([ship(1, duration=100), ship(2, duration=10, arrival=20), ship(3, duration=10, arrival=25),
      ship(4, duration=60, length=280)],
     [berth("A", length=300), berth("B", length=200, free_from=50)]),
    ([ship(i, duration=d, arrival=a) for i, (d, a) in enumerate([(45, 0), (15, 5), (30, 10), (60, 0), (20, 40)], 1)],
     [berth("A"), berth("B", free_from=25)]),
]


@pytest.mark.parametrize("ships,berths", CASES)
def test_plan_matches_brute_force_optimum(ships, berths):
    by = plan(ships, berths)
    check_valid(by, ships, berths)
    assert total_delay(by, ships) == brute_force_optimum(ships, berths)


# --- prepare ---------------------------------------------------------------------------
# Assumption: optimize.prepare(vessels, berths, now) takes
#   vessels: list of merged state+static dicts {"mmsi", "state", "since" (aware
#            datetime), "since_seen", "length", "ship_type", "location" (GeoJSON)}
#            (also "category", "lat", "lon" are supplied in case the code reads those),
#   berths:  list of berth dicts as in config.BERTHS, each with one point
#            (supplied as "point" [lon, lat], "lon"/"lat" and "location"),
#   now:     aware datetime,
# and returns a 3-tuple (waiting_ships, berths_with_free_from, unfit_ships):
# ships in the plan_berths shape with arrival 0, berths with "free_from" in minutes
# from now, and the anchored commercial ships that fit no known berth.
# Durations come from config.SERVICE_MINUTES (defaults: cargo 24 h).

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
GENOA = (8.90, 44.40)


def vessel(mmsi, state="anchored", length=200, ship_type=70, since_minutes_ago=60,
           since_seen=True, lon=GENOA[0], lat=GENOA[1]):
    cat = {7: "cargo", 8: "tanker", 6: "passenger"}.get(ship_type // 10 if ship_type else 0, "other")
    return {"mmsi": mmsi, "state": state, "since": NOW - timedelta(minutes=since_minutes_ago),
            "since_seen": since_seen, "length": length, "ship_type": ship_type,
            "category": cat, "location": {"type": "Point", "coordinates": [lon, lat]},
            "lon": lon, "lat": lat}


def pberth(name, lon, lat, length=300, categories=("cargo",)):
    return {"name": name, "zone": name, "length": length, "categories": list(categories),
            "source": "official", "point": [lon, lat], "lon": lon, "lat": lat,
            "location": {"type": "Point", "coordinates": [lon, lat]}}


def _mmsi(x):
    return x if isinstance(x, int) else x["mmsi"]


def run_prepare(vessels, berths):
    waiting, out_berths, unfit = optimize.prepare(vessels, berths, NOW)
    return ({_mmsi(w): w for w in waiting},
            {b["name"]: b for b in out_berths},
            {_mmsi(u) for u in unfit})


def test_prepare_keeps_anchored_commercial_ship_with_arrival_zero():
    waiting, _, _ = run_prepare([vessel(1)], [pberth("A", *GENOA)])
    assert set(waiting) == {1}
    assert waiting[1]["arrival"] == 0


def test_prepare_waiting_ship_gets_category_duration():
    waiting, _, _ = run_prepare([vessel(1, ship_type=80)],
                                [pberth("OIL", *GENOA, categories=("tanker",))])
    assert waiting[1]["duration"] == config.SERVICE_MINUTES["tanker"]


def test_prepare_drops_non_commercial_anchored_ship():
    waiting, _, unfit = run_prepare([vessel(1, ship_type=52)], [pberth("A", *GENOA)])
    assert 1 not in waiting
    assert 1 not in unfit


def test_prepare_drops_anchored_ship_with_unknown_length():
    waiting, _, _ = run_prepare([vessel(1, length=None)], [pberth("A", *GENOA)])
    assert 1 not in waiting


def test_prepare_drops_underway_ship():
    waiting, _, unfit = run_prepare([vessel(1, state="underway")], [pberth("A", *GENOA)])
    assert 1 not in waiting and 1 not in unfit


def test_prepare_lists_ship_too_long_for_every_berth_as_unfit():
    waiting, _, unfit = run_prepare([vessel(1, length=400)], [pberth("A", *GENOA, length=300)])
    assert 1 not in waiting
    assert 1 in unfit


def test_prepare_lists_ship_with_no_category_berth_as_unfit():
    waiting, _, unfit = run_prepare([vessel(1, ship_type=80)],
                                    [pberth("BOX", *GENOA, categories=("cargo",))])
    assert 1 not in waiting
    assert 1 in unfit


def test_prepare_free_berths_have_no_block():
    _, berths, _ = run_prepare([vessel(1)], [pberth("A", *GENOA)])
    assert berths["A"].get("free_from", 0) == 0


def test_prepare_berthed_ship_occupies_until_since_plus_duration():
    # Seen arrival 2 h ago, cargo default 24 h -> free in 22 h.
    v = vessel(9, state="at_berth", since_minutes_ago=120, since_seen=True)
    _, berths, _ = run_prepare([v], [pberth("A", *GENOA)])
    assert berths["A"]["free_from"] == config.SERVICE_MINUTES["cargo"] - 120


def test_prepare_unseen_arrival_assumes_half_stay():
    # Arrival not seen: since is taken as mid-stay -> free at since + 12 h.
    v = vessel(9, state="at_berth", since_minutes_ago=120, since_seen=False)
    _, berths, _ = run_prepare([v], [pberth("A", *GENOA)])
    assert berths["A"]["free_from"] == config.SERVICE_MINUTES["cargo"] // 2 - 120


def test_prepare_berthed_ship_takes_nearest_compatible_berth():
    v = vessel(9, state="at_berth", lon=8.80, lat=44.40)
    berths_in = [pberth("FAR", 8.95, 44.40), pberth("NEAR", 8.801, 44.40)]
    _, berths, _ = run_prepare([v], berths_in)
    assert berths["NEAR"].get("free_from", 0) > 0
    assert berths["FAR"].get("free_from", 0) == 0


def test_prepare_berthed_ship_skips_nearer_incompatible_berth():
    v = vessel(9, state="at_berth", ship_type=80, lon=8.80, lat=44.40)
    berths_in = [pberth("BOX", 8.801, 44.40, categories=("cargo",)),
                 pberth("OIL", 8.95, 44.40, categories=("tanker",))]
    _, berths, _ = run_prepare([v], berths_in)
    assert berths["OIL"].get("free_from", 0) > 0
    assert berths["BOX"].get("free_from", 0) == 0


def test_prepare_two_berthed_ships_do_not_share_a_berth():
    # The second one takes the nearest *free* compatible berth.
    a = vessel(8, state="at_berth", lon=8.80, lat=44.40)
    b = vessel(9, state="at_berth", lon=8.80, lat=44.40)
    berths_in = [pberth("NEAR", 8.801, 44.40), pberth("NEXT", 8.81, 44.40)]
    _, berths, _ = run_prepare([a, b], berths_in)
    assert berths["NEAR"].get("free_from", 0) > 0
    assert berths["NEXT"].get("free_from", 0) > 0


def test_prepare_non_commercial_berthed_ship_occupies_nothing():
    v = vessel(9, state="at_berth", ship_type=52)
    _, berths, _ = run_prepare([v], [pberth("A", *GENOA)])
    assert berths["A"].get("free_from", 0) == 0


def test_prepare_output_feeds_plan_berths():
    vs = [vessel(1), vessel(2, length=250),
          vessel(9, state="at_berth", since_minutes_ago=1380, lon=8.801, lat=44.40)]
    bs = [pberth("A", 8.801, 44.40), pberth("B", 8.95, 44.40)]
    waiting, berths, _ = optimize.prepare(vs, bs, NOW)
    by = {p["mmsi"]: p for p in optimize.plan_berths(waiting, berths)}
    assert set(by) == {1, 2}
    # Berth A is free after 60 minutes; one ship goes to B at 0, the other to A at 60.
    assert sorted(p["start"] for p in by.values()) == [0, 60]
