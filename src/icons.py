"""Vessel shapes for the map, one per ship category (spec section 8 point 8).

Side-view silhouettes drawn for the project on a 24x24 grid, in the Material style: Material
Symbols has no container ship or tanker. Used as pydeck IconLayer masks, so only their shape
counts: the layer tints them with the state color.
"""

from urllib.parse import quote

# Legend order. "other" is the neutral dot, also for vessels with no AIS type yet.
# Explicit width and height: deck.gl cannot rasterize an SVG without natural dimensions.
SVG = {k: f'<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24">{shape}</svg>'
       for k, shape in {
    "cargo": '<path d="M1 15h22l-2.5 5h-17z"/><rect x="3" y="9" width="5.5" height="5.5" rx=".5"/>'
             '<rect x="9.5" y="9" width="5.5" height="5.5" rx=".5"/><rect x="6" y="5.5" width="6" height="3" rx=".5"/>'
             '<rect x="17" y="6" width="4" height="8.5" rx=".5"/>',
    "tanker": '<path d="M1 14h22l-2.5 6h-17z"/><rect x="3" y="12" width="13" height="1.5" rx=".5"/>'
              '<rect x="17" y="7" width="4" height="6.5" rx=".5"/><rect x="18.5" y="4.5" width="1.5" height="2"/>',
    "passenger": '<path d="M1 15h22l-2.5 5h-17z"/><rect x="3" y="10.5" width="18" height="4" rx="1"/>'
                 '<rect x="5" y="7" width="13" height="3" rx="1"/><rect x="13" y="3.5" width="3" height="3" rx=".5"/>',
    "service": '<path d="M3 14h18l-2 6H7z"/><rect x="8" y="8" width="7" height="5.5" rx="1"/>'
               '<rect x="11" y="3" width="1.5" height="4.5"/><rect x="4" y="12" width="3" height="1.5"/>',
    "pleasure": '<path d="M11 2v13H4z"/><path d="M12.5 5v10H19z"/><path d="M3 16.5h18L18 21H6z"/>',
    "other": '<circle cx="12" cy="12" r="7"/>',
}.items()}

# Drawn at 48 px and scaled down by the layer: sharper than 24 px on high-density screens.
ICONS = {k: {"url": "data:image/svg+xml;charset=utf-8," + quote(svg), "width": 48, "height": 48,
             "mask": True, "id": k} for k, svg in SVG.items()}


def vessel_icon(category):
    """The icon of a ship_category value; the neutral one for anything else, never raising."""
    return ICONS[category] if isinstance(category, str) and category in ICONS else ICONS["other"]
