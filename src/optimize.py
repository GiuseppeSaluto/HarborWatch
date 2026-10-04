"""Phase 2: berth allocation optimizer (OR-Tools CP-SAT)."""

from ortools.sat.python import cp_model

# Hard cap on solving time; past it CP-SAT returns the best plan found so far.
# ponytail: ~10 vessels solve to optimality in milliseconds, 20+ hit the cap and return
# a good but unproven plan. Genoa has ~6-10 at anchor. Upgrade path if needed: a tighter
# horizon and symmetry breaking between berths of equal length.
SOLVER_SECONDS = 10


def plan_berths(vessels, berths):
    """Assign each vessel a berth and a start time, minimizing the total wait.

    vessels: dicts with name, length (m), category (cargo, tanker, passenger),
        arrival and service (whole minutes from now).
    berths: dicts with name, length (m) and accepts (list of categories), as in data/berths.json.
    Returns one dict per vessel (vessel, berth, start, end, wait), sorted by start.
    """
    # ponytail: deterministic model from spec section 9. No tides, pilotage windows,
    # commercial priorities, and berths are assumed free from minute 0; vessels already
    # at berth would need an "available from" per berth once the dashboard feeds real data.
    def fits(v, b):
        return b["length"] >= v["length"] and v["category"] in b["accepts"]

    homeless = [v["name"] for v in vessels if not any(fits(v, b) for b in berths)]
    if homeless:
        raise ValueError(f"no berth long enough and accepting the category for: {', '.join(homeless)}")

    model = cp_model.CpModel()
    # Worst case: every vessel queues on one berth after the last arrival.
    horizon = max(v["arrival"] for v in vessels) + sum(v["service"] for v in vessels)

    starts, chosen = {}, {}
    per_berth = {b["name"]: [] for b in berths}
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
