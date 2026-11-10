"""Connect to AISStream and Open Waters AIS and write to MongoDB."""

import asyncio
import json
import logging
import math
import signal
import time
from datetime import UTC, datetime, timedelta

import websockets
from pymongo import ASCENDING, GEOSPHERE, AsyncMongoClient, ReplaceOne, UpdateOne
from pymongo.errors import BulkWriteError, PyMongoError

import config
from classify import COMMERCIAL, classify, coast_distance_m, ship_category
from history import archive_since, congestion_pipeline, stays_pipeline, vessel_groups

AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"
# Open Waters' native stream: unlike its AISStream-compatible /v0, every event names its source.
OPENWATERS_URL = "wss://ais.openwaters.io/v1/stream"
FEEDS = ("aisstream", "openwaters")

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
    try:
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
            width = dim.get("C", 0) + dim.get("D", 0)
            return "vessels", {
                "mmsi": mmsi,
                "name": body.get("Name", "").strip() or None,
                "imo": body.get("ImoNumber") or None,
                "length": length or None,
                "width": width or None,
                "destination": body.get("Destination", "").strip() or None,
                "ship_type": body.get("Type") or None,  # AIS ship type code, 0 = not available
            }

        return None
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def from_openwaters(event):
    """An Open Waters /v1 event as the AISStream message parse() validates, plus its source info.

    Returns (msg, info), or None for other frames and events missing what parse needs.
    """
    try:
        if event.get("type") != "event" or any(event.get(k) is None for k in
                                                ("msg_type", "message", "mmsi", "time", "source")):
            return None
        ts = datetime.fromisoformat(event["time"])
        ts = ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)  # the stream sends UTC
    except (AttributeError, TypeError, ValueError):
        return None
    kind = event["msg_type"]
    msg = {"MessageType": kind, "Message": {kind: event["message"]},
           "MetaData": {"MMSI": event["mmsi"], "time_utc": f"{ts:%Y-%m-%d %H:%M:%S} +0000 UTC"}}
    return msg, {"source": f"openwaters:{event['source']}", "license": event.get("license"),
                 "attribution": event.get("attribution")}


def hour_stats(hour, counts):
    """The source_stats documents of one hour: one per feed, with the vessels only it heard."""
    empty = {"messages": 0, "kept": 0, "mmsis": set()}
    a, b = (counts.get(feed, empty) for feed in FEEDS)
    return [{"_id": {"hour": hour, "feed": feed}, "messages": c["messages"], "kept": c["kept"],
             "vessels": len(c["mmsis"]), "only": len(c["mmsis"] - other["mmsis"])}
            for feed, c, other in ((FEEDS[0], a, b), (FEEDS[1], b, a))]


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
        "state": classify(doc["sog"], doc["nav_status"], coast_distance_m(lon, lat)),
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
        try:
            await db.positions.insert_many(positions, ordered=False)
        except BulkWriteError as exc:
            # A retry of a write whose reply got lost: insert_many set the _ids in place, so the
            # docs already stored come back as duplicates. Without this the buffer never empties.
            if exc.details.get("writeConcernErrors") or any(
                    e["code"] != 11000 for e in exc.details["writeErrors"]):
                raise
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
    # $merge into stays matches on (mmsi, start), which needs a unique index on those fields.
    await db.stays.create_index([("mmsi", ASCENDING), ("start", ASCENDING)], unique=True)


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


async def archive(db, now):
    """Copy complete stays and hourly congestion to their permanent collections.

    The positions they are computed from expire after POSITIONS_TTL_DAYS; the copies stay.
    """
    counts = await (await db.positions.aggregate([
        {"$match": {"state": {"$exists": True}}}, {"$group": {"_id": "$mmsi", "n": {"$sum": 1}}}])).to_list()
    # A stay found again is the same stay: keep the archived copy.
    into_stays = {"$merge": {"into": "stays", "on": ["mmsi", "start"],
                             "whenMatched": "keepExisting", "whenNotMatched": "insert"}}
    for group in vessel_groups(counts, config.STAY_BATCH_POSITIONS):
        await (await db.positions.aggregate([{"$match": {"mmsi": {"$in": group}}},
                                             *stays_pipeline(config.STALE_MINUTES), into_stays])).to_list()
    commercial = [v["mmsi"] async for v in db.vessels.find({}, {"mmsi": 1, "ship_type": 1})
                  if ship_category(v.get("ship_type")) in COMMERCIAL]
    # Only whole hours still in the TTL window: the current one is rewritten until it ends.
    await (await db.positions.aggregate([*congestion_pipeline(archive_since(now), commercial),
                                         {"$merge": {"into": "congestion_hourly", "whenMatched": "replace"}}])).to_list()


async def safe_flush(db, positions, vessels, states):
    """flush(), but a MongoDB outage is logged instead of stopping the ingestion."""
    try:
        await flush(db, positions, vessels, states)
    except PyMongoError as exc:
        log.warning("flush failed, keeping %d positions in memory: %s", len(positions), exc)


AISSTREAM_INFO = {"source": "aisstream", "license": "aisstream-io-terms",
                  "attribution": "AISStream (https://aisstream.io)"}


def from_aisstream(frame):
    """An AISStream frame as (msg, info) for the queue, or None for an error frame."""
    if isinstance(frame, dict) and "error" in frame:
        log.error("AISStream: %s", frame["error"])
        return None
    return frame, AISSTREAM_INFO


def from_openwaters_frame(frame):
    """An Open Waters frame as (msg, info) for the queue: welcome and error frames are logged."""
    kind = frame.get("type") if isinstance(frame, dict) else None
    if kind == "welcome":
        log.info("Open Waters limits: %s", frame.get("limits"))
    elif kind == "error":
        log.error("Open Waters: %s", frame)
    return from_openwaters(frame)


async def read_feed(name, url, subscription, unwrap, queue, headers=None):
    """Read one AIS feed forever, putting (msg, info) on the queue; reconnect when it drops."""
    # Iterating over connect() reconnects automatically on network errors.
    async for ws in (websockets.connect(url, additional_headers=headers) if headers else websockets.connect(url)):
        try:
            await ws.send(json.dumps(subscription))
            log.info("subscribed to %s", name)
            while True:
                try:
                    async with asyncio.timeout(SILENCE_S):
                        message = await ws.recv()
                except TimeoutError:
                    log.warning("%s: no message for %d s, reconnecting", name, SILENCE_S)
                    await ws.close()
                    break
                try:
                    item = unwrap(json.loads(message))
                except json.JSONDecodeError:
                    log.warning("invalid %s JSON, skipping message", name)
                    continue
                if item:
                    await queue.put(item)
        except websockets.ConnectionClosed as exc:
            log.warning("%s: connection closed (%s), reconnecting in %d s", name, exc, RECONNECT_DELAY_S)
        # Also reached when the server closes cleanly and the inner loop just ends.
        await asyncio.sleep(RECONNECT_DELAY_S)


async def write_sources(db, sources, hour, counts):
    """Each source's credits and last message, and the finished hour's per-feed stats if any."""
    if sources:
        await db.sources.bulk_write([UpdateOne({"_id": k}, {"$set": v}, upsert=True) for k, v in sources.items()])
    if hour is not None:
        await db.source_stats.bulk_write([ReplaceOne({"_id": d["_id"]}, d, upsert=True)
                                          for d in hour_stats(hour, counts)])


async def consume(db, queue):
    """The only writer: sampling, states and flushes for the messages of every feed."""
    # Outside the readers: buffers and sampling survive a dropped connection, and one
    # last_saved for all feeds keeps one position per vessel per minute, the first to arrive.
    positions, vessels, states, last_saved = [], {}, [], {}
    sources, counts = {}, {}
    hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    last_flush = time.monotonic()
    last_stats = last_archive = -math.inf  # log storage and archive at the first flush
    try:
        while True:
            item = await queue.get()
            if item is None:  # main is shutting down: everything queued before is handled
                break
            msg, info = item
            feed = info["source"].split(":")[0]
            now = datetime.now(UTC)
            sources[info["source"]] = {"feed": feed, "license": info["license"],
                                       "attribution": info["attribution"], "last_message": now}
            if now - hour >= timedelta(hours=1):
                # Known limit: an ingestion restart mid-hour rewrites that hour with the
                # counts since the restart only (vessel sets cannot be added up).
                try:
                    await write_sources(db, {}, hour, counts)
                except PyMongoError as exc:
                    log.warning("source stats of %s lost: %s", hour, exc)
                hour, counts = now.replace(minute=0, second=0, microsecond=0), {}
            parsed = parse(msg)
            if parsed:
                collection, doc = parsed
                if collection == "positions":
                    c = counts.setdefault(feed, {"messages": 0, "kept": 0, "mmsis": set()})
                    c["messages"] += 1
                    c["mmsis"].add(doc["mmsi"])
                    if keep_position(doc, last_saved):
                        c["kept"] += 1
                        fields = state_fields(doc)
                        # The state in each position lets measure_stays read the history
                        # without classifying it again.
                        positions.append({**doc, "state": fields["state"], "source": info["source"]})
                        states.append(state_update(fields))
                else:
                    # Dict keyed by mmsi: only the latest static data per vessel is written.
                    vessels[doc["mmsi"]] = UpdateOne({"mmsi": doc["mmsi"]}, {"$set": doc}, upsert=True)
            # Known limit: flush is checked only when a message arrives; fine for a busy port.
            if time.monotonic() - last_flush >= config.FLUSH_SECONDS:
                await safe_flush(db, positions, vessels, states)
                try:
                    await write_sources(db, sources, None, counts)
                except PyMongoError as exc:
                    log.warning("sources update failed: %s", exc)
                last_flush = time.monotonic()
                if last_flush - last_stats >= config.STATS_SECONDS:
                    try:
                        await log_storage(db)
                    except PyMongoError as exc:
                        log.warning("storage check failed: %s", exc)
                    last_stats = last_flush
                if last_flush - last_archive >= config.ARCHIVE_MINUTES * 60:
                    try:
                        await archive(db, datetime.now(UTC))
                    except PyMongoError as exc:
                        log.warning("archive failed, retrying in %d min: %s", config.ARCHIVE_MINUTES, exc)
                    last_archive = last_flush
    finally:
        # Ctrl+C or SIGTERM: write what is still in memory before exiting.
        log.info("shutting down, final flush")
        await safe_flush(db, positions, vessels, states)
        try:
            await write_sources(db, sources, hour, counts)
        except PyMongoError as exc:
            log.warning("final sources update failed: %s", exc)


async def main():
    client = AsyncMongoClient(config.MONGODB_URI)
    db = client[config.DB_NAME]
    await ensure_indexes(db)

    # systemd, docker and `timeout` stop a process with SIGTERM: treat it like Ctrl+C,
    # so the consumer's finally block still writes the buffers. (Unix only.)
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)

    # The feeds only read and queue; one consumer writes, so a flush waiting on Atlas
    # never sees a buffer grow under it and clear() drop positions it did not write.
    queue = asyncio.Queue()
    (lat0, lon0), (lat1, lon1) = config.BOUNDING_BOX
    consumer = asyncio.create_task(consume(db, queue))
    readers = [asyncio.create_task(read_feed("AISStream", AISSTREAM_URL, {
        "APIKey": config.AISSTREAM_API_KEY,
        "BoundingBoxes": [config.BOUNDING_BOX],  # a list of boxes
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    }, from_aisstream, queue))]
    if config.OPENWATERS_TOKEN:
        readers.append(asyncio.create_task(read_feed("Open Waters", OPENWATERS_URL, {
            "type": "subscribe", "bbox": [[min(lat0, lat1), min(lon0, lon1), max(lat0, lat1), max(lon0, lon1)]],
        }, from_openwaters_frame, queue, {"Authorization": f"Bearer {config.OPENWATERS_TOKEN}"})))
    try:
        # None of them ends on its own: whichever does (an error, a cancel) stops the ingestion.
        done, _ = await asyncio.wait([consumer, *readers], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()  # re-raise why it stopped
    finally:
        # Also on SIGTERM or Ctrl+C: stop reading, let the consumer write what is queued, then close.
        for task in readers:
            task.cancel()
        queue.put_nowait(None)
        (result,) = await asyncio.gather(consumer, return_exceptions=True)
        if isinstance(result, Exception):  # its last writes failed: say so before exiting
            log.error("consumer stopped with %r", result)
        await client.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass  # Ctrl+C or SIGTERM: the final flush already ran, no traceback needed
