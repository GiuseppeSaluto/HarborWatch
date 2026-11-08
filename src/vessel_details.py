"""Compatibility imports while vessel detail helpers move to their owning modules."""

from history import observations


def plan_reason(vessel, berths):
    """Keep the card's explanation while eligibility moves into the planner."""
    from optimize import plan_reason as exclusion_reason

    return exclusion_reason(vessel, berths) or "Eligible for the berth plan."
