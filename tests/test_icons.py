"""Black-box tests for icons.py: vessel shapes for the map (spec section 8 point 8,
section 10 and section 13 "icons").

Only what is checkable without rendering: keys and order, the pydeck IconLayer
icon format, self-contained SVG data URIs, distinct shapes, the neutral
fallback and the absence of a Streamlit dependency. The visual check (screenshot
at zoom 11, light and dark theme) is manual and not part of the suite.
No network, no database.
"""

import base64
import importlib
import sys
from urllib.parse import unquote

import pytest

import classify
import icons

KEYS = ["cargo", "tanker", "passenger", "service", "pleasure", "other"]
SHAPED = ["cargo", "tanker", "passenger", "service", "pleasure"]


def decode(url):
    """Decode an SVG data URI, base64 or percent-encoded."""
    header, _, payload = url.partition(",")
    if ";base64" in header:
        return base64.b64decode(payload).decode("utf-8")
    return unquote(payload)


# --- ICONS: keys and order ---------------------------------------------------------------

def test_icons_has_exactly_the_six_keys_in_legend_order():
    assert list(icons.ICONS) == KEYS


def test_commercial_shapes_come_first_in_legend_order():
    # With "commercial only" on, the legend shows cargo, tanker, passenger in this order.
    assert list(icons.ICONS)[:3] == ["cargo", "tanker", "passenger"]


# --- ICONS: pydeck IconLayer format ----------------------------------------------------------

@pytest.mark.parametrize("key", KEYS)
def test_icon_has_the_pydeck_fields(key):
    icon = icons.ICONS[key]
    assert isinstance(icon, dict)
    assert {"url", "width", "height", "mask", "id"} <= set(icon)


@pytest.mark.parametrize("key", KEYS)
def test_icon_url_is_an_svg_data_uri(key):
    url = icons.ICONS[key]["url"]
    assert isinstance(url, str)
    assert url.startswith("data:image/svg+xml")


@pytest.mark.parametrize("key", KEYS)
def test_icon_url_decodes_to_an_svg_element(key):
    assert "<svg" in decode(icons.ICONS[key]["url"])


@pytest.mark.parametrize("key", KEYS)
def test_icon_svg_is_self_contained(key):
    # Spec: SVG in data URIs, no external files and no CDN. The only URL allowed
    # is the SVG XML namespace identifier, which is never fetched.
    svg = decode(icons.ICONS[key]["url"])
    stripped = svg.replace("http://www.w3.org/2000/svg", "").replace(
        "http://www.w3.org/1999/xlink", "")
    assert "http://" not in stripped and "https://" not in stripped
    assert "<image" not in svg
    assert "@import" not in svg
    assert "<script" not in svg.lower()


@pytest.mark.parametrize("key", KEYS)
def test_icon_svg_uses_a_24_grid(key):
    # Assumption: "drawn on a 24x24 grid" is expressed as viewBox="0 0 24 24";
    # the spec states the grid, not how the SVG declares it.
    svg = decode(icons.ICONS[key]["url"])
    assert 'viewBox="0 0 24 24"' in svg or "viewBox='0 0 24 24'" in svg


@pytest.mark.parametrize("key", KEYS)
def test_icon_is_square_with_positive_integer_size(key):
    icon = icons.ICONS[key]
    for side in ("width", "height"):
        assert isinstance(icon[side], int) and not isinstance(icon[side], bool)
        assert icon[side] > 0
    assert icon["width"] == icon["height"]


@pytest.mark.parametrize("key", KEYS)
def test_icon_is_a_mask(key):
    # mask=True: the icon is tinted with the state colour of the layer.
    assert icons.ICONS[key]["mask"] is True


@pytest.mark.parametrize("key", KEYS)
def test_icon_id_is_a_non_empty_string(key):
    icon_id = icons.ICONS[key]["id"]
    assert isinstance(icon_id, str) and icon_id


def test_icon_ids_are_all_different():
    ids = [icons.ICONS[k]["id"] for k in KEYS]
    assert len(set(ids)) == len(KEYS)


def test_icon_urls_are_all_different():
    # Six distinct shapes: no category shares its drawing with another.
    urls = [icons.ICONS[k]["url"] for k in KEYS]
    assert len(set(urls)) == len(KEYS)


def test_decoded_svgs_are_all_different():
    # Guards against the same drawing encoded in two different ways.
    svgs = [" ".join(decode(icons.ICONS[k]["url"]).split()) for k in KEYS]
    assert len(set(svgs)) == len(KEYS)


# --- vessel_icon(category) -------------------------------------------------------------------

@pytest.mark.parametrize("key", KEYS)
def test_vessel_icon_returns_the_icon_of_a_known_category(key):
    assert icons.vessel_icon(key) == icons.ICONS[key]


@pytest.mark.parametrize("key", SHAPED)
def test_shaped_categories_do_not_get_the_neutral_icon(key):
    assert icons.vessel_icon(key)["url"] != icons.ICONS["other"]["url"]


@pytest.mark.parametrize("value", [
    "unknown", None, "", "Cargo", "CARGO", " cargo", "cargo ", "fishing", "container",
    0, 70, 3.5, True, b"cargo", ("cargo",), frozenset(),
])
def test_vessel_icon_falls_back_to_neutral(value):
    assert icons.vessel_icon(value) == icons.ICONS["other"]


@pytest.mark.parametrize("value", [[], {}, ["cargo"], {"cargo": 1}, set(), object()])
def test_vessel_icon_never_raises_on_odd_or_unhashable_values(value):
    # A list or dict is not hashable: a plain `category in ICONS` would raise.
    assert icons.vessel_icon(value) == icons.ICONS["other"]


def test_vessel_icon_is_stable_for_the_same_category():
    for key in KEYS + ["unknown", None]:
        assert icons.vessel_icon(key) == icons.vessel_icon(key)


def test_each_id_always_maps_to_the_same_url():
    # deck.gl caches icons by id: one id must always come with the same url.
    url_of = {}
    for value in KEYS + ["unknown", None, "", "x", 42] * 2:
        icon = icons.vessel_icon(value)
        assert url_of.setdefault(icon["id"], icon["url"]) == icon["url"]


# --- link with classify.ship_category ---------------------------------------------------------

AIS_CODES = list(range(0, 100)) + [None]


@pytest.mark.parametrize("code", AIS_CODES)
def test_every_ship_category_value_gets_an_icon(code):
    category = classify.ship_category(code)
    assert icons.vessel_icon(category) in list(icons.ICONS.values())


def test_ship_category_reaches_every_shaped_category():
    # Spec: six shapes, one per ship_category category; each shaped category must
    # be producible from some AIS type code.
    produced = {classify.ship_category(code) for code in AIS_CODES}
    assert set(SHAPED) <= produced


def test_unknown_type_gets_the_neutral_icon():
    # A vessel with no AIS type must not look like a specific type.
    assert icons.vessel_icon(classify.ship_category(0)) == icons.ICONS["other"]
    assert icons.vessel_icon(classify.ship_category(None)) == icons.ICONS["other"]


def test_commercial_codes_get_their_shape():
    assert icons.vessel_icon(classify.ship_category(70)) == icons.ICONS["cargo"]
    assert icons.vessel_icon(classify.ship_category(80)) == icons.ICONS["tanker"]
    assert icons.vessel_icon(classify.ship_category(60)) == icons.ICONS["passenger"]
    assert icons.vessel_icon(classify.ship_category(40)) == icons.ICONS["passenger"]


def test_tug_and_sailing_codes_get_their_shape():
    # Assumption: AIS type 52 (tug) maps to "service" and 36 (sailing) / 37
    # (pleasure craft) map to "pleasure"; the spec names the categories
    # ("service (rimorchiatore)", "pleasure (barca a vela)") but not the codes.
    assert icons.vessel_icon(classify.ship_category(52)) == icons.ICONS["service"]
    assert icons.vessel_icon(classify.ship_category(36)) == icons.ICONS["pleasure"]
    assert icons.vessel_icon(classify.ship_category(37)) == icons.ICONS["pleasure"]


# --- no Streamlit dependency -----------------------------------------------------------------

def test_icons_imports_without_streamlit(monkeypatch):
    # sys.modules[name] = None makes any `import streamlit` raise ImportError;
    # icons is then imported fresh. monkeypatch restores both entries afterwards.
    monkeypatch.setitem(sys.modules, "streamlit", None)
    monkeypatch.delitem(sys.modules, "icons", raising=False)
    fresh = importlib.import_module("icons")
    assert list(fresh.ICONS) == KEYS
