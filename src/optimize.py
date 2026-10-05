"""Phase 2: berth allocation optimizer (OR-Tools CP-SAT)."""

import itertools
import math
import statistics
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


def measure_stays(positions, max_gap_minutes):
    """Complete berth stays in a position history: (mmsi, start, end) for each.

    positions: dicts with mmsi, ts and state, sorted by (mmsi, ts). A stay counts only if we
    saw the vessel arrive and leave, with no gap over max_gap_minutes from the position
    before it to the one after it; end is the first position after the berth.
    """
    gap = timedelta(minutes=max_gap_minutes)
    stays = []
    for mmsi, group in itertools.groupby(positions, key=lambda p: p["mmsi"]):
        ps = list(group)
        raw = [p["state"] for p in ps]
        # A one-position blip (e.g. a moored vessel briefly over the speed threshold) is noise:
        # it takes the state of its two neighbours when they agree.
        state = [raw[i - 1] if 0 < i < len(raw) - 1 and raw[i - 1] == raw[i + 1] else raw[i]
                 for i in range(len(raw))]
        i = 0
        while i < len(ps):
            if state[i] != "at_berth":
                i += 1
                continue
            j = i
            while j + 1 < len(ps) and state[j + 1] == "at_berth":
                j += 1
            window = ps[i - 1:j + 2] if i > 0 else []  # the run plus one position each side
            if j + 1 < len(ps) and window and all(b["ts"] - a["ts"] <= gap for a, b in zip(window, window[1:])):
                stays.append((mmsi, ps[i]["ts"], ps[j + 1]["ts"]))
            i = j + 1
    return stays


def service_minutes(stays, category_of, defaults, min_stays):
    """Median stay per category, in whole minutes, once a category has min_stays stays.

    Returns (minutes, counts), both keyed like defaults; categories with too few stays keep
    their default.
    """
    lengths = {category: [] for category in defaults}
    for mmsi, start, end in stays:
        if category_of.get(mmsi) in lengths:
            lengths[category_of[mmsi]].append((end - start).total_seconds() / 60)
    minutes = {c: int(statistics.median(ls)) if len(ls) >= min_stays else defaults[c] for c, ls in lengths.items()}
    return minutes, {c: len(ls) for c, ls in lengths.items()}


def prepare(states, berths, now, service=None):
    """Turn the current vessel states into plan_berths input.

    states: dicts with name, state, category, length (m or None), since (UTC datetime the
        vessel entered its state), location ([lon, lat]) and optionally arrival_seen
        (default True; False when since is only when we first saw the vessel moored).
    berths: as in data/berths.json. now: UTC datetime.
    service: expected stay in minutes per category (from service_minutes), default config.SERVICE_MINUTES.
    Returns (vessels, berths): the commercial anchored vessels that fit some berth, waiting
    from minute 0 with the expected stay of their category; and copies of the berths
    with free_from, the minutes until the vessel moored there is expected to leave, and
    occupied_by, its name, on the berths a moored vessel was matched to.
    """
    # ponytail: one expected stay per category, and moored vessels are matched to the nearest
    # compatible berth. Upgrade path: stays per vessel size or terminal, real berth geometries.
    service = service or config.SERVICE_MINUTES
    known = [s for s in states if s["category"] in COMMERCIAL and s["length"]]
    berths = [{**b, "free_from": 0} for b in berths]
    occupied = set()
    for s in (s for s in known if s["state"] == "at_berth"):
        free = [b for b in berths if b["name"] not in occupied and fits(s, b)]
        if not free:
            continue  # more vessels than berths we know of in that area
        berth = min(free, key=lambda b: math.dist(b["location"], s["location"]))
        occupied.add(berth["name"])
        berth["occupied_by"] = s["name"]
        stay = service[s["category"]]
        # Arrival not seen (e.g. moored before the ingestion started): assume it was halfway
        # through its stay when we first saw it, instead of just arrived.
        # ponytail: a coin-flip guess; the upgrade is reading the arrival from the history.
        leaves = s["since"] + timedelta(minutes=stay if s.get("arrival_seen", True) else stay / 2)
        berth["free_from"] = max(0, int((leaves - now).total_seconds() // 60))
    vessels = [{"name": s["name"], "length": s["length"], "category": s["category"],
                "arrival": 0, "service": service[s["category"]]}
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
