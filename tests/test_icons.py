"""Tests for icons.py: vessel shapes for the map (spec section 8 point 8, section 13 "icons").

The drawings themselves are checked by eye on a screenshot; here only what a broken change
would get wrong: the pydeck format, the fallback to the neutral shape, and the link with
classify.ship_category. No network, no database.
"""

from urllib.parse import unquote

import classify
import icons


def test_six_distinct_icons_in_pydeck_format():
    assert list(icons.ICONS) == ["cargo", "tanker", "passenger", "service", "pleasure", "other"]
    for key, icon in icons.ICONS.items():
        svg = unquote(icon["url"].partition(",")[2])
        assert icon["url"].startswith("data:image/svg+xml") and "<svg" in svg, key
        # deck.gl cannot rasterize an SVG without natural dimensions (the map went blank on 2026-10-11).
        assert 'width="' in svg and 'height="' in svg, key
        assert icon["width"] == icon["height"] > 0 and icon["mask"] is True, key
    assert len({i["id"] for i in icons.ICONS.values()}) == len({i["url"] for i in icons.ICONS.values()}) == 6


def test_known_category_gets_its_icon():
    for key in icons.ICONS:
        assert icons.vessel_icon(key) == icons.ICONS[key]


def test_anything_else_gets_the_neutral_icon_without_raising():
    # Unhashable values included: a plain `category in ICONS` would raise on them.
    for value in ["unknown", None, "", "Cargo", " cargo", 70, b"cargo", [], {}, ["cargo"]]:
        assert icons.vessel_icon(value) == icons.ICONS["other"], value


def test_every_ais_type_maps_to_an_icon():
    expected = {70: "cargo", 80: "tanker", 60: "passenger", 40: "passenger", 52: "service",
                36: "pleasure", 37: "pleasure", 0: "other", None: "other"}
    for code in [*range(100), None]:
        icon = icons.vessel_icon(classify.ship_category(code))
        assert icon in icons.ICONS.values(), code
        if code in expected:
            assert icon == icons.ICONS[expected[code]], code
