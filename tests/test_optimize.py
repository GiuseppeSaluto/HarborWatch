"""Black-box tests for optimize.plan_berths and optimize.prepare (spec section 9).

Requires OR-Tools (CP-SAT), which plan_berths uses; no network or MongoDB.
Times in plans are integer minutes from "now" (minute 0).
"""

import copy
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


def test_plan_without_ships_is_an_empty_list():
    # Spec section 13: [] (not None), also with occupied berths.
    for berths in ([], [berth("A")], [berth("A", free_from=100), berth("B", free_from=30)]):
        assert optimize.plan_berths([], berths) == [], berths


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


def test_prepare_free_from_is_whole_minutes_rounded_down_and_never_below_zero():
    # Seen arrival, cargo default stay: the expected departure is `left` minutes
    # from now. Fractions round down (90.75 -> 90, 15 s -> 0, not 1); a vessel
    # already past its expected stay frees the berth at minute 0, never negative.
    stay = config.SERVICE_MINUTES["cargo"]
    for left, expected in ((90.75, 90), (0.25, 0), (0, 0), (-0.5, 0), (-60, 0)):
        v = vessel(9, state="at_berth", since_minutes_ago=stay - left, since_seen=True)
        _, berths, _ = run_prepare([v], [pberth("A", *GENOA)])
        assert berths["A"]["free_from"] == expected, left


def test_prepare_half_stay_free_from_is_also_rounded_down():
    # Arrival not seen: the expected departure is since + half the default stay.
    # The same rounding applies: 90.75 minutes left -> 90 (round or ceil give 91),
    # 15 seconds left -> 0 (ceil gives 1).
    half = config.SERVICE_MINUTES["cargo"] / 2
    for left, expected in ((90.75, 90), (0.25, 0)):
        v = vessel(9, state="at_berth", since_minutes_ago=half - left, since_seen=False)
        _, berths, _ = run_prepare([v], [pberth("A", *GENOA)])
        assert berths["A"]["free_from"] == expected, left


# --- plan_berths: "wait" (spec section 13) ----------------------------------------------------
# wait = start - arrival, also when arrival is not 0.


def test_wait_is_zero_for_late_arrival_at_free_berth():
    by = plan([ship(1, arrival=30)], [berth("A")])
    assert by[1]["start"] == 30
    assert by[1]["wait"] == 0


def test_wait_counts_from_arrival_not_from_minute_zero():
    by = plan([ship(1, arrival=40)], [berth("A", free_from=100)])
    assert by[1]["start"] == 100
    assert by[1]["wait"] == 60


def test_wait_equals_start_minus_arrival_for_every_ship():
    for ships, berths in CASES:
        by = plan(ships, berths)
        for s in ships:
            p = by[s["mmsi"]]
            assert p["wait"] == p["start"] - s["arrival"], (s, p)


# --- prepare: points clarified on 2026-11-03 (spec section 13) -----------------------------


def test_prepare_service_sets_anchored_duration_and_berthed_departure():
    # Spec section 13: `service` (minutes per category) is used both for the
    # duration of anchored ships and for the expected departure of moored ones.
    service = {cat: minutes + 100 for cat, minutes in config.SERVICE_MINUTES.items()}
    vs = [vessel(1), vessel(9, state="at_berth", since_minutes_ago=60, since_seen=True,
                             lon=8.80, lat=44.40)]
    bs = [pberth("A", 8.801, 44.40), pberth("B", 8.95, 44.40)]
    waiting, out_berths, _ = optimize.prepare(vs, bs, NOW, service=service)
    waiting = {_mmsi(w): w for w in waiting}
    out_berths = {b["name"]: b for b in out_berths}
    assert waiting[1]["duration"] == service["cargo"]
    assert out_berths["A"]["free_from"] == service["cargo"] - 60


def test_prepare_without_category_uses_ship_category_of_ship_type():
    # Spec section 13: without `category` (or with an empty one) the category comes
    # from ship_type: 70 is planned as cargo, 37 (pleasure) is in neither list.
    # Both variants: a missing key and an empty string are distinct code paths.
    for missing in ("absent", "empty"):
        cargo, pleasure = vessel(1, ship_type=70), vessel(2, ship_type=37)
        for v in (cargo, pleasure):
            if missing == "absent":
                del v["category"]
            else:
                v["category"] = ""
        waiting, _, unfit = run_prepare([cargo, pleasure], [pberth("A", *GENOA)])
        assert set(waiting) == {1}, missing
        assert waiting[1]["category"] == "cargo", missing
        assert 2 not in unfit, missing


def test_prepare_returns_berth_copies_with_occupied_by_only_on_assigned_ones():
    # Spec section 13: returned berths are copies (input unchanged); a berth taken
    # by a moored ship has occupied_by = its mmsi, the others have no such key.
    v = vessel(9, state="at_berth", lon=8.80, lat=44.40)
    berths_in = [pberth("NEAR", 8.801, 44.40), pberth("FAR", 8.95, 44.40)]
    before = copy.deepcopy(berths_in)
    _, berths, _ = run_prepare([v], berths_in)
    assert berths_in == before
    assert berths["NEAR"]["occupied_by"] == 9
    assert "occupied_by" not in berths["FAR"]


def test_prepare_moored_ship_without_free_berth_does_not_stop_later_ones():
    # Spec section 13: more moored ships than berths in a zone: the extra one
    # occupies nothing and the moored ships after it are still assigned.
    first = vessel(7, state="at_berth", lon=8.80, lat=44.40)
    extra = vessel(8, state="at_berth", lon=8.80, lat=44.40)
    later = vessel(9, state="at_berth", ship_type=80, lon=8.95, lat=44.40)
    berths_in = [pberth("BOX", 8.801, 44.40, categories=("cargo",)),
                 pberth("OIL", 8.951, 44.40, categories=("tanker",))]
    _, berths, _ = run_prepare([first, extra, later], berths_in)
    assert berths["BOX"]["occupied_by"] == 7
    assert berths["OIL"]["occupied_by"] == 9
    assert berths["OIL"]["free_from"] > 0
    assert all(b.get("occupied_by") != 8 for b in berths.values())


def test_prepare_every_returned_berth_has_free_from_zero_when_free():
    # Spec section 13: every returned berth has `free_from`, 0 for a free berth.
    # The input berths have no such key, so it must be added by prepare.
    v = vessel(9, state="at_berth", lon=8.80, lat=44.40)
    berths_in = [pberth("NEAR", 8.801, 44.40), pberth("FAR", 8.95, 44.40),
                 pberth("OIL", 8.951, 44.40, categories=("tanker",))]
    assert all("free_from" not in b for b in berths_in)
    for vessels in ([v], []):
        _, berths, _ = run_prepare(vessels, berths_in)
        assert set(berths) == {"NEAR", "FAR", "OIL"}, vessels
        assert all("free_from" in b for b in berths.values()), vessels
        assert berths["FAR"]["free_from"] == 0, vessels
        assert berths["OIL"]["free_from"] == 0, vessels


def test_exclusion_reasons_and_eligibility():
    berth = dict(length=200, categories=["cargo"])
    vessel = dict(state="anchored", category="cargo", length=150)
    assert optimize.plan_reason(vessel, [berth]) is None
    assert "length unknown" in optimize.plan_reason({**vessel, "length": None}, [berth])
    assert "long enough" in optimize.plan_reason({**vessel, "length": 250}, [berth])
    assert "category" in optimize.plan_reason(vessel, [{**berth, "categories": ["tanker"]}])
    assert "commercial" in optimize.plan_reason({**vessel, "category": "pleasure"}, [berth])
    assert "at anchor" in optimize.plan_reason({**vessel, "state": "at_berth"}, [berth])


# --- plan_reason (spec section 13) -----------------------------------------------------
# The five checks, each failed alone by changing one thing of a plannable cargo ship.
PLANNABLE = dict(category="cargo", state="anchored", length=150)
CARGO_BERTHS = [pberth("BOX", *GENOA, length=300, categories=("cargo",))]
SHORT_TANKER_BERTHS = [pberth("OIL", *GENOA, length=100, categories=("tanker",))]
SHORT_CARGO_BERTHS = [pberth("BOX", *GENOA, length=100, categories=("cargo",))]


def single_failures():
    """Reason for each check failed alone, in the spec's order."""
    return [
        optimize.plan_reason({**PLANNABLE, "category": "pleasure"}, CARGO_BERTHS),
        optimize.plan_reason({**PLANNABLE, "state": "at_berth"}, CARGO_BERTHS),
        optimize.plan_reason({**PLANNABLE, "length": None}, CARGO_BERTHS),
        optimize.plan_reason(PLANNABLE, [pberth("OIL", *GENOA, categories=("tanker",))]),
        optimize.plan_reason(PLANNABLE, SHORT_CARGO_BERTHS),
    ]


def test_plan_reason_texts_start_with_not_planned_and_are_all_different():
    reasons = single_failures()
    assert all(r is not None and r.startswith("Not planned:") for r in reasons), reasons
    assert len(set(reasons)) == len(reasons), reasons


def test_plan_reason_gives_the_first_failing_check_in_spec_order():
    reasons = single_failures()
    # A vessel failing check k and every later one gets the reason of check k.
    # The berths hold one short tanker berth: no category match and nothing long enough.
    failing_from = [
        {"category": "pleasure", "state": "at_berth", "length": None},
        {"state": "at_berth", "length": None},
        {"length": None},
        {},
    ]
    for k, changes in enumerate(failing_from):
        got = optimize.plan_reason({**PLANNABLE, **changes}, SHORT_TANKER_BERTHS)
        assert got == reasons[k], (k, changes, got)


def test_plan_reason_length_counts_only_berths_of_the_category():
    # A long tanker berth does not make a cargo ship fit the short cargo berth.
    berths = SHORT_CARGO_BERTHS + [pberth("OIL", *GENOA, length=400, categories=("tanker",))]
    assert optimize.plan_reason(PLANNABLE, berths) == single_failures()[4]


def test_plan_reason_length_zero_is_unknown():
    unknown = optimize.plan_reason({**PLANNABLE, "length": None}, CARGO_BERTHS)
    assert optimize.plan_reason({**PLANNABLE, "length": 0}, CARGO_BERTHS) == unknown


def test_prepare_ships_are_exactly_those_plan_reason_accepts():
    berths = [pberth("BOX", 8.90, 44.40, length=300, categories=("cargo",)),
              pberth("OIL", 8.95, 44.40, length=250, categories=("tanker",))]
    vessels = [
        vessel(1),                                   # plannable
        vessel(2, ship_type=80, length=240),         # plannable tanker
        vessel(3, length=0),                         # length unknown
        vessel(4, length=None),                      # length unknown
        vessel(5, length=350),                       # too long for the cargo berth
        vessel(6, ship_type=60, length=200),         # passenger: no berth takes it
        vessel(7, ship_type=37),                     # not commercial
        vessel(8, state="underway"),                 # not anchored
        vessel(9, state="at_berth", since_minutes_ago=10),  # occupies BOX, not planned
        vessel(10, ship_type=80, length=260),        # tanker too long
    ]
    waiting, _, _ = run_prepare(vessels, berths)
    accepted = {v["mmsi"] for v in vessels if optimize.plan_reason(v, berths) is None}
    assert accepted == {1, 2}  # BOX occupied by vessel 9 does not exclude vessel 1
    assert set(waiting) == accepted


def test_prepare_given_category_is_used_as_is_without_recomputing():
    oil = [pberth("OIL", *GENOA, categories=("tanker",))]
    only_category = vessel(1, ship_type=80)
    del only_category["ship_type"]
    # ship_type 70 would give cargo: the given category must win.
    conflicting = {**vessel(2, ship_type=70), "category": "tanker"}
    for v in (only_category, conflicting):
        waiting, _, unfit = run_prepare([v], oil)
        assert set(waiting) == {v["mmsi"]} and not unfit, v
        assert waiting[v["mmsi"]]["duration"] == config.SERVICE_MINUTES["tanker"], v


def test_prepare_moored_ship_of_unknown_length_occupies_no_berth():
    for length in (None, 0):
        moored = vessel(1, state="at_berth", length=length, since_minutes_ago=10)
        later = vessel(2, state="at_berth", since_minutes_ago=10)
        _, berths, _ = run_prepare([moored], [pberth("A", *GENOA)])
        assert "occupied_by" not in berths["A"] and berths["A"]["free_from"] == 0, length
        # The berth stays free for the next moored ship in the given order.
        _, berths, _ = run_prepare([moored, later], [pberth("A", *GENOA)])
        assert berths["A"]["occupied_by"] == 2, length


# --- prepare: missing since_seen (spec section 13, 2026-11-10) -----------------------------


def _moored_free_from(since_seen):
    """free_from of berth A for one moored cargo ship, 100 min after since, stay 1000 min.

    since_seen is True, False, or "absent" (the key is removed from the vessel dict).
    """
    service = dict(config.SERVICE_MINUTES, cargo=1000)
    v = vessel(9, state="at_berth", since_minutes_ago=100, since_seen=bool(since_seen))
    if since_seen == "absent":
        del v["since_seen"]
    _, out_berths, _ = optimize.prepare([v], [pberth("A", *GENOA)], NOW, service=service)
    return {b["name"]: b for b in out_berths}["A"]["free_from"]


def test_prepare_moored_ship_without_since_seen_occupies_half_stay():
    # Spec section 13: a missing since_seen counts as false -> half stay:
    # 1000 // 2 - 100 = 400, not the full-stay 1000 - 100 = 900.
    assert _moored_free_from("absent") == 400


def test_prepare_missing_since_seen_matches_false_and_differs_from_true():
    assert _moored_free_from(True) == 900
    assert _moored_free_from(False) == 400
    assert _moored_free_from("absent") == _moored_free_from(False)
    assert _moored_free_from("absent") != _moored_free_from(True)
