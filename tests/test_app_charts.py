"""Tests for app_charts.map_view, the initial map view on a phone (spec section 8 point 9,
"Vista della mappa sul telefono (2026-11-10)"), and app_charts.credits, the data credits
line (section 8 point 13); see section 10.

No external services needed.
"""

from datetime import datetime, timedelta, timezone

import pytest

import app_charts
import config

# Real User-Agent strings as sent by the browsers.
IPHONE_SAFARI = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
                 "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
ANDROID_CHROME = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.6478.122 Mobile Safari/537.36")
WINDOWS_CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

PHONE_WIDTH_PX = 360


def visible_lon_span(zoom):
    """Degrees of longitude shown by a map PHONE_WIDTH_PX wide (Web Mercator, 512 px tiles)."""
    return 360 * PHONE_WIDTH_PX / (512 * 2 ** zoom)


def assert_centre_in_port(view):
    lats = [b["lat"] for b in config.BERTHS]
    assert min(lats) <= view["latitude"] <= max(lats), view


@pytest.mark.parametrize("user_agent", [IPHONE_SAFARI, ANDROID_CHROME], ids=["iphone", "android"])
def test_phone_view_fits_every_berth_in_360_px(user_agent):
    view = app_charts.map_view(user_agent)
    assert {"latitude", "longitude", "zoom"} <= set(view)
    assert_centre_in_port(view)
    half = visible_lon_span(view["zoom"]) / 2
    left, right = view["longitude"] - half, view["longitude"] + half
    outside = [b["name"] for b in config.BERTHS if not left <= b["lon"] <= right]
    assert outside == [], f"berths cut off at zoom {view['zoom']}: {outside}"


def test_non_phone_view_is_one_and_closer_than_phone():
    views = [app_charts.map_view(ua) for ua in (WINDOWS_CHROME, "", None)]
    for view in views:
        assert {"latitude", "longitude", "zoom"} <= set(view)
        assert_centre_in_port(view)
    zooms = {view["zoom"] for view in views}
    assert len(zooms) == 1, f"desktop, empty and missing User-Agent differ: {zooms}"
    assert zooms.pop() > app_charts.map_view(IPHONE_SAFARI)["zoom"]


# --- app_charts.credits (spec section 8 point 13 and section 13) -----------------------------

SINCE = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
OWA = "Open Waters AIS (https://openwaters.io/ais/)"
AISHUB = "AISHub (https://www.aishub.net)"


def source(_id, attribution, last_message=SINCE, feed=None):
    doc = {"_id": _id, "feed": feed or ("aisstream" if _id == "aisstream" else "openwaters"),
           "license": "x", "last_message": last_message}
    if attribution is not ...:
        doc["attribution"] = attribution
    return doc


def test_credits_counts_sources_heard_since_the_boundary():
    sources = [source("aisstream", "AISStream (https://aisstream.io)", SINCE),
               source("openwaters:aishub", f"{OWA}. {AISHUB}", SINCE - timedelta(seconds=1))]
    assert app_charts.credits(sources, SINCE) == "AISStream (https://aisstream.io)"


def test_credits_repeated_parts_appear_once():
    sources = [source("openwaters:aishub", f"{OWA}. {AISHUB}"),
               source("openwaters:station:ed25519:sFTMPfTZJVC0ka6a3tnOSP9ykFvW3YP5oSKZ9QdzUDo", OWA)]
    assert app_charts.credits(sources, SINCE) == f"{OWA} · {AISHUB}"
    # Parts are compared without a final dot, and shown without it.
    sources = [source("openwaters:a", f"{OWA}. aisstream.io."),
               source("openwaters:b", f"{OWA}. aisstream.io")]
    assert app_charts.credits(sources, SINCE) == f"{OWA} · aisstream.io"


def test_credits_order_is_aisstream_then_other_sources_by_id():
    # Input order differs from the expected one, so keeping the list order is caught.
    sources = [source("openwaters:station:ed25519:sFTMPfTZJVC0ka6a3tnOSP9ykFvW3YP5oSKZ9QdzUDo", OWA),
               source("openwaters:aisstream", f"{OWA}. aisstream.io"),
               source("aisstream", "AISStream (https://aisstream.io)"),
               source("openwaters:aishub", f"{OWA}. {AISHUB}")]
    assert app_charts.credits(sources, SINCE) == (
        f"AISStream (https://aisstream.io) · {OWA} · {AISHUB} · aisstream.io")


def test_credits_source_without_attribution_adds_nothing():
    # None too: from_openwaters gives attribution None when the event has none.
    for attribution in (..., "", None):
        sources = [source("openwaters:aishub", f"{OWA}. {AISHUB}"),
                   source("openwaters:other", attribution)]
        assert app_charts.credits(sources, SINCE) == f"{OWA} · {AISHUB}", attribution


def test_credits_is_empty_without_sources():
    assert app_charts.credits([], SINCE) == ""
    old = [source("aisstream", "AISStream (https://aisstream.io)", SINCE - timedelta(days=1))]
    assert app_charts.credits(old, SINCE) == ""
