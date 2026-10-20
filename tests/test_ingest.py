"""Black-box tests for ingest.parse, flush and keep_position (spec sections 3, 6, 7, 10 and 13).

Messages are built here in the AISStream envelope format and MongoDB is a hand-written
fake, so the tests need neither the network nor a database. parse is expected to be pure.
"""

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from pymongo.errors import AutoReconnect, BulkWriteError

import classify
import config
import ingest

MMSI = 247123450
LAT, LON = 44.40, 8.90


def position_msg(mmsi=MMSI, lat=LAT, lon=LON, sog=0.2, nav_status=5):
    """AISStream PositionReport envelope; coordinates/MMSI duplicated in MetaData
    and Message like the real stream does."""
    meta = {"MMSI": mmsi, "ShipName": "TEST VESSEL", "latitude": lat,
            "longitude": lon, "time_utc": "2026-10-04 10:00:00.123456789 +0000 UTC"}
    report = {"UserID": mmsi, "Latitude": lat, "Longitude": lon, "Sog": sog,
              "Cog": 180.0, "TrueHeading": 180, "NavigationalStatus": nav_status,
              "Valid": True, "MessageID": 1, "RepeatIndicator": 0,
              "Timestamp": 0, "RateOfTurn": 0, "PositionAccuracy": True,
              "Raim": False, "CommunicationState": 0, "SpecialManoeuvreIndicator": 0,
              "Spare": 0}
    if mmsi is None:
        del meta["MMSI"]
        del report["UserID"]
    return {"MessageType": "PositionReport", "MetaData": meta,
            "Message": {"PositionReport": report}}


def static_msg(mmsi=MMSI, ship_type=70):
    meta = {"MMSI": mmsi, "ShipName": "TEST VESSEL", "latitude": LAT,
            "longitude": LON, "time_utc": "2026-10-04 10:00:00.123456789 +0000 UTC"}
    data = {"UserID": mmsi, "Name": "TEST VESSEL", "ImoNumber": 9123456,
            "CallSign": "ABCD", "Type": ship_type, "Destination": "GENOA",
            "Dimension": {"A": 150, "B": 50, "C": 16, "D": 16},
            "MaximumStaticDraught": 9.5, "Valid": True, "MessageID": 5,
            "Eta": {"Day": 4, "Hour": 12, "Minute": 0, "Month": 10}}
    return {"MessageType": "ShipStaticData", "MetaData": meta,
            "Message": {"ShipStaticData": data}}


# Interface (spec section 13): ingest.parse(msg) takes the decoded JSON dict of
# one AISStream message and returns (collection, doc), or None when validation
# discards the message.

def _parse(msg):
    # No exception allowed: parse runs on every stream message, and a raise would stop the ingestion.
    return ingest.parse(copy.deepcopy(msg))


def _doc(result):
    if isinstance(result, (tuple, list)) and len(result) == 2 and isinstance(result[0], str):
        return result[1]
    return result


# --- valid position -----------------------------------------------------------------

def test_valid_position_is_accepted():
    assert _doc(_parse(position_msg())) is not None


def test_position_has_mmsi():
    assert _doc(_parse(position_msg()))["mmsi"] == MMSI


def test_position_location_is_geojson_point_lon_lat():
    # Spec: `location` is a GeoJSON Point; GeoJSON order is [lon, lat].
    loc = _doc(_parse(position_msg()))["location"]
    assert loc["type"] == "Point"
    assert loc["coordinates"] == pytest.approx([LON, LAT])


def test_position_has_sog_and_nav_status():
    doc = _doc(_parse(position_msg(sog=3.4, nav_status=0)))
    assert doc["sog"] == pytest.approx(3.4)
    assert doc["nav_status"] == 0


def test_position_has_timestamp():
    assert _doc(_parse(position_msg())).get("ts") is not None


def test_zero_speed_is_valid():
    assert _doc(_parse(position_msg(sog=0.0))) is not None


def test_coordinate_extremes_are_valid():
    assert _doc(_parse(position_msg(lat=90.0, lon=180.0))) is not None
    assert _doc(_parse(position_msg(lat=-90.0, lon=-180.0))) is not None


# --- rejected positions -------------------------------------------------------------

def test_lat_sentinel_91_is_rejected():
    assert _doc(_parse(position_msg(lat=91.0))) is None


def test_lon_sentinel_181_is_rejected():
    assert _doc(_parse(position_msg(lon=181.0))) is None


@pytest.mark.parametrize("lat,lon", [(-90.5, LON), (LAT, -180.5), (95.0, LON), (LAT, 200.0)])
def test_out_of_range_coordinates_are_rejected(lat, lon):
    assert _doc(_parse(position_msg(lat=lat, lon=lon))) is None


@pytest.mark.parametrize("lat,lon", [(None, LON), (LAT, None), (None, None)])
def test_null_coordinates_are_rejected(lat, lon):
    assert _doc(_parse(position_msg(lat=lat, lon=lon))) is None


def test_missing_mmsi_is_rejected():
    assert _doc(_parse(position_msg(mmsi=None))) is None


def test_sog_sentinel_102_3_is_rejected():
    # AIS: 1023 raw = 102.3 kn means "speed not available".
    assert _doc(_parse(position_msg(sog=102.3))) is None


def test_negative_sog_is_rejected():
    assert _doc(_parse(position_msg(sog=-1.0))) is None


def test_null_sog_is_rejected():
    assert _doc(_parse(position_msg(sog=None))) is None


# --- static data ---------------------------------------------------------------------
# Assumption: the static document uses the field names "mmsi", "name", "imo",
# "length", "width", "destination", "ship_type" (spec section 6 lists "nome, IMO,
# lunghezza, larghezza, destinazione, ship_type"); length = Dimension A + B and
# width = C + D as in the AIS standard.


def test_static_data_is_accepted():
    assert _doc(_parse(static_msg())) is not None


def test_static_data_fields():
    doc = _doc(_parse(static_msg(ship_type=80)))
    assert doc["mmsi"] == MMSI
    assert doc["ship_type"] == 80
    assert doc["imo"] == 9123456
    assert doc["destination"] == "GENOA"
    assert doc["name"] == "TEST VESSEL"


def test_static_length_is_bow_plus_stern():
    assert _doc(_parse(static_msg()))["length"] == 200


def test_static_width_is_port_plus_starboard():
    assert _doc(_parse(static_msg()))["width"] == 32


def test_static_missing_dimension_parts_count_as_zero_and_zero_sum_is_none():
    # Spec section 13: a part missing from Dimension is 0, also with no Dimension
    # key at all; a sum of 0 is an unknown size (None), not 0.
    cases = [
        ({"B": 50, "C": 8, "D": 8}, 50, 16),        # spec example: A missing
        ({"A": 120, "D": 9}, 120, 9),                # B and C missing
        ({"A": 0, "B": 0, "C": 8, "D": 8}, None, 16),
        ({}, None, None),
        (None, None, None),                          # no Dimension key
    ]
    for dimension, length, width in cases:
        msg = static_msg()
        if dimension is None:
            del msg["Message"]["ShipStaticData"]["Dimension"]
        else:
            msg["Message"]["ShipStaticData"]["Dimension"] = dimension
        doc = _doc(_parse(msg))
        assert doc is not None, dimension
        assert (doc["length"], doc["width"]) == (length, width), dimension


def test_static_without_mmsi_is_rejected():
    msg = static_msg()
    del msg["MetaData"]["MMSI"]
    del msg["Message"]["ShipStaticData"]["UserID"]
    assert _doc(_parse(msg)) is None


def test_position_and_static_are_distinguishable():
    # The two message kinds go to different collections (positions, vessels):
    # the parsed position must carry a location, the static record a ship_type.
    pos = _doc(_parse(position_msg()))
    sta = _doc(_parse(static_msg()))
    assert "location" in pos
    assert "ship_type" in sta and "location" not in sta


def test_position_goes_to_positions_collection():
    # Assumption: the collection element of the pair is the MongoDB collection
    # name from spec section 6.
    collection, _ = ingest.parse(position_msg())
    assert collection == "positions"


def test_static_data_goes_to_vessels_collection():
    collection, _ = ingest.parse(static_msg())
    assert collection == "vessels"


# --- MMSI range (spec section 13): integer from 1 to 999 999 999 inclusive ------------------

def test_mmsi_range_edges_are_accepted():
    for mmsi in (1, 999_999_999):
        for build in (position_msg, static_msg):
            doc = _doc(_parse(build(mmsi=mmsi)))
            assert doc is not None, (build.__name__, mmsi)
            assert doc["mmsi"] == mmsi, (build.__name__, mmsi)


def test_mmsi_out_of_range_or_not_integer_is_rejected():
    for mmsi in (0, -1, 1_000_000_000, 247123450.5, "247123450"):
        for build in (position_msg, static_msg):
            assert _parse(build(mmsi=mmsi)) is None, (build.__name__, mmsi)


@pytest.mark.parametrize("msg", [None, [], "invalid", {"Message": None},
                                {"MessageType": []}, {"MetaData": None}])
def test_malformed_envelope_is_rejected(msg):
    assert ingest.parse(msg) is None


@pytest.mark.parametrize("value", [None, 123, "", "invalid", "2026-02-30 10:00:00"])
def test_invalid_timestamp_is_rejected(value):
    msg = position_msg()
    msg["MetaData"]["time_utc"] = value
    assert ingest.parse(msg) is None


def test_missing_timestamp_is_rejected():
    msg = position_msg()
    del msg["MetaData"]["time_utc"]
    assert ingest.parse(msg) is None


@pytest.mark.parametrize("field", ["Latitude", "Longitude", "Sog"])
def test_non_numeric_position_is_rejected(field):
    msg = position_msg()
    msg["Message"]["PositionReport"][field] = "invalid"
    assert ingest.parse(msg) is None


@pytest.mark.parametrize("field,value", [("Dimension", None), ("Dimension", {"A": "invalid"}),
                                        ("Name", None), ("Destination", 123)])
def test_malformed_static_data_is_rejected(field, value):
    msg = static_msg()
    msg["Message"]["ShipStaticData"][field] = value
    assert ingest.parse(msg) is None


def test_ingestion_continues_after_malformed_messages(monkeypatch):
    db = FakeDb()
    client = MagicMock()
    client.__getitem__.return_value = db
    client.close = AsyncMock()
    ws = AsyncMock()
    ws.recv.side_effect = ["invalid json", "null", json.dumps(position_msg(lat="invalid")),
                           json.dumps(position_msg()), asyncio.CancelledError()]

    async def connections(*args):
        yield ws

    monkeypatch.setattr(ingest, "AsyncMongoClient", lambda uri: client)
    monkeypatch.setattr(ingest.websockets, "connect", connections)
    monkeypatch.setattr(ingest, "ensure_indexes", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ingest.main())
    stored = db.log[0][2]
    assert len(stored) == 1
    assert stored[0]["mmsi"] == MMSI
    client.close.assert_awaited_once()


# --- flush(db, positions, vessels, states) --------------------------------------------------
# Spec section 7 point 1 and section 13. The db is a hand-written fake: each
# collection records its calls in one shared log and can be told to raise.
# Assumption: flush awaits the collection methods (pymongo AsyncMongoClient), so
# the fake methods are coroutines; flush is run with asyncio.run.
# Assumption: the fake records `ordered` whether it is passed by keyword or not.


class FakeCollection:
    def __init__(self, name, log):
        self.name, self.log, self.error = name, log, None

    async def insert_many(self, documents, ordered=True, **kwargs):
        self.log.append((self.name, "insert_many", list(documents), ordered))
        if self.error is not None:
            raise self.error

    async def bulk_write(self, requests, ordered=True, **kwargs):
        self.log.append((self.name, "bulk_write", list(requests), ordered))
        if self.error is not None:
            raise self.error


class FakeDb:
    def __init__(self):
        self.log = []
        self.positions = FakeCollection("positions", self.log)
        self.vessels = FakeCollection("vessels", self.log)
        self.states = FakeCollection("states", self.log)

    def __getitem__(self, name):
        return getattr(self, name)


def buffers():
    positions = [{"mmsi": 1, "n": 1}, {"mmsi": 2, "n": 2}]
    vessels = {1: "vessel-op-1", 2: "vessel-op-2"}
    states = ["state-op-1", "state-op-2"]
    return positions, vessels, states


def bulk_error(codes, write_concern_errors=()):
    return BulkWriteError({
        "writeErrors": [{"index": i, "code": c, "errmsg": "error"} for i, c in enumerate(codes)],
        "writeConcernErrors": list(write_concern_errors),
        "nInserted": 0,
    })


def flush(db, positions, vessels, states):
    asyncio.run(ingest.flush(db, positions, vessels, states))


def test_flush_writes_the_three_buffers_in_order_and_clears_them():
    db = FakeDb()
    positions, vessels, states = buffers()
    expected = [
        ("positions", "insert_many", list(positions), False),
        ("vessels", "bulk_write", list(vessels.values()), False),
        ("states", "bulk_write", list(states), True),
    ]
    flush(db, positions, vessels, states)
    assert db.log == expected
    assert positions == [] and vessels == {} and states == []


def test_flush_skips_empty_buffers():
    # Each buffer empty in turn: pymongo raises on an empty insert_many or bulk_write.
    order = ["positions", "vessels", "states"]
    for empty in order:
        db = FakeDb()
        positions, vessels, states = buffers()
        {"positions": positions, "vessels": vessels, "states": states}[empty].clear()
        flush(db, positions, vessels, states)
        assert [name for name, _, _, _ in db.log] == [n for n in order if n != empty], empty


def test_flush_counts_duplicate_only_insert_errors_as_written():
    # Atlas saved the positions but the reply was lost: the retry gets only
    # 11000 duplicates. The buffer is emptied and vessels and states still go out.
    db = FakeDb()
    db.positions.error = bulk_error([11000, 11000])
    positions, vessels, states = buffers()
    flush(db, positions, vessels, states)
    assert positions == [] and vessels == {} and states == []
    assert [name for name, _, _, _ in db.log] == ["positions", "vessels", "states"]


def test_flush_any_other_error_keeps_the_buffer_and_stops():
    cases = [
        # (failing collection, error) -- each one must propagate.
        ("positions", bulk_error([11000, 121])),                       # not only duplicates
        ("positions", bulk_error([11000], [{"code": 64, "errmsg": "wc"}])),  # write concern error
        ("positions", AutoReconnect("connection lost")),
        ("vessels", bulk_error([11000])),            # the duplicate rule is for insert_many only
        ("states", AutoReconnect("connection lost")),
    ]
    order = ["positions", "vessels", "states"]
    for failing, error in cases:
        db = FakeDb()
        getattr(db, failing).error = error
        positions, vessels, states = buffers()
        original = {"positions": list(positions), "vessels": dict(vessels), "states": list(states)}
        current = {"positions": positions, "vessels": vessels, "states": states}
        with pytest.raises(type(error)):
            flush(db, positions, vessels, states)
        idx = order.index(failing)
        for name in order[:idx]:
            assert not current[name], (failing, name)                # written before: cleared
        assert current[failing] == original[failing], failing       # failed: intact
        for name in order[idx + 1:]:
            assert current[name] == original[name], (failing, name)  # never touched
        assert [n for n, _, _, _ in db.log] == order[:idx + 1], failing


# --- keep_position(doc, last_saved) ---------------------------------------------------------
# Spec section 7 point 2 and section 13. Timestamps are years in the past: the
# rule runs on the AIS time in doc["ts"], not on the wall clock.

T_AIS = datetime(2020, 3, 1, 8, 0, tzinfo=timezone.utc)


def sample():
    return timedelta(seconds=config.SAMPLE_SECONDS)


def pos_doc(mmsi, ts):
    return {"mmsi": mmsi, "ts": ts, "sog": 0.0, "nav_status": 0,
            "location": {"type": "Point", "coordinates": [LON, LAT]}}


def test_keep_position_keeps_first_position_of_a_vessel():
    last_saved = {}
    assert ingest.keep_position(pos_doc(1, T_AIS), last_saved) is True
    assert last_saved == {1: T_AIS}


def test_keep_position_spacing_is_measured_from_last_kept_position():
    last_saved = {1: T_AIS}
    almost = T_AIS + sample() - timedelta(seconds=1)
    assert ingest.keep_position(pos_doc(1, almost), last_saved) is False
    assert last_saved == {1: T_AIS}
    # One second after the dropped fix, but SAMPLE_SECONDS after the kept one.
    assert ingest.keep_position(pos_doc(1, T_AIS + sample()), last_saved) is True
    assert last_saved == {1: T_AIS + sample()}


def test_keep_position_drops_late_or_repeated_messages():
    for ts in (T_AIS, T_AIS - timedelta(seconds=1), T_AIS - 3 * sample()):
        last_saved = {1: T_AIS}
        assert ingest.keep_position(pos_doc(1, ts), last_saved) is False, ts
        assert last_saved == {1: T_AIS}, ts


def test_keep_position_vessels_do_not_affect_each_other():
    last_saved = {1: T_AIS}
    soon = T_AIS + timedelta(seconds=1)
    assert ingest.keep_position(pos_doc(2, soon), last_saved) is True
    assert ingest.keep_position(pos_doc(1, soon), last_saved) is False
    assert last_saved == {1: T_AIS, 2: soon}


# --- state_fields(doc) (spec section 13) -----------------------------------------------------
# Input is a position already validated by parse. Expected states are computed
# with the classify functions exactly as the spec writes them:
# classify(sog, nav_status, coast_distance_m(lon, lat)).

SEA = (8.85, 44.34)       # open sea south of Genoa, kilometres from the coast


def parsed_position(lat, lon, sog, nav_status):
    return _doc(_parse(position_msg(lat=lat, lon=lon, sog=sog, nav_status=nav_status)))


def test_state_fields_copies_the_position_fields():
    doc = parsed_position(LAT, LON, 3.4, 0)
    fields = ingest.state_fields(doc)
    for key in ("mmsi", "ts", "location", "sog"):
        assert fields[key] == doc[key], key
    # Literal reading of section 13: those four fields plus state, nothing else.
    assert set(fields) == {"mmsi", "ts", "location", "sog", "state"}


def test_state_fields_state_is_classify_on_the_document_position():
    berth = config.BERTHS[0]
    cases = [
        (SEA[1], SEA[0], 5.0, 0),               # moving: speed decides
        (SEA[1], SEA[0], 0.0, 5),               # "moored" status far from the coast
        (SEA[1], SEA[0], 0.0, 0),               # stopped at sea
        (berth["lat"], berth["lon"], 0.0, 0),   # stopped at a berth point: lon/lat order matters
    ]
    seen = set()
    for lat, lon, sog, nav in cases:
        doc = parsed_position(lat, lon, sog, nav)
        expected = classify.classify(sog, nav, classify.coast_distance_m(lon, lat))
        assert ingest.state_fields(doc)["state"] == expected, (lat, lon, sog, nav)
        seen.add(expected)
    # The cases must really exercise different states, or the test proves little.
    assert {"underway", "at_berth", "anchored"} <= seen
