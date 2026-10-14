"""Streamlit dashboard: vessels by state on a map, and waiting time at anchor."""

import asyncio
import math
from datetime import UTC, datetime, timedelta

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

import config
from classify import COMMERCIAL, ship_category
from diagnostics import diagnose, probe_stream
from icons import SVG, vessel_icon
from history import continuous_hours, hourly_series, longest_run, vessel_tracks
from optimize import plan_berths, prepare, service_minutes, trusted_stay, usable_stays

LABELS = {"at_berth": "At berth", "anchored": "At anchor", "underway": "Underway"}
ICONS = {"at_berth": ":material/directions_boat:", "anchored": ":material/anchor:", "underway": ":material/sailing:"}
# Categorical slots 1-3 of the dataviz skill's validated reference palette, stepped per theme.
# Same color for a state everywhere: map dots and berth-plan bars.
PALETTE = {"light": {"at_berth": "#2a78d6", "anchored": "#eb6834", "underway": "#1baf7a", "surface": "#ffffff"},
           "dark": {"at_berth": "#3987e5", "anchored": "#d95926", "underway": "#199e70", "surface": "#0e1117"}}
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


# Chart-paper grid behind the page header, after aisstream.io's home page: 1 px lines every
# 32 px and a faint glow, fading out downwards so it never sits behind charts.
GRID = {"light": {"line": "rgba(49, 51, 63, 0.08)", "glow": "rgba(42, 120, 214, 0.10)"},
        "dark": {"line": "rgba(250, 250, 250, 0.06)", "glow": "rgba(57, 135, 229, 0.16)"}}


def header_grid_css():
    g = GRID["dark" if st.context.theme.type == "dark" else "light"]
    return f"""<style>
[data-testid=stMain]::before {{
    content: ""; position: absolute; inset: 0 0 auto 0; height: 340px; pointer-events: none;
    background-image: linear-gradient({g["line"]} 1px, transparent 1px),
        linear-gradient(90deg, {g["line"]} 1px, transparent 1px),
        radial-gradient(circle at 78% 24%, {g["glow"]}, transparent 30%);
    background-size: 32px 32px, 32px 32px, auto;
    mask-image: linear-gradient(to bottom, black 60%, transparent);
}}
</style>"""


def colors():
    return PALETTE["dark" if st.context.theme.type == "dark" else "light"]


@st.cache_resource
def get_db():
    """One MongoClient for the whole app, shared across reruns and sessions."""
    # tz_aware: dates come back as UTC-aware datetimes instead of naive ones.
    # 10 s instead of the default 30 s: when Atlas is unreachable, say so sooner.
    return MongoClient(config.MONGODB_URI, tz_aware=True, serverSelectionTimeoutMS=10_000)[config.DB_NAME]


def load_states(db, now):
    """Vessels heard from in the last STALE_MINUTES, with their static data."""
    rows = db.states.aggregate([
        {"$match": {"ts": {"$gte": now - timedelta(minutes=config.STALE_MINUTES)}}},
        {"$lookup": {"from": "vessels", "localField": "mmsi", "foreignField": "mmsi", "as": "vessel"}},
        {"$project": {
            "_id": 0, "mmsi": 1, "state": 1, "since": 1, "since_seen": 1, "ts": 1, "sog": 1,
            "lon": {"$arrayElemAt": ["$location.coordinates", 0]},  # GeoJSON: [lon, lat]
            "lat": {"$arrayElemAt": ["$location.coordinates", 1]},
            "name": {"$first": "$vessel.name"},  # missing until a ShipStaticData arrives
            "length": {"$first": "$vessel.length"},
            "destination": {"$first": "$vessel.destination"},
            "ship_type": {"$ifNull": [{"$first": "$vessel.ship_type"}, 0]},  # 0 = unknown
        }},
    ])
    return pd.DataFrame(list(rows))


def fmt(delta):
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m"


st.set_page_config(page_title="HarborWatch", page_icon=":material/anchor:", layout="wide")
st.title("HarborWatch: Port of Genoa")


@st.fragment(run_every="60s")  # reruns only this function, so the page doesn't flicker
def dashboard():
    now = pd.Timestamp(datetime.now(UTC))
    # The page in order of trust (spec section 8, point 9): the port now, its history, then the
    # model's proposal. The status comes first: it matters most when something is down.
    with st.container(horizontal=True, vertical_alignment="center"):
        atlas_ok = status_row(now)
        commercial_only = st.toggle("Commercial vessels only", value=True,
                                    help="Cargo, tanker and passenger ships: tugs, pilot boats and yachts "
                                         "are the life of the port, not congestion.")
        show_tracks = st.toggle("Show tracks", value=True,
                                help=f"Paths of the last {config.TRACK_HOURS} h, for vessels that moved at least "
                                     f"{config.TRACK_MIN_MOVE_M} m")
    if not atlas_ok:
        st.error("Cannot reach MongoDB Atlas: open Status for the diagnosis.", icon=":material/cloud_off:")
        return
    df = load_states(get_db(), now)
    if df.empty:
        st.warning(f"No vessel heard from in the last {config.STALE_MINUTES} minutes: is the ingestion running?")
        return

    df["type"] = df["ship_type"].map(ship_category)
    everyone = df  # the berth plan needs every moored vessel, whatever the toggle says
    heard = len(df)
    if commercial_only:
        df = df[df["type"].isin(COMMERCIAL)]
    hidden = heard - len(df)

    # `since` is the first time *we* saw the vessel in this state: when we did not see it begin
    # (since_seen false: ingestion restart, or a vessel unheard for over STALE_MINUTES), the
    # wait is only a lower bound, and the list says so.
    anchored = df[df["state"] == "anchored"].assign(
        wait=lambda d: now - pd.to_datetime(d["since"], utc=True),
        seen=lambda d: d["since_seen"].eq(True) if "since_seen" in d else False)
    counts = df["state"].value_counts()

    unseen = int((~anchored["seen"]).sum())
    # A wrapping row instead of columns: four cards in a row on a wide screen, two by two on a
    # phone (columns would stack them one per row, a whole screen before the map).
    with st.container(horizontal=True, key="cards"):
        for state in LABELS:
            # The keyed container lets state_card_css() paint the card in its state's map color.
            with st.container(key=f"state-{state}"):
                st.metric(LABELS[state], int(counts.get(state, 0)), border=True, icon=ICONS[state])
        st.metric("Mean wait", fmt(anchored["wait"].mean()) if len(anchored) else "-",
                  border=True, icon=":material/hourglass_top:",
                  help="Mean wait of the vessels at anchor now." + (
                      f" {unseen} of {len(anchored)} arrived before we saw them: the real mean is longer." if unseen else ""))
    st.html(state_card_css())

    left, right = st.columns([2, 1], gap="medium")
    with left:
        st.pydeck_chart(port_map(df, recent_tracks() if show_tracks else ([], {})), height=560)
        st.caption(legend(df) + f" · dashed lines: last {config.TRACK_HOURS} h · circles: berth areas · "
                   "hover for details", unsafe_allow_html=True)
    with right:
        st.subheader("Waiting at anchor")
        if anchored.empty:
            st.write("No vessel at anchor.")
        else:
            waiting = anchored.sort_values("wait", ascending=False)
            st.dataframe(pd.DataFrame({
                "vessel": [r["name"] if isinstance(r["name"], str) else str(r["mmsi"]) for _, r in waiting.iterrows()],
                "type": waiting["type"], "length": waiting["length"],
                "wait": [("" if r["seen"] else "≥ ") + fmt(r["wait"]) for _, r in waiting.iterrows()],
            }), hide_index=True)
            if unseen:
                st.caption("≥: we did not see the vessel arrive, so it has been waiting at least this long.")
        # The anchorage now and its history side by side: both are about the same vessels.
        st.markdown("**At anchor, per hour**")
        history = congestion_history()
        if history.empty:
            st.write("No history yet.")
        else:
            st.altair_chart(congestion_chart(history), width="stretch")
            st.caption("Commercial vessels seen at anchor each hour (Genoa time). "
                       "Gaps: hours without data, not empty anchorages.")

    berth_plan(everyone, now)

    st.caption(f"Updated {local(now):%H:%M:%S %Z} · vessels heard from in the last {config.STALE_MINUTES} min · "
               f"{hidden} other vessels hidden (tugs, yachts, service craft, or type not received yet) · "
               "map © OpenStreetMap contributors")


@st.cache_data(ttl=600)
def congestion_history(days=config.POSITIONS_TTL_DAYS):
    """Commercial vessels at anchor per hour from the archive; hours without data stay empty, not zero."""
    query = {} if days is None else {"_id": {"$gte": datetime.now(UTC) - timedelta(days=days)}}
    hours = list(get_db().congestion_hourly.find(query, {"anchored": 1}))
    if not hours:
        return pd.DataFrame()  # hourly_series would give no rows, and a DataFrame of none has no columns
    counts = {pd.Timestamp(h["_id"]).tz_convert("UTC"): h["anchored"] for h in hours}
    history = pd.DataFrame(hourly_series(set(counts), counts))
    history["label"] = history["hour"].map(lambda h: f"{local(h):%a %H:%M}")
    history["hour"] = history["hour"].map(chart_time)
    return history


@st.cache_data(ttl=3600)  # a week of positions: recompute hourly, not every minute
def stay_estimates():
    """Expected stay per category, measured from the archived stays where possible."""
    db = get_db()
    vessels = {v["mmsi"]: v for v in db.vessels.find({}, {"mmsi": 1, "ship_type": 1, "length": 1})}
    stays = [{"category": ship_category(vessels.get(d["mmsi"], {}).get("ship_type")),
              "length": vessels.get(d["mmsi"], {}).get("length"),
              "minutes": (d["end"] - d["start"]).total_seconds() / 60}
             for d in db.stays.find({}, {"mmsi": 1, "start": 1, "end": 1})]
    history = congestion_history(days=None)  # the continuous window can be older than the chart
    window = 60 * longest_run(history["anchored"].tolist() if not history.empty else [])
    counts = {c: sum(1 for s in usable_stays(stays) if s["category"] == c) for c in config.SERVICE_MINUTES}
    return service_minutes(stays, window), counts, window


def berth_plan(df, now):
    """Phase 2: proposed berth and start time for every commercial vessel at anchor."""
    st.subheader("Proposed berth plan")
    rows = df.astype(object).where(df.notna(), None)  # NaN (unknown length, name) -> None
    vessels = [{"mmsi": r["mmsi"], "state": r["state"], "category": r["type"], "length": r["length"],
                "since": r["since"], "since_seen": r.get("since_seen") is True,  # missing (older documents) = not seen
                "lon": r["lon"], "lat": r["lat"]}
               for _, r in rows.iterrows()]
    names = {r["mmsi"]: r["name"] or str(r["mmsi"]) for _, r in rows.iterrows()}
    service, counts, window = stay_estimates()
    ships, berths, unfit = prepare(vessels, config.BERTHS, now, service)
    plan = plan_berths(ships, berths)
    if not plan:
        st.write("No commercial vessel at anchor that fits a known berth.")
        return
    shown = {b["name"] for b in berths if b["free_from"]} | {p["berth"] for p in plan}
    st.altair_chart(gantt(plan, [b for b in berths if b["name"] in shown], now, names), width="stretch")
    with st.expander("Table view"):
        st.dataframe(pd.DataFrame([{
            "vessel": names[p["mmsi"]], "berth": p["berth"],
            "moors at": f"{local(now + timedelta(minutes=p['start'])):%a %H:%M %Z}",
            "more wait": fmt(timedelta(minutes=p["wait"])),
        } for p in plan]), hide_index=True)
    # Say who is left out instead of dropping them silently: a vessel longer than every berth
    # we know of in its category, or one whose length AIS has not told us yet.
    left_out = [f"{names[v['mmsi']]} ({v['category']}, " + (f"{v['length']:g} m" if v["length"] else "length unknown") + ")"
                for v in unfit]
    if left_out:
        st.caption("Not planned, no known berth fits (some berth lengths are our estimates, so they can be "
                   "too short) or length unknown: " + ", ".join(left_out))
    busy = sum(1 for b in berths if b["free_from"])
    st.caption(f"A simple model's proposal: it minimizes the total wait over {len(berths)} known berths "
               f"({busy} busy now, {len(berths) - len(shown)} free ones without a proposal not shown), from vessel "
               "length and type only. Real waits also depend on weather, pilots and commercial reasons: tankers "
               "often wait at anchor while a fitting berth is free. "
               "Expected stays: " + ", ".join(
                   f"{c} {m // 60} h " + (
                       f"(median of {counts[c]} observed)"
                       if trusted_stay(counts[c], config.SERVICE_MINUTES[c], window, config.MIN_STAYS)
                       else f"(default: {counts[c]} of {config.MIN_STAYS} stays observed, "
                            f"{window // 60} of {config.STAY_WINDOW_FACTOR * config.SERVICE_MINUTES[c] // 60} h "
                            "of continuous collection)")
                   for c, m in service.items()) + ". Vessels we did not see arrive are assumed halfway through their stay.")


def status_row(now):
    """The page's one status (spec section 8, point 9): a badge for the data, and a Status
    popover with the numbers behind it and the diagnostics. Returns whether Atlas answered."""
    try:
        last = get_db().positions.find_one(sort=[("ts", -1)], projection={"ts": 1})
        atlas_ok, age = True, now - pd.Timestamp(last["ts"]) if last else None
    except ConnectionFailure:  # network, DNS, IP access list; other errors must show
        atlas_ok, age = False, None
    if not atlas_ok:
        status, icon, color = "unreachable", ":material/cloud_off:", "red"
    elif age is None or age > timedelta(minutes=config.STALE_MINUTES):
        status, icon, color = "stopped", ":material/block:", "red"
    elif age > timedelta(minutes=config.LIVE_MINUTES):
        status, icon, color = "delayed", ":material/warning:", "orange"
    else:
        status, icon, color = "live", ":material/check_circle:", "green"
    # Status colors are for status only, and always come with an icon and a word.
    st.badge(f"Data {status}" + ("" if age is None else f" · last position {fmt_age(age)}"), icon=icon, color=color)
    with st.popover("Status", icon=":material/monitor_heart:"):
        if atlas_ok:
            data_numbers(age, status == "stopped")
        diagnostics_panel(atlas_ok, age)
    return atlas_ok


def data_numbers(age, stopped):
    """How far to trust the page: data freshness, continuity, measured stays, storage."""
    # Continuity at hour resolution, reusing the cached hourly series: count back from the
    # last hour with data to the first hour without.
    history = congestion_history()
    hours = continuous_hours(history["anchored"].tolist() if not history.empty else [])
    _, counts, window = stay_estimates()
    measured = [c for c, n in counts.items() if trusted_stay(n, config.SERVICE_MINUTES[c], window, config.MIN_STAYS)]
    stats = get_db().command("dbStats")
    used_mb = (stats["dataSize"] + stats["indexSize"]) / 2**20
    cols = st.columns(2)
    cols[0].metric("Last position", "never" if age is None else fmt_age(age))
    cols[1].metric("Continuous collection", "0 h" if stopped else f"{hours} h",
                   help=f"Hours in a row with data, up to now{f' (the last run lasted {hours} h)' if stopped else ''}. "
                        "The berth plan and the stay estimates need days of it.")
    cols = st.columns(2)
    cols[0].metric("Categories measured", f"{len(measured)} of {len(counts)}",
                   help=", ".join(f"{c}: {n} of {config.MIN_STAYS} stays" for c, n in counts.items()) +
                        f"; longest continuous collection {window // 60} h. A category needs {config.MIN_STAYS} "
                        f"stays and a continuous window of {config.STAY_WINDOW_FACTOR}x its default stay; "
                        "otherwise it uses the default.")
    cols[1].metric("Atlas storage", f"{used_mb:.1f} MB", help=f"of {config.STORAGE_LIMIT_MB} MB "
                   f"({100 * used_mb / config.STORAGE_LIMIT_MB:.1f}%); the TTL keeps it near ~70 MB")


# Badge per diagnosis: (label, icon, color). Status colors as in the data badge: green when fine,
# orange for external causes we can only wait out, red for links we can fix, gray when unknown.
DIAGNOSIS_BADGES = {
    "live": ("Live", ":material/check_circle:", "green"),
    "unknown": ("Not checked", ":material/help:", "gray"),
    "coverage": ("No coverage", ":material/signal_cellular_off:", "orange"),
    "aisstream": ("AISStream silent", ":material/wifi_off:", "orange"),
    "ingestion": ("Ingestion stopped", ":material/sync_problem:", "red"),
    "access": ("AISStream refused", ":material/key_off:", "red"),
    "atlas": ("Atlas unreachable", ":material/cloud_off:", "red"),
}


def diagnostics_panel(atlas_ok, age):
    """Which link is broken when data stop: Atlas, the ingestion or AISStream (spec section 8, point 6)."""
    st.markdown("**Diagnostics**")
    # The stream check opens a connection of its own, so it runs only on request, never on the
    # minute refresh: AISStream allows 3 connections per key, and the ingestion uses one.
    if st.button("Check the stream", icon=":material/network_check:",
                 help=f"Listens to AISStream for {config.PROBE_SECONDS} s and counts positions in the port and worldwide"):
        with st.spinner(f"Listening for {config.PROBE_SECONDS} s..."):
            st.session_state.probe = (datetime.now(UTC), asyncio.run(probe_stream(config.PROBE_SECONDS)))
    probed_at, probe = st.session_state.get("probe", (None, None))
    code, message = diagnose(atlas_ok, age, probe)
    label, icon, color = DIAGNOSIS_BADGES[code]
    st.badge(label, icon=icon, color=color)
    st.write(message)
    if probe:
        st.caption(f"Stream check at {local(probed_at):%H:%M}: {probe['port']} positions in the port, "
                   f"{probe['world']} worldwide in {probe['seconds']} s"
                   + ("" if probe["confirmed"] else ", subscription not confirmed"))


def state_card_css():
    """Tie each state card to the map: accent line and icon in the state's dot color."""
    c = colors()
    # Cards share the row equally and wrap two by two below ~4 x 170 px.
    return "<style>.st-key-cards > div {flex: 1 1 170px; min-width: 150px;}" + "".join(
        f".st-key-state-{s} [data-testid=stMetric] {{box-shadow: inset 0 3px 0 {c[s]};}}"
        f".st-key-state-{s} [data-testid=stMetricIcon] {{color: {c[s]};}}"
        for s in LABELS) + "</style>"


def legend(df):
    """Map legend (HTML, for st.caption): the state colors of the current theme, then the shapes
    of the categories on the map."""
    c = colors()
    shape = lambda k: SVG[k].replace('width="48" height="48"', 'width="1.1em" height="1.1em" fill="currentColor" '
                                     'style="vertical-align:-0.2em"')
    present = {vessel_icon(t)["id"] for t in df["type"]}
    return " · ".join([f'<span style="color:{c[s]}">●</span> {LABELS[s].lower()}' for s in LABELS]
                      + [f"{shape(k)} {k}" for k in SVG if k in present])


def fmt_age(delta):
    seconds = int(delta.total_seconds())
    return f"{seconds} s ago" if seconds < 120 else f"{seconds // 60} min ago" if seconds < 7200 else f"{seconds // 3600} h ago"


def rgb(hex_color):
    return [int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]


@st.cache_data(ttl=60)
def recent_tracks():
    """vessel_tracks over the last TRACK_HOURS, plus how long each vessel has been followed."""
    now = datetime.now(UTC)
    # A time filter on ts: served by the ts_1 index, the 2dsphere one is not needed here.
    positions = list(get_db().positions.find(
        {"ts": {"$gte": now - timedelta(hours=config.TRACK_HOURS)}}, {"_id": 0, "mmsi": 1, "ts": 1, "location": 1}))
    spans = {}
    for p in positions:
        first, last = spans.get(p["mmsi"], (p["ts"], p["ts"]))
        spans[p["mmsi"]] = (min(first, p["ts"]), max(last, p["ts"]))
    return vessel_tracks(positions, now), {m: last - first for m, (first, last) in spans.items()}


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
        "title": [r["name"] if isinstance(r["name"], str) else str(r["mmsi"]) for _, r in df.iterrows()],
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
        "title": shown[t["mmsi"]]["name"] if isinstance(shown[t["mmsi"]]["name"], str) else str(t["mmsi"]),
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


st.html(header_grid_css())  # style only: st.html applies it without taking space on the page
dashboard()
