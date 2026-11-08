from datetime import UTC, datetime, timedelta

import config
import vessel_details as details

NOW = datetime(2026, 11, 7, tzinfo=UTC)


def position(minutes, state, lon=8.85):
    return dict(ts=NOW + timedelta(minutes=minutes), state=state,
                location={"coordinates": [lon, 44.4]})


def test_observations_sort_and_report_changes_without_repeating_states():
    paths, events = details.observations([
        position(2, "at_berth", 8.87), position(0, "anchored"), position(1, "anchored", 8.86)])
    assert paths == [[[8.85, 44.4], [8.86, 44.4], [8.87, 44.4]]]
    assert [(e["state"], e["kind"], e["ts"]) for e in events] == [
        ("anchored", "First observed", NOW),
        ("at_berth", "State change observed", NOW + timedelta(minutes=2))]


def test_gap_breaks_route_and_does_not_claim_a_state_change():
    gap = config.STALE_MINUTES + 1
    paths, events = details.observations([
        position(0, "anchored"), position(gap, "at_berth"), position(gap + 1, "at_berth", 8.86)])
    assert paths == [[[8.85, 44.4], [8.86, 44.4]]]
    assert events[1]["kind"] == "Seen after data gap"


def test_empty_history():
    assert details.observations([]) == ([], [])


def test_missing_old_state_does_not_claim_an_observed_transition():
    _, events = details.observations([position(0, None), position(1, 'anchored')])
    assert events[1]['kind'] == 'First state observed'


def test_gap_at_threshold_remains_continuous():
    paths, events = details.observations([
        position(0, 'anchored'), position(config.STALE_MINUTES, 'underway', 8.86)])
    assert len(paths) == 1
    assert events[1]['kind'] == 'State change observed'


def test_observations_on_bundled_recording_preserve_data_gaps():
    import gzip
    import json
    from collections import defaultdict
    from pathlib import Path

    groups = defaultdict(list)
    path = Path(__file__).parents[1] / "data/sample/positions-2026-10-15.jsonl.gz"
    with gzip.open(path, "rt") as stream:
        for record in map(json.loads, stream):
            record["ts"] = datetime.fromisoformat(record["ts"].replace("Z", "+00:00"))
            record["location"] = {"coordinates": [record["lon"], record["lat"]]}
            groups[record["mmsi"]].append(record)
    for positions in groups.values():
        ordered = sorted(positions, key=lambda p: p["ts"])
        paths, events = details.observations(positions)
        gaps = sum(b["ts"] - a["ts"] > timedelta(minutes=config.STALE_MINUTES)
                   for a, b in zip(ordered, ordered[1:]))
        assert sum(e["kind"] == "Seen after data gap" for e in events) == gaps
        assert events[0]["kind"] == "First observed"
        assert all(len(path) > 1 for path in paths)
        assert all(e["ts"] in {p["ts"] for p in positions} for e in events)
