"""Tell which link of the chain is broken when data stop: Atlas, the ingestion or AISStream."""

import asyncio
import json
import time
from datetime import timedelta

import websockets

import config
from ingest import AISSTREAM_URL

WORLD = [[-90, -180], [90, 180]]

MESSAGES = {
    "atlas": "MongoDB Atlas is not answering: check the network and the Atlas IP access list.",
    "live": "Data are live.",
    "unknown": "Data are old. Run the stream check to find out why.",
    "access": "AISStream did not confirm the subscription: check the API key, the network or the service.",
    "ingestion": "AISStream sends data for the port but none are saved: the ingestion is stopped or stuck.",
    "coverage": "AISStream works elsewhere but has no data for this area: receiver coverage is missing.",
    "aisstream": "AISStream confirms the subscription but sends nothing worldwide: the service is down.",
}


def diagnose(atlas_ok, position_age, probe):
    """Return (code, message) for the first matching condition of spec section 8, point 6."""
    if not atlas_ok:
        code = "atlas"
    elif position_age is not None and position_age <= timedelta(minutes=config.LIVE_MINUTES):
        code = "live"
    elif probe is None:
        code = "unknown"
    elif not probe["confirmed"]:
        code = "access"
    elif probe["port"] > 0:
        code = "ingestion"
    elif probe["world"] > 0:
        code = "coverage"
    else:
        code = "aisstream"
    return code, MESSAGES[code]


def _in_port(lat, lon):
    (lat_min, lon_min), (lat_max, lon_max) = config.BOUNDING_BOX
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


async def probe_stream(seconds):
    """Listen to the whole world for `seconds` and count positions, in total and in the port box.

    One subscription covers both counts, since the world box contains the port.
    """
    result = {"confirmed": False, "world": 0, "port": 0, "seconds": seconds}
    deadline = time.monotonic() + seconds
    try:
        async with websockets.connect(AISSTREAM_URL, compression="deflate", max_size=None) as ws:
            await ws.send(json.dumps({
                "APIKey": config.AISSTREAM_API_KEY,
                "BoundingBoxes": [WORLD],
                "FilterMessageTypes": ["PositionReport"],
            }))
            while (left := deadline - time.monotonic()) > 0:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), left))
                except TimeoutError:
                    break
                if msg.get("MessageType") == "SubscriptionConfirmation":
                    result["confirmed"] = True
                    continue
                meta = msg.get("MetaData") or {}
                lat, lon = meta.get("latitude"), meta.get("longitude")
                if lat is None or lon is None:
                    continue
                result["world"] += 1
                result["port"] += _in_port(lat, lon)
    except (OSError, websockets.WebSocketException):
        pass  # network error or closed connection: return what was counted so far
    return result
