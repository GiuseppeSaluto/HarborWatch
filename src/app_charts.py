"""Dashboard drawings from the data they are given (no Atlas): map, anchorage chart, berth plan, legend,
and the time and duration formats they share with the page."""

import math
from datetime import timedelta

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st

import config
from app_theme import LABELS, colors
from icons import SVG, vessel_icon

MOORED, PROPOSED = "Moored now (estimated stay)", "Proposed mooring"


def local(t):
    """A UTC instant as the port's clock time, for text."""
    return pd.Timestamp(t).tz_convert(config.TIMEZONE)


def chart_time(t):
    """The port's clock time labelled as UTC, for charts.

    Vega-Lite only draws times in UTC or in the browser's zone; on a UTC scale this shows the
    port's clock whatever the viewer's browser is set to.
    """
    return local(t).tz_localize(None).tz_localize("UTC")


def fmt(delta):
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m"


def vessel_name(name, mmsi):
    """The AIS name, or the MMSI until a ShipStaticData arrives (name missing, NaN or None)."""
    return name if isinstance(name, str) else str(mmsi)


def fmt_age(delta):
    seconds = int(delta.total_seconds())
    return f"{seconds} s ago" if seconds < 120 else f"{seconds // 60} min ago" if seconds < 7200 else f"{seconds // 3600} h ago"


def rgb(hex_color):
    return [int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]


def legend(df):
    """Map legend (HTML, for st.caption): the state colors of the current theme, then the shapes
    of the categories on the map."""
    c = colors()
    shape = lambda k: SVG[k].replace('width="48" height="48"', 'width="1.1em" height="1.1em" fill="currentColor" '
                                     'style="vertical-align:-0.2em"')
    present = {vessel_icon(t)["id"] for t in df["type"]}
    return " · ".join([f'<span style="color:{c[s]}">●</span> {LABELS[s].lower()}' for s in LABELS]
                      + [f"{shape(k)} {k}" for k in SVG if k in present])


def port_map(df, tracks_and_spans):
    """Vessels shaped by category and colored by state, plus one ring per berth area; both explain
    themselves on hover."""
    c = colors()
    known = lambda v, unit="": "?" if pd.isna(v) else f"{v:g}{unit}"
    vessels = pd.DataFrame({
        "position": df[["lon", "lat"]].values.tolist(),
        "color": df["state"].map(lambda s: rgb(c[s])),
        "icon": df["type"].map(vessel_icon),
        # Plain-text title/line1/line2 on both layers: pydeck has one tooltip template for every
        # layer, and it escapes field values, so the markup lives in the template, not the data.
        "title": [vessel_name(r["name"], r["mmsi"]) for _, r in df.iterrows()],
        "line1": [f"{r['type']}, {known(r['length'], ' m')}" for _, r in df.iterrows()],
        "line2": [f"{LABELS[r['state']]}, {known(r['sog'], ' kn')}" for _, r in df.iterrows()],
    })
    by_area = {}
    for b in config.BERTHS:
        by_area.setdefault(b["zone"], []).append(b)
    areas = pd.DataFrame([{
        "position": [bs[0]["lon"], bs[0]["lat"]], "name": area,
        "title": area,
        "line1": f"{len(bs)} berth{'s' * (len(bs) > 1)}: {', '.join(str(b['length']) for b in bs)} m",
        "line2": f"accepts {', '.join(bs[0]['categories'])}",
    } for area, bs in by_area.items()])
    # Direct labels only where they fit: the passenger terminals and SECH sit a few hundred
    # metres apart, so their names would pile up; hovering their rings tells them apart.
    km = lambda p, q: math.dist([p[0] * 111 * math.cos(math.radians(p[1])), p[1] * 111],
                                [q[0] * 111 * math.cos(math.radians(q[1])), q[1] * 111])
    labeled = areas[[all(km(p, q) > 1 for q in areas["position"] if q is not p) for p in areas["position"]]]
    # Paths only for the vessels on the map, so the commercial-only toggle applies to them too.
    tracks, spans = tracks_and_spans
    shown = {r["mmsi"]: r for _, r in df.iterrows()}
    paths = pd.DataFrame([{
        "path": path, "color": rgb(c[shown[t["mmsi"]]["state"]]),
        "title": vessel_name(shown[t["mmsi"]]["name"], t["mmsi"]),
        "line1": f"path of the last {fmt(spans[t['mmsi']])}", "line2": LABELS[shown[t["mmsi"]]["state"]],
    } for t in tracks if t["mmsi"] in shown for path in t["paths"]],
        columns=["path", "color", "title", "line1", "line2"])
    ink = [255, 255, 255] if st.context.theme.type == "dark" else [11, 11, 11]
    return pdk.Deck(
        map_style=None,  # Streamlit's basemap, light or dark with the theme
        initial_view_state=pdk.ViewState(latitude=44.405, longitude=8.85, zoom=11.3),
        tooltip={"html": "<b>{title}</b><br>{line1}<br>{line2}"},
        layers=[
            # A faint fill makes the whole ring hoverable: deck.gl only picks drawn pixels, so an
            # outline alone would need the pointer exactly on its 1.5 px line.
            pdk.Layer("ScatterplotLayer", areas, get_position="position", get_radius=220, filled=True,
                      get_fill_color=ink + [30], stroked=True, get_line_color=ink, line_width_min_pixels=1.5,
                      pickable=True),
            pdk.Layer("TextLayer", labeled, get_position="position", get_text="name", get_size=13,
                      get_color=ink, get_pixel_offset=[0, -20]),
            # Under the dots, so a vessel's dot stays on top of its own path.
            # Dashed: a track joins sampled positions, it is not the exact route sailed.
            pdk.Layer("PathLayer", paths, get_path="path", get_color="color", get_width=2,
                      width_min_pixels=2, opacity=0.7, pickable=True,
                      extensions=[{"@@type": "PathStyleExtension", "dash": True}], get_dash_array=[4, 3],
                      dash_justified=True),
            # Surface-colored halo keeps overlapping icons apart (moored ships sit side by side).
            # Quoted: pydeck turns a bare string into a JavaScript expression ("pixels" = an undefined variable).
            pdk.Layer("ScatterplotLayer", vessels, get_position="position", get_fill_color=rgb(c["surface"]) + [200],
                      get_radius=11, radius_units="'pixels'"),
            # Shape = category, color = state; 20 px whatever the zoom (IconLayer sizes are in pixels by
            # default), so the anchorage stays readable at zoom 11.
            pdk.Layer("IconLayer", vessels, get_position="position", get_icon="icon", get_color="color",
                      get_size=20, pickable=True),
        ],
    )


def congestion_chart(history):
    """One series, so no legend: the subheader names it. Hover shows the hour and the count."""
    c = colors()
    hover = alt.selection_point(on="pointerover", nearest=True, fields=["hour"], empty=False, clear="pointerout")
    base = alt.Chart(history).encode(
        x=alt.X("hour:T", title=None, scale=alt.Scale(type="utc"), axis=alt.Axis(format="%a", grid=False, tickCount={"interval": "day", "step": 1})))  # one label a day
    line = base.mark_line(color=c["anchored"], strokeWidth=2).encode(
        y=alt.Y("anchored:Q", title="vessels", axis=alt.Axis(tickMinStep=1)))
    points = base.mark_point(filled=True, size=80, color=c["anchored"]).encode(
        y="anchored:Q", opacity=alt.condition(hover, alt.value(1), alt.value(0)),
        tooltip=[alt.Tooltip("label:N", title="hour"), alt.Tooltip("anchored:Q", title="at anchor")],
    ).add_params(hover)
    alone = base.mark_point(filled=True, size=64, color=c["anchored"]).encode(y="anchored:Q").transform_filter(
        alt.datum.alone)
    rule = base.mark_rule(color="gray").encode(opacity=alt.condition(hover, alt.value(0.5), alt.value(0)))
    return (line + alone + rule + points).properties(height=220)


def gantt(plan, berths, now, names):
    """One row per berth: the moored vessel's estimated stay, then the proposed moorings."""
    at = lambda minutes: now + timedelta(minutes=minutes)
    bars = pd.DataFrame(
        [{"berth": b["name"], "vessel": names.get(b["occupied_by"], str(b["occupied_by"])), "kind": MOORED,
          "from": now, "to": at(b["free_from"])}
         for b in berths if b["free_from"]] +
        [{"berth": p["berth"], "vessel": names[p["mmsi"]], "kind": PROPOSED, "from": at(p["start"]), "to": at(p["end"])}
         for p in plan])
    # Tooltips have no scale, so they get the local times as text; the bars get chart_time.
    bars["from_text"], bars["to_text"] = (bars[t].map(lambda d: f"{local(d):%a %H:%M}") for t in ("from", "to"))
    bars["from"], bars["to"] = bars["from"].map(chart_time), bars["to"].map(chart_time)
    c = colors()
    names = [b["name"] for b in berths]  # berths.json order, west to east; free berths keep their row
    # A 2px stroke in the page color keeps a moored bar and the proposed one after it apart.
    return alt.Chart(bars).mark_bar(cornerRadius=4, height={"band": 0.7}, stroke=c["surface"], strokeWidth=2).encode(
        # utc scale + plain format = the labels as given (chart_time); a formatType would switch to browser time.
        x=alt.X("from:T", title=None, scale=alt.Scale(type="utc"), axis=alt.Axis(format="%a %H:%M", grid=False)),
        x2="to:T",
        # Every berth labelled in full: Vega would otherwise drop overlapping labels and cut long ones.
        y=alt.Y("berth:N", title=None, scale=alt.Scale(domain=names), sort=names,
                axis=alt.Axis(labelOverlap=False, labelLimit=220)),
        color=alt.Color("kind:N", title=None, legend=alt.Legend(orient="top", labelLimit=320),
                        scale=alt.Scale(domain=[MOORED, PROPOSED], range=[c["at_berth"], c["anchored"]])),
        tooltip=["vessel:N", "berth:N", "kind:N", alt.Tooltip("from_text:N", title="from"), alt.Tooltip("to_text:N", title="to")],
    ).properties(height=30 * len(berths))
