"""Pure functions: vessel state (at_berth, anchored, underway) and ship category."""

import math

import config

MOORED = 5  # AIS navigational status "moored"


def distance_to_coast_m(lon, lat, coastline=config.COASTLINE):
    """Shortest distance in metres from a point to a GeoJSON MultiLineString."""
    # Local equirectangular projection: accurate to well under 1% across a port.
    kx, ky = 111_320 * math.cos(math.radians(lat)), 110_540
    best = math.inf
    # Known limit: linear scan over ~4k segments, a few ms per vessel. If it gets slow,
    # store the coastline in MongoDB and use $near on a 2dsphere index.
    for line in coastline["coordinates"]:
        for (ax, ay), (bx, by) in zip(line, line[1:]):
            ax, ay, bx, by = (ax - lon) * kx, (ay - lat) * ky, (bx - lon) * kx, (by - lat) * ky
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            t = 0 if seg2 == 0 else max(0, min(1, -(ax * dx + ay * dy) / seg2))
            best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def classify(sog, nav_status, dist_to_quay_m):
    # Known limit: instantaneous speed threshold, so a slow manoeuvring vessel can be
    # misclassified. Upgrade path: require the state to hold over a time window.
    # Speed wins over nav_status, which crews often leave stale ("moored" while leaving).
    if sog >= config.SPEED_THRESHOLD_KN:
        return "underway"
    # Known limit: distance from the OSM coastline stands in for berth geometries; measured
    # 2-45 m for moored vessels vs ~1 km at anchor. Real berths are needed in phase 2.
    if dist_to_quay_m <= config.QUAY_DISTANCE_M or nav_status == MOORED:
        return "at_berth"
    # Every message comes from the port bounding box, so slow and off the quay = anchorage.
    return "anchored"


# Categories that make up port congestion; the rest (tugs, pilots, yachts...) is port life.
COMMERCIAL = {"cargo", "tanker", "passenger"}


def ship_category(ship_type):
    """Group the AIS ship type code (ITU-R M.1371, first digit = family) into a category."""
    if not ship_type:
        return "unknown"  # no ShipStaticData received yet, or the vessel sends 0
    if 70 <= ship_type <= 79:
        return "cargo"
    if 80 <= ship_type <= 89:
        return "tanker"
    if 60 <= ship_type <= 69 or 40 <= ship_type <= 49:  # 40-49: high speed craft, mostly fast ferries
        return "passenger"
    if ship_type in (36, 37):  # sailing, pleasure craft: superyachts declare these
        return "pleasure"
    if 30 <= ship_type <= 35 or 50 <= ship_type <= 59:  # fishing, towing, pilot, tug, SAR, police...
        return "service"
    return "other"
