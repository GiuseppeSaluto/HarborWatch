import copy
import json
from datetime import timedelta
from pathlib import Path

import config
from ingest import keep_position, parse, state_fields

MOORED, ANCHORED, UNDERWAY, STATIC = json.loads((Path(__file__).parent / "sample_messages.json").read_text())


def test_position_report():
    collection, doc = parse(MOORED)
    assert collection == "positions"
    assert doc["mmsi"] == MOORED["MetaData"]["MMSI"]
    report = MOORED["Message"]["PositionReport"]
    assert doc["location"]["coordinates"] == [report["Longitude"], report["Latitude"]]
    assert doc["ts"].isoformat() == "2026-10-02T10:18:53+00:00"
    assert parse(ANCHORED)[1]["nav_status"] == 1
    assert parse(UNDERWAY)[1]["sog"] > 0.5


def test_ship_static_data():
    collection, doc = parse(STATIC)
    assert collection == "vessels"
    assert doc == {"mmsi": 319883000, "name": "ARIELA", "imo": 1010818,
                   "length": 55, "beam": 9, "destination": "GENOVA"}


def test_discards_invalid():
    for field, value in [("Latitude", 91), ("Longitude", 181), ("Sog", 102.3), ("Sog", None)]:
        msg = copy.deepcopy(MOORED)
        msg["Message"]["PositionReport"][field] = value
        assert parse(msg) is None, (field, value)
    msg = copy.deepcopy(MOORED)
    del msg["MetaData"]["MMSI"]
    assert parse(msg) is None
    assert parse({"MessageType": "SubscriptionConfirmation", "Message": {}}) is None


def test_keep_position_samples_per_vessel():
    last_saved = {}
    doc = parse(MOORED)[1]
    later = lambda seconds: {**doc, "ts": doc["ts"] + timedelta(seconds=seconds)}
    assert keep_position(doc, last_saved)
    assert not keep_position(doc, last_saved)  # duplicate
    assert not keep_position(later(config.SAMPLE_SECONDS - 1), last_saved)
    assert keep_position(later(config.SAMPLE_SECONDS), last_saved)
    assert keep_position({**doc, "mmsi": doc["mmsi"] + 1}, last_saved)  # other vessels are independent


def test_state_fields():
    assert state_fields(parse(MOORED)[1])["state"] == "at_berth"
    assert state_fields(parse(ANCHORED)[1])["state"] == "anchored"
    fields = state_fields(parse(UNDERWAY)[1])
    assert fields["state"] == "underway"
    assert set(fields) == {"mmsi", "state", "ts", "location", "sog"}
