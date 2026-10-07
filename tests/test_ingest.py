"""Black-box tests for ingest.parse (spec sections 3, 6 and 10).

Messages are built here in the AISStream envelope format, so the tests do not
need the network or MongoDB. parse is expected to be pure.
"""

import copy

import pytest

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


# Assumption: ingest.parse(msg) takes the decoded JSON dict of one AISStream
# message and returns either None (message discarded by validation) or the
# document to store. It may return the document directly or as a (kind, doc)
# pair; both shapes are accepted by `_doc`. The spec only says "validazione
# (parse)" in ingest.py.

def _parse(msg):
    try:
        return ingest.parse(copy.deepcopy(msg))
    except (ValueError, KeyError, TypeError):
        # Assumption: rejecting by raising is also acceptable validation.
        return None


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
