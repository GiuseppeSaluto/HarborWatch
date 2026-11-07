"""Observed vessel history and explanations of berth-plan eligibility."""

from datetime import timedelta

import config
from classify import COMMERCIAL
from optimize import fits


def observations(positions):
    """Build routes and state events from sampled AIS positions.

    A return after a gap is a new sighting, not evidence of when the state
    changed. A lone position has no route; the map still shows its marker.
    """
    paths, events, path = [], [], []
    previous = None
    for fix in sorted(positions, key=lambda p: p["ts"]):
        gap = previous is not None and fix["ts"] - previous["ts"] > timedelta(minutes=config.STALE_MINUTES)
        if gap:
            if len(path) > 1:
                paths.append(path)
            path = []
        path.append(fix["location"]["coordinates"])
        state, before = fix.get("state"), previous and previous.get("state")
        if previous is None:
            kind = "First observed"
        elif gap:
            kind = "Seen after data gap"
        elif state != before:
            kind = "First state observed" if before is None else "State change observed"
        else:
            kind = None
        if kind:
            events.append({"ts": fix["ts"], "state": state, "kind": kind})
        previous = fix
    if len(path) > 1:
        paths.append(path)
    return paths, events


def plan_reason(vessel, berths):
    """Use the planner's rules so the explanation agrees with the actual plan."""
    if vessel["category"] not in COMMERCIAL:
        return "Not planned: only commercial vessels are included in the berth plan."
    if vessel["state"] != "anchored":
        return "Not planned: only vessels currently at anchor need a berth assignment."
    if not vessel.get("length"):
        return "Not planned: vessel length unknown; compatibility cannot be checked."
    accepted = [b for b in berths if vessel["category"] in b["categories"]]
    if not accepted:
        return "Not planned: no known berth accepts this vessel category."
    if not any(fits(vessel, b) for b in accepted):
        return "Not planned: no known berth accepting this category is long enough. Berth lengths may be estimates."
    return "Eligible for the berth plan."
