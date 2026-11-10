"""Tests for app_charts.map_view: the initial map view on a phone (spec section 8 point 9,
"Vista della mappa sul telefono (2026-11-10)", and section 10).

No external services needed.
"""

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


@pytest.mark.parametrize("user_agent", [IPHONE_SAFARI, ANDROID_CHROME], ids=["iphone", "android"])
def test_phone_view_fits_every_berth_in_360_px(user_agent):
    view = app_charts.map_view(user_agent)
    assert {"latitude", "longitude", "zoom"} <= set(view)
    half = visible_lon_span(view["zoom"]) / 2
    left, right = view["longitude"] - half, view["longitude"] + half
    outside = [b["name"] for b in config.BERTHS if not left <= b["lon"] <= right]
    assert outside == [], f"berths cut off at zoom {view['zoom']}: {outside}"


def test_non_phone_view_is_one_and_closer_than_phone():
    views = [app_charts.map_view(ua) for ua in (WINDOWS_CHROME, "", None)]
    for view in views:
        assert {"latitude", "longitude", "zoom"} <= set(view)
    zooms = {view["zoom"] for view in views}
    assert len(zooms) == 1, f"desktop, empty and missing User-Agent differ: {zooms}"
    assert zooms.pop() > app_charts.map_view(IPHONE_SAFARI)["zoom"]
