"""Connect to AISStream and write to MongoDB."""

import asyncio
import json
import logging
import math
import signal
import time
from datetime import UTC, datetime, timedelta

import websockets
from pymongo import ASCENDING, GEOSPHERE, AsyncMongoClient, UpdateOne
from pymongo.errors import PyMongoError

import config
from classify import classify, distance_to_coast_m

AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"

# Pause before reconnecting after a connection that worked and then dropped.
# Failed connection attempts already back off exponentially inside websockets.
RECONNECT_DELAY_S = 5

# A connection can stay open and go silent (AISStream did, 2026-10-05): with no message for
# this long, close it and reconnect instead of waiting forever. Genoa normally sends several
# messages per second.
SILENCE_S = 120

log = logging.getLogger("ingest")


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
            "ship_type": body.get("Type") or None,  # AIS ship type code, 0 = not available
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
    """Upsert the vessel state, keeping `since` while the state stays the same.

    since_seen tells whether we actually saw the state begin: true after a change from a
    recent different state, false when since only marks the first sighting (new vessel,
    or a gap over STALE_MINUTES such as an ingestion restart).
    """
    # Pipeline update: "$state", "$since", "$since_seen" and "$ts" are the stored values
    # before this update, so the check happens inside MongoDB and survives restarts.
    recent = {"$gte": ["$ts", fields["ts"] - timedelta(minutes=config.STALE_MINUTES)]}
    same = {"$and": [{"$eq": ["$state", fields["state"]]}, recent]}
    since = {"$cond": [same, "$since", fields["ts"]]}
    since_seen = {"$cond": [same, "$since_seen", recent]}
    return UpdateOne({"mmsi": fields["mmsi"]},
                     [{"$set": {**fields, "since": since, "since_seen": since_seen}}], upsert=True)


async def flush(db, positions, vessels, states):
    """Write the buffers to MongoDB in a few batched calls, emptying each one once written.

    If a write fails, the buffers not yet written are kept and retried at the next flush.
    """
    if positions:
        # ponytail: a partial failure here (rare: retryable writes cover network blips)
        # would re-insert the already written docs next time and hit duplicate _id errors.
        await db.positions.insert_many(positions, ordered=False)
        positions.clear()
    if vessels:
        await db.vessels.bulk_write(list(vessels.values()), ordered=False)
        vessels.clear()
    if states:
        # ordered: two updates for the same vessel must apply in arrival order.
        await db.states.bulk_write(states, ordered=True)
        states.clear()


async def ensure_indexes(db):
    """Create the indexes if missing; safe to call on every start."""
    await db.positions.create_index([("location", GEOSPHERE)])
    await db.positions.create_index([("mmsi", ASCENDING), ("ts", ASCENDING)])
    # TTL only works on a single-field index, hence ts appears twice.
    ttl = config.POSITIONS_TTL_DAYS * 86400
    current = (await db.positions.index_information()).get("ts_1")
    if current and current.get("expireAfterSeconds") != ttl:
        # create_index can't change options of an existing index (IndexOptionsConflict),
        # and collMod needs dbAdmin, which our readWrite Atlas user lacks: drop and rebuild.
        await db.positions.drop_index("ts_1")
        log.info("positions TTL changed to %d days", config.POSITIONS_TTL_DAYS)
    await db.positions.create_index("ts", expireAfterSeconds=ttl)
    await db.vessels.create_index("mmsi", unique=True)
    await db.states.create_index("mmsi", unique=True)


async def log_storage(db):
    """Log used storage and a projection of where the TTL will make it settle."""
    stats = await db.command("dbStats")
    used = stats["dataSize"] + stats["indexSize"]
    total = await db.positions.estimated_document_count()
    last_day = await db.positions.count_documents({"ts": {"$gte": datetime.now(UTC) - timedelta(days=1)}})
    # With the TTL, positions settle at about last_day * TTL days. Scaling the whole
    # database by that ratio overestimates at first (fixed index overhead), then converges.
    # During the first day last_day is incomplete, so the projection is still low.
    projected = used / total * last_day * config.POSITIONS_TTL_DAYS if total else used
    limit = config.STORAGE_LIMIT_MB * 1024 * 1024
    level = logging.WARNING if max(used, projected) > limit * config.STORAGE_WARN_RATIO else logging.INFO
    log.log(level, "storage %.1f MB of %d (%.0f%%), projected %.1f MB at %d days TTL; %d positions, %d in the last 24 h",
            used / 2**20, config.STORAGE_LIMIT_MB, 100 * used / limit,
            projected / 2**20, config.POSITIONS_TTL_DAYS, total, last_day)


async def safe_flush(db, positions, vessels, states):
    """flush(), but a MongoDB outage is logged instead of stopping the ingestion."""
    try:
        await flush(db, positions, vessels, states)
    except PyMongoError as exc:
        log.warning("flush failed, keeping %d positions in memory: %s", len(positions), exc)


async def main():
    client = AsyncMongoClient(config.MONGODB_URI)
    db = client[config.DB_NAME]
    await ensure_indexes(db)

    # systemd, docker and `timeout` stop a process with SIGTERM: treat it like Ctrl+C,
    # so the finally block below still writes the buffers. (Unix only.)
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)

    # Outside the reconnect loop: buffers and sampling survive a dropped connection.
    positions, vessels, states, last_saved = [], {}, [], {}
    last_flush = time.monotonic()
    last_stats = -math.inf  # log storage at the first flush
    try:
        # Iterating over connect() reconnects automatically on network errors.
        async for ws in websockets.connect(AISSTREAM_URL):
            try:
                await ws.send(json.dumps({
                    "APIKey": config.AISSTREAM_API_KEY,
                    "BoundingBoxes": [config.BOUNDING_BOX],  # a list of boxes
                    "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
                }))
                log.info("subscribed to AISStream")
                while True:
                    try:
                        async with asyncio.timeout(SILENCE_S):
                            message = await ws.recv()
                    except TimeoutError:
                        log.warning("no message for %d s, reconnecting", SILENCE_S)
                        await ws.close()
                        break
                    msg = json.loads(message)
                    if "error" in msg:  # e.g. a wrong API key; AISStream closes right after
                        log.error("AISStream: %s", msg["error"])
                    parsed = parse(msg)
                    if parsed:
                        collection, doc = parsed
                        if collection == "positions" and keep_position(doc, last_saved):
                            fields = state_fields(doc)
                            # The state in each position lets measure_stays read the history
                            # without classifying it again.
                            positions.append({**doc, "state": fields["state"]})
                            states.append(state_update(fields))
                        elif collection == "vessels":
                            # Dict keyed by mmsi: only the latest static data per vessel is written.
                            vessels[doc["mmsi"]] = UpdateOne({"mmsi": doc["mmsi"]}, {"$set": doc}, upsert=True)
                    # ponytail: flush is checked only when a message arrives; fine for a busy port.
                    # Awaiting it here keeps the buffers single-writer; incoming messages just
                    # queue in the websocket meanwhile.
                    if time.monotonic() - last_flush >= config.FLUSH_SECONDS:
                        await safe_flush(db, positions, vessels, states)
                        last_flush = time.monotonic()
                        if last_flush - last_stats >= config.STATS_SECONDS:
                            try:
                                await log_storage(db)
                            except PyMongoError as exc:
                                log.warning("storage check failed: %s", exc)
                            last_stats = last_flush
            except websockets.ConnectionClosed as exc:
                log.warning("connection closed (%s), reconnecting in %d s", exc, RECONNECT_DELAY_S)
            # Also reached when the server closes cleanly and the inner loop just ends.
            await asyncio.sleep(RECONNECT_DELAY_S)
    finally:
        # Ctrl+C or SIGTERM: write what is still in memory before exiting.
        log.info("shutting down, final flush")
        await safe_flush(db, positions, vessels, states)
        await client.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass  # Ctrl+C or SIGTERM: the final flush already ran, no traceback needed
