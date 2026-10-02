"""Connect to AISStream and write to MongoDB."""

import asyncio
import json
import time
from datetime import UTC, datetime

import websockets
from pymongo import ASCENDING, GEOSPHERE, MongoClient, UpdateOne

import config
from classify import classify, distance_to_coast_m

AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"


def parse(msg):
    """Validate an AIS message and return (collection, document), or None to discard it."""
    kind = msg.get("MessageType")
    body = msg.get("Message", {}).get(kind)
    mmsi = msg.get("MetaData", {}).get("MMSI")
    if not body or not isinstance(mmsi, int) or not 0 < mmsi <= 999_999_999:
        return None

    if kind == "PositionReport":
        lat, lon, sog = body.get("Latitude"), body.get("Longitude"), body.get("Sog")
        # AIS sentinels for "not available": lat 91, lon 181, sog 102.3.
        if lat is None or lon is None or sog is None:
            return None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 0 <= sog < 102.3):
            return None
        # time_utc looks like "2026-10-02 10:18:47.848845362 +0000 UTC": second precision is enough.
        ts = datetime.strptime(msg["MetaData"]["time_utc"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        return "positions", {
            "ts": ts,
            "mmsi": mmsi,
            "location": {"type": "Point", "coordinates": [lon, lat]},  # GeoJSON: [lon, lat]
            "sog": sog,
            "nav_status": body.get("NavigationalStatus"),
        }

    if kind == "ShipStaticData":
        # Dimension holds antenna distances to bow (A), stern (B), port (C), starboard (D); 0 = unknown.
        dim = body.get("Dimension", {})
        length = dim.get("A", 0) + dim.get("B", 0)
        beam = dim.get("C", 0) + dim.get("D", 0)
        return "vessels", {
            "mmsi": mmsi,
            "name": body.get("Name", "").strip() or None,
            "imo": body.get("ImoNumber") or None,
            "length": length or None,
            "beam": beam or None,
            "destination": body.get("Destination", "").strip() or None,
        }

    return None


def keep_position(doc, last_saved):
    """True if at least SAMPLE_SECONDS passed since this vessel's last kept position.

    last_saved maps mmsi -> ts of the last kept position and is updated in place.
    Uses the AIS timestamp, not the wall clock, so late or duplicate messages are dropped too.
    """
    last = last_saved.get(doc["mmsi"])
    if last is not None and (doc["ts"] - last).total_seconds() < config.SAMPLE_SECONDS:
        return False
    last_saved[doc["mmsi"]] = doc["ts"]
    return True


def state_fields(doc):
    """Current-state fields for a kept position: classification plus where and when."""
    lon, lat = doc["location"]["coordinates"]
    return {
        "mmsi": doc["mmsi"],
        "state": classify(doc["sog"], doc["nav_status"], distance_to_coast_m(lon, lat)),
        "ts": doc["ts"],
        "location": doc["location"],
        "sog": doc["sog"],
    }


def state_update(fields):
    """Upsert the vessel state, keeping `since` while the state stays the same."""
    # Pipeline update: "$state" and "$since" are the stored values before this update,
    # so the state change check happens inside MongoDB and survives restarts.
    since = {"$cond": [{"$eq": ["$state", fields["state"]]}, "$since", fields["ts"]]}
    return UpdateOne({"mmsi": fields["mmsi"]}, [{"$set": {**fields, "since": since}}], upsert=True)


def flush(db, positions, vessels, states):
    """Write the buffers to MongoDB in a few batched calls, then empty them."""
    if positions:
        db.positions.insert_many(positions, ordered=False)
    if vessels:
        db.vessels.bulk_write(list(vessels.values()), ordered=False)
    if states:
        # ordered: two updates for the same vessel must apply in arrival order.
        db.states.bulk_write(states, ordered=True)
    positions.clear()
    vessels.clear()
    states.clear()


def ensure_indexes(db):
    """Create the indexes if missing; safe to call on every start."""
    db.positions.create_index([("location", GEOSPHERE)])
    db.positions.create_index([("mmsi", ASCENDING), ("ts", ASCENDING)])
    # TTL only works on a single-field index, hence ts appears twice.
    db.positions.create_index("ts", expireAfterSeconds=config.POSITIONS_TTL_DAYS * 86400)
    db.vessels.create_index("mmsi", unique=True)
    db.states.create_index("mmsi", unique=True)


async def main():
    db = MongoClient(config.MONGODB_URI)[config.DB_NAME]
    ensure_indexes(db)

    async with websockets.connect(AISSTREAM_URL) as ws:
        await ws.send(json.dumps({
            "APIKey": config.AISSTREAM_API_KEY,
            "BoundingBoxes": [config.BOUNDING_BOX],  # a list of boxes
            "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
        }))
        positions, vessels, states, last_saved = [], {}, [], {}
        last_flush = time.monotonic()
        async for message in ws:
            parsed = parse(json.loads(message))
            if parsed:
                collection, doc = parsed
                if collection == "positions" and keep_position(doc, last_saved):
                    positions.append(doc)
                    states.append(state_update(state_fields(doc)))
                elif collection == "vessels":
                    # Dict keyed by mmsi: only the latest static data per vessel is written.
                    vessels[doc["mmsi"]] = UpdateOne({"mmsi": doc["mmsi"]}, {"$set": doc}, upsert=True)
            # ponytail: flush is checked only when a message arrives; fine for a busy port.
            # Sync PyMongo blocks the event loop for the write, a few hundred ms every
            # FLUSH_SECONDS. Upgrade path: asyncio.to_thread or PyMongo's async client.
            if time.monotonic() - last_flush >= config.FLUSH_SECONDS:
                flush(db, positions, vessels, states)
                last_flush = time.monotonic()


if __name__ == "__main__":
    asyncio.run(main())
