"""Phase 2: berth allocation optimizer (OR-Tools CP-SAT)."""

import math
from datetime import timedelta

from ortools.sat.python import cp_model

import config
from classify import COMMERCIAL

# Hard cap on solving time; past it CP-SAT returns the best plan found so far.
# ponytail: ~10 vessels solve to optimality in milliseconds, 20+ hit the cap and return
# a good but unproven plan. Genoa has ~6-10 at anchor. Upgrade path if needed: a tighter
# horizon and symmetry breaking between berths of equal length.
SOLVER_SECONDS = 10


def fits(vessel, berth):
    """A berth is compatible if it is long enough and accepts the vessel category."""
    return berth["length"] >= vessel["length"] and vessel["category"] in berth["accepts"]


def prepare(states, berths, now):
    """Turn the current vessel states into plan_berths input.

    states: dicts with name, state, category, length (m or None), since (UTC datetime the
        vessel entered its state) and location ([lon, lat]).
    berths: as in data/berths.json. now: UTC datetime.
    Returns (vessels, berths): the commercial anchored vessels that fit some berth, waiting
    from minute 0 with the default service time of their category; and copies of the berths
    with free_from, the minutes until the vessel moored there is expected to leave.
    """
    # ponytail: service times are per-category defaults (spec section 9) and moored vessels
    # are matched to the nearest compatible berth. Upgrade path: stays measured from history,
    # and real berth geometries instead of one point per area.
    known = [s for s in states if s["category"] in COMMERCIAL and s["length"]]
    berths = [{**b, "free_from": 0} for b in berths]
    occupied = set()
    for s in (s for s in known if s["state"] == "at_berth"):
        free = [b for b in berths if b["name"] not in occupied and fits(s, b)]
        if not free:
            continue  # more vessels than berths we know of in that area
        berth = min(free, key=lambda b: math.dist(b["location"], s["location"]))
        occupied.add(berth["name"])
        leaves = s["since"] + timedelta(minutes=config.SERVICE_MINUTES[s["category"]])
        berth["free_from"] = max(0, int((leaves - now).total_seconds() // 60))
    vessels = [{"name": s["name"], "length": s["length"], "category": s["category"],
                "arrival": 0, "service": config.SERVICE_MINUTES[s["category"]]}
               for s in known if s["state"] == "anchored" and any(fits(s, b) for b in berths)]
    return vessels, berths


def plan_berths(vessels, berths):
    """Assign each vessel a berth and a start time, minimizing the total wait.

    vessels: dicts with name, length (m), category (cargo, tanker, passenger),
        arrival and service (whole minutes from now).
    berths: dicts with name, length (m) and accepts (list of categories), as in data/berths.json,
        plus optional free_from (minutes from now until the berth is free, default 0).
    Returns one dict per vessel (vessel, berth, start, end, wait), sorted by start.
    """
    # ponytail: deterministic model from spec section 9. No tides, pilotage windows or
    # commercial priorities.
    if not vessels:
        return []
    homeless = [v["name"] for v in vessels if not any(fits(v, b) for b in berths)]
    if homeless:
        raise ValueError(f"no berth long enough and accepting the category for: {', '.join(homeless)}")

    model = cp_model.CpModel()
    # Worst case: every vessel queues on one berth after the last arrival or berth release.
    latest = max([v["arrival"] for v in vessels] + [b.get("free_from", 0) for b in berths])
    horizon = latest + sum(v["service"] for v in vessels)

    starts, chosen = {}, {}
    per_berth = {b["name"]: [] for b in berths}
    for b in berths:
        if b.get("free_from", 0) > 0:  # the moored vessel, as a fixed block from minute 0
            per_berth[b["name"]].append(model.new_fixed_size_interval_var(0, b["free_from"], f"busy_{b['name']}"))
    for v in vessels:
        start = model.new_int_var(v["arrival"], horizon, f"start_{v['name']}")  # no start before arrival
        starts[v["name"]] = start
        for b in berths:
            if not fits(v, b):
                continue  # only compatible berths get an interval
            here = model.new_bool_var(f"{v['name']}_at_{b['name']}")
            interval = model.new_optional_fixed_size_interval_var(
                start, v["service"], here, f"{v['name']}_on_{b['name']}")
            chosen[v["name"], b["name"]] = here
            per_berth[b["name"]].append(interval)
        model.add_exactly_one(here for (name, _), here in chosen.items() if name == v["name"])

    for intervals in per_berth.values():
        model.add_no_overlap(intervals)  # an interval only counts when its vessel is placed there

    # Sum of waits; the arrivals are constants, so minimizing the starts is the same thing.
    model.minimize(sum(starts.values()))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = SOLVER_SECONDS
    status = solver.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(f"no plan found: {solver.status_name(status)}")

    plan = []
    for v in vessels:
        start = solver.value(starts[v["name"]])
        berth = next(b for (name, b), here in chosen.items() if name == v["name"] and solver.value(here))
        plan.append({"vessel": v["name"], "berth": berth, "start": start,
                     "end": start + v["service"], "wait": start - v["arrival"]})
    return sorted(plan, key=lambda p: p["start"])
