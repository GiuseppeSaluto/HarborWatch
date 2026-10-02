import copy
import json
from pathlib import Path

from ingest import parse

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
