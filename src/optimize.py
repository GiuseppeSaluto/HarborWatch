"""Phase 2: berth allocation optimizer (OR-Tools CP-SAT)."""

import itertools
import math
import statistics
from datetime import timedelta

from ortools.sat.python import cp_model

import config
from classify import COMMERCIAL

# Hard cap on solving time; past it CP-SAT returns the best plan found so far.
# Known limit: ~10 vessels solve to optimality in milliseconds, 20+ hit the cap and return
# a good but unproven plan. Genoa has ~6-10 at anchor. Upgrade path if needed: a tighter
# horizon and symmetry breaking between berths of equal length.
SOLVER_SECONDS = 10


def fits(vessel, berth):
    """A berth is compatible if it is long enough and accepts the vessel category."""
    return berth["length"] >= vessel["length"] and vessel["category"] in berth["categories"]


def measure_stays(positions, max_gap_minutes=None):
    """Complete berth stays in a position history: {mmsi, start, end} for each.

    positions: dicts with mmsi, ts and state, in any order. A stay counts only if we saw the
    vessel arrive and leave, with no gap over max_gap_minutes (default STALE_MINUTES) from
    the position before it to the one after it; end is the first position after the berth.
    """
    gap = timedelta(minutes=config.STALE_MINUTES if max_gap_minutes is None else max_gap_minutes)
    stays = []
    ordered = sorted(positions, key=lambda p: (p["mmsi"], p["ts"]))
    for mmsi, group in itertools.groupby(ordered, key=lambda p: p["mmsi"]):
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
                stays.append({"mmsi": mmsi, "start": ps[i]["ts"], "end": ps[j + 1]["ts"]})
            i = j + 1
    return stays


def stays_pipeline(max_gap_minutes):
    """measure_stays as a MongoDB aggregation on positions: only the stays leave the server.

    Returns documents {mmsi, start, end}. measure_stays stays the tested definition; this
    pipeline must give the same stays (checked against it on real and random histories).
    """
    # explain(): the first window reads the (mmsi, ts) index already in order and fetches only
    # mmsi, ts, state; MongoDB re-sorts in memory before the later windows, on documents that
    # small (~300k at the 7-day TTL) well under the 100 MB per-stage limit.
    gap_ms = max_gap_minutes * 60_000  # date minus date is in milliseconds
    by_vessel = {"partitionBy": "$mmsi", "sortBy": {"ts": 1}}
    shift = lambda field, by: {"$shift": {"output": field, "by": by}}
    return [
        {"$match": {"state": {"$exists": True}}},
        # 1. Each position sees its neighbours: same vessel, in time order.
        {"$setWindowFields": {**by_vessel, "output": {
            "prev_state": shift("$state", -1), "next_state": shift("$state", 1), "prev_ts": shift("$ts", -1)}}},
        # 2. Noise: a one-position blip takes its neighbours' state when they agree; gap since the previous one.
        {"$set": {
            "state": {"$cond": [{"$and": [{"$ne": ["$prev_state", None]}, {"$eq": ["$prev_state", "$next_state"]}]},
                                "$prev_state", "$state"]},
            "gap": {"$subtract": ["$ts", "$prev_ts"]}}},
        # 3. Number the runs of equal state: a running sum of "state changed here".
        {"$setWindowFields": {**by_vessel, "output": {"prev_smoothed": shift("$state", -1)}}},
        {"$set": {"new_run": {"$cond": [{"$eq": ["$state", "$prev_smoothed"]}, 0, 1]}}},
        {"$setWindowFields": {**by_vessel, "output": {
            "run": {"$sum": "$new_run", "window": {"documents": ["unbounded", "current"]}}}}},
        # 4. One document per run; the gap before it is the one of its first position.
        {"$group": {
            "_id": {"mmsi": "$mmsi", "run": "$run"}, "state": {"$first": "$state"},
            "start": {"$min": "$ts"}, "last": {"$max": "$ts"},
            "gap_before": {"$max": {"$cond": ["$new_run", "$gap", None]}},
            "gap_inside": {"$max": {"$cond": ["$new_run", None, "$gap"]}}}},
        # 5. Each run sees the next run of the same vessel: when it starts and the gap to it.
        {"$setWindowFields": {"partitionBy": "$_id.mmsi", "sortBy": {"_id.run": 1}, "output": {
            "next_start": shift("$start", 1), "gap_after": shift("$gap_before", 1)}}},
        # 6. Complete berth stays: arrival and departure seen, no gap over max_gap_minutes.
        {"$match": {
            "state": "at_berth",
            "gap_before": {"$ne": None, "$lte": gap_ms}, "gap_after": {"$ne": None, "$lte": gap_ms},
            "$or": [{"gap_inside": None}, {"gap_inside": {"$lte": gap_ms}}]}},
        {"$project": {"_id": 0, "mmsi": "$_id.mmsi", "start": 1, "end": "$next_start"}},
        {"$sort": {"mmsi": 1, "start": 1}},
    ]


def trusted_stay(count, default_minutes, window_minutes, min_stays):
    """Whether a category's measured stays can replace its default.

    Needs min_stays complete stays, seen in a continuous collection window at least
    STAY_WINDOW_FACTOR times the default stay (window_minutes None skips that condition).
    """
    long_enough = window_minutes is None or window_minutes >= config.STAY_WINDOW_FACTOR * default_minutes
    return count >= min_stays and long_enough


def usable_stays(stays):
    """The stays that say something about berth calls.

    stays: dicts with category, length (m, or None) and minutes. Harbour boats under
    MIN_STAY_VESSEL_M and stays under MIN_STAY_MINUTES are left out: on 2026-10-04 they
    dragged the passenger median down to 8 minutes.
    """
    return [s for s in stays
            if (s["length"] or 0) >= config.MIN_STAY_VESSEL_M and s["minutes"] >= config.MIN_STAY_MINUTES]


def service_minutes(stays, window_minutes=None, defaults=None, min_stays=None):
    """Expected stay per category, in whole minutes: the median of the usable stays where
    trusted_stay allows it, else the default.

    stays: dicts with category, length and minutes. window_minutes: the longest continuous
    collection window in the history they come from. defaults and min_stays default to
    config.SERVICE_MINUTES and config.MIN_STAYS. Returns a dict keyed like defaults.
    """
    defaults = config.SERVICE_MINUTES if defaults is None else defaults
    min_stays = config.MIN_STAYS if min_stays is None else min_stays
    minutes = {category: [] for category in defaults}
    for stay in usable_stays(stays):
        if stay["category"] in minutes:
            minutes[stay["category"]].append(stay["minutes"])
    return {c: int(statistics.median(ms)) if trusted_stay(len(ms), defaults[c], window_minutes, min_stays)
            else defaults[c] for c, ms in minutes.items()}


def prepare(vessels, berths, now, service=None):
    """Turn the current vessel states into plan_berths input.

    vessels: dicts with mmsi, state, category, length (m or None), since (UTC datetime the
        vessel entered its state), since_seen (False when since is only when we first saw it,
        default True), lon and lat.
    berths: as in data/berths.json (name, zone, length, categories, lon, lat). now: UTC datetime.
    service: expected stay in minutes per category, default config.SERVICE_MINUTES.
    Returns (ships, berths, unfit):
    - ships: the commercial anchored vessels that fit some berth, in the plan_berths shape,
      waiting from minute 0 for the expected stay of their category;
    - berths: copies with free_from, the minutes until the vessel moored there is expected to
      leave, and occupied_by, its mmsi, on the berths a moored vessel was matched to;
    - unfit: the commercial anchored vessels left out (no fitting berth, or length unknown),
      so that the dashboard lists them instead of dropping them silently.
    """
    # Known limit: one expected stay per category, and moored vessels are matched to the nearest
    # compatible berth. Upgrade path: stays per vessel size or terminal, real berth geometries.
    service = service or config.SERVICE_MINUTES
    known = [v for v in vessels if v["category"] in COMMERCIAL and v["length"]]
    berths = [{**b, "free_from": 0} for b in berths]
    occupied = set()
    for v in (v for v in known if v["state"] == "at_berth"):
        free = [b for b in berths if b["name"] not in occupied and fits(v, b)]
        if not free:
            continue  # more vessels than berths we know of in that zone
        berth = min(free, key=lambda b: math.dist((b["lon"], b["lat"]), (v["lon"], v["lat"])))
        occupied.add(berth["name"])
        berth["occupied_by"] = v["mmsi"]
        stay = service[v["category"]]
        # Arrival not seen (e.g. moored before the ingestion started): assume it was halfway
        # through its stay when we first saw it, instead of just arrived.
        # Known limit: a coin-flip guess; the upgrade is reading the arrival from the history.
        leaves = v["since"] + timedelta(minutes=stay if v.get("since_seen", True) else stay / 2)
        berth["free_from"] = max(0, int((leaves - now).total_seconds() // 60))
    ships = [{"mmsi": v["mmsi"], "length": v["length"], "category": v["category"],
              "arrival": 0, "duration": service[v["category"]]}
             for v in known if v["state"] == "anchored" and any(fits(v, b) for b in berths)]
    planned = {ship["mmsi"] for ship in ships}
    unfit = [v for v in vessels
             if v["state"] == "anchored" and v["category"] in COMMERCIAL and v["mmsi"] not in planned]
    return ships, berths, unfit


def plan_berths(ships, berths):
    """Assign each ship a berth and a start time, minimizing the total wait.

    ships: dicts with mmsi, length (m), category (cargo, tanker, passenger),
        arrival and duration (whole minutes from now).
    berths: dicts with name, length (m) and categories (list), as in data/berths.json,
        plus optional free_from (minutes from now until the berth is free, default 0).
    Returns one dict per ship (mmsi, berth, start, end, wait), sorted by start.
    """
    # Known limit: deterministic model from spec section 9. No tides, pilotage windows or
    # commercial priorities.
    if not ships:
        return []
    homeless = [str(v["mmsi"]) for v in ships if not any(fits(v, b) for b in berths)]
    if homeless:
        raise ValueError(f"no berth long enough and accepting the category for: {', '.join(homeless)}")

    model = cp_model.CpModel()
    # Worst case: every vessel queues on one berth after the last arrival or berth release.
    latest = max([v["arrival"] for v in ships] + [b.get("free_from", 0) for b in berths])
    horizon = latest + sum(v["duration"] for v in ships)

    starts, chosen = {}, {}
    per_berth = {b["name"]: [] for b in berths}
    for b in berths:
        if b.get("free_from", 0) > 0:  # the moored vessel, as a fixed block from minute 0
            per_berth[b["name"]].append(model.new_fixed_size_interval_var(0, b["free_from"], f"busy_{b['name']}"))
    for v in ships:
        start = model.new_int_var(v["arrival"], horizon, f"start_{v['mmsi']}")  # no start before arrival
        starts[v["mmsi"]] = start
        for b in berths:
            if not fits(v, b):
                continue  # only compatible berths get an interval
            here = model.new_bool_var(f"{v['mmsi']}_at_{b['name']}")
            interval = model.new_optional_fixed_size_interval_var(
                start, v["duration"], here, f"{v['mmsi']}_on_{b['name']}")
            chosen[v["mmsi"], b["name"]] = here
            per_berth[b["name"]].append(interval)
        model.add_exactly_one(here for (mmsi, _), here in chosen.items() if mmsi == v["mmsi"])

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
    for v in ships:
        start = solver.value(starts[v["mmsi"]])
        berth = next(b for (mmsi, b), here in chosen.items() if mmsi == v["mmsi"] and solver.value(here))
        plan.append({"mmsi": v["mmsi"], "berth": berth, "start": start,
                     "end": start + v["duration"], "wait": start - v["arrival"]})
    return sorted(plan, key=lambda p: p["start"])
