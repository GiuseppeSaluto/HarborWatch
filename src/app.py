"""Streamlit dashboard: vessels by state on a map, and waiting time at anchor."""

import asyncio
import math
from collections import Counter
from datetime import UTC, datetime, timedelta

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st
from pymongo import MongoClient
from pymongo.errors import PyMongoError

import config
from classify import COMMERCIAL, ship_category
from diagnostics import diagnose, probe_stream
from history import continuous_hours, hourly_series, longest_run, vessel_tracks
from optimize import plan_berths, prepare, service_minutes, stays_pipeline, trusted_stay, usable_stays

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
    try:
        data_status(now)  # before the empty check: it matters most when the ingestion is down
        df = load_states(get_db(), now)
    except PyMongoError:
        st.error("Cannot reach MongoDB Atlas: see Diagnostics in the sidebar.", icon=":material/cloud_off:")
        return
    if df.empty:
        st.warning(f"No vessel heard from in the last {config.STALE_MINUTES} minutes: is ingest.py running?")
        return

    df["type"] = df["ship_type"].map(ship_category)
    everyone = df  # the berth plan needs every moored vessel, whatever the toggle says
    heard = len(df)
    if st.toggle("Commercial vessels only (cargo, tanker, passenger)", value=True):
        df = df[df["type"].isin(COMMERCIAL)]
    hidden = heard - len(df)

    # Known limit: `since` is the first time *we* saw the vessel in this state, so vessels
    # already at anchor when the ingestion started show a shorter wait than the real one.
    anchored = df[df["state"] == "anchored"].assign(wait=lambda d: now - pd.to_datetime(d["since"], utc=True))
    counts = df["state"].value_counts()

    # Sparkline in the "At anchor" card: the hourly series of the chart below, last 24 hours with data.
    trend = congestion_history()
    trend = trend["anchored"].dropna().tail(24).tolist() if not trend.empty else []
    cols = st.columns(5)
    for col, state in zip(cols, LABELS):
        spark = {"chart_data": trend, "chart_type": "area",
                 "help": "Trend: commercial vessels at anchor per hour, last 24 hours with data"} \
            if state == "anchored" and len(trend) > 1 else {}
        # height="stretch": cards without a sparkline grow to the row's height, so the row stays even.
        # The keyed container lets state_card_css() paint the card in its state's map color.
        with col.container(key=f"state-{state}", height="stretch"):
            st.metric(LABELS[state], int(counts.get(state, 0)), border=True, icon=ICONS[state], height="stretch", **spark)
    st.markdown(state_card_css(), unsafe_allow_html=True)
    cols[3].metric("Mean wait at anchor", fmt(anchored["wait"].mean()) if len(anchored) else "-",
                   border=True, icon=":material/hourglass_top:", height="stretch")
    cols[4].metric("Max wait at anchor", fmt(anchored["wait"].max()) if len(anchored) else "-",
                   border=True, icon=":material/schedule:", height="stretch")

    show_tracks = st.toggle("Show tracks", value=True,
                            help=f"Paths of the last {config.TRACK_HOURS} h, for vessels that moved at least "
                                 f"{config.TRACK_MIN_MOVE_M} m")
    st.pydeck_chart(port_map(df, recent_tracks() if show_tracks else ([], {})), height=520)
    st.caption(legend() + f" · lines: paths of the last {config.TRACK_HOURS} h · rings: berth areas from "
               "data/berths.json · hover for details", unsafe_allow_html=True)

    st.subheader("Commercial vessels at anchor, per hour")
    history = congestion_history()
    if history.empty:
        st.write("No history yet.")
    else:
        st.altair_chart(congestion_chart(history), width="stretch")
        st.caption("Distinct commercial vessels seen at anchor during each hour (Genoa time). "
                   "Gaps are hours when the ingestion was off, not empty anchorages.")

    st.subheader("Waiting at anchor")
    if anchored.empty:
        st.write("No vessel at anchor.")
    else:
        st.dataframe(
            anchored.sort_values("wait", ascending=False)
            .assign(wait=lambda d: d["wait"].map(fmt))[["name", "mmsi", "type", "length", "destination", "wait"]],
            hide_index=True,
        )
    berth_plan(everyone, now)

    st.caption(f"Updated {local(now):%H:%M:%S %Z} · vessels heard from in the last {config.STALE_MINUTES} min · "
               f"{hidden} hidden (tugs, yachts, service craft, or type not received yet) · "
               "map © OpenStreetMap contributors")


@st.cache_data(ttl=600)
def congestion_history():
    """Commercial vessels at anchor per hour; hours without any data stay empty, not zero."""
    db = get_db()
    since = datetime.now(UTC) - timedelta(days=config.POSITIONS_TTL_DAYS)
    hour = {"$dateTrunc": {"date": "$ts", "unit": "hour"}}
    covered = {pd.Timestamp(d["_id"]).tz_convert("UTC") for d in db.positions.aggregate([
        {"$match": {"state": {"$exists": True}, "ts": {"$gte": since}}}, {"$group": {"_id": hour}}])}
    if not covered:
        return pd.DataFrame()  # hourly_series would give no rows, and a DataFrame of none has no columns
    commercial = {v["mmsi"] for v in db.vessels.find({}, {"mmsi": 1, "ship_type": 1})
                  if ship_category(v.get("ship_type")) in COMMERCIAL}
    counts = Counter(pd.Timestamp(d["_id"]["hour"]).tz_convert("UTC") for d in db.positions.aggregate([
        {"$match": {"state": "anchored", "ts": {"$gte": since}}},
        {"$group": {"_id": {"hour": hour, "mmsi": "$mmsi"}}}]) if d["_id"]["mmsi"] in commercial)
    history = pd.DataFrame(hourly_series(covered, counts))
    history["label"] = history["hour"].map(lambda h: f"{local(h):%a %H:%M}")
    history["hour"] = history["hour"].map(chart_time)
    return history


@st.cache_data(ttl=3600)  # a week of positions: recompute hourly, not every minute
def stay_estimates():
    """Expected stay per category, measured from the position history where possible."""
    db = get_db()
    vessels = {v["mmsi"]: v for v in db.vessels.find({}, {"mmsi": 1, "ship_type": 1, "length": 1})}
    # The stays are found inside MongoDB: only they travel, not the whole position history.
    stays = [{"category": ship_category(vessels.get(d["mmsi"], {}).get("ship_type")),
              "length": vessels.get(d["mmsi"], {}).get("length"),
              "minutes": (d["end"] - d["start"]).total_seconds() / 60}
             for d in db.positions.aggregate(stays_pipeline(config.STALE_MINUTES))]
    history = congestion_history()
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
    st.altair_chart(gantt(plan, berths, now, names), width="stretch")
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
        st.caption("Not planned, no known berth fits (berth lengths in data/berths.json are partly observed, "
                   "so they can be too short) or length unknown: " + ", ".join(left_out))
    busy = sum(1 for b in berths if b["free_from"])
    st.caption(f"Minimizes the total wait over {len(berths)} berths ({busy} busy now) in data/berths.json. "
               "Expected stays: " + ", ".join(
                   f"{c} {m // 60} h " + (
                       f"(median of {counts[c]} observed)"
                       if trusted_stay(counts[c], config.SERVICE_MINUTES[c], window, config.MIN_STAYS)
                       else f"(default: {counts[c]} of {config.MIN_STAYS} stays observed, "
                            f"{window // 60} of {config.STAY_WINDOW_FACTOR * config.SERVICE_MINUTES[c] // 60} h "
                            "of continuous collection)")
                   for c, m in service.items()) + ". Vessels we did not see arrive are assumed halfway through their stay.")


def data_status(now):
    """How far to trust the page: data freshness, continuity, measured stays, storage."""
    db = get_db()
    last = db.positions.find_one(sort=[("ts", -1)], projection={"ts": 1})
    age = now - pd.Timestamp(last["ts"]) if last else None
    if age is None or age > timedelta(minutes=config.STALE_MINUTES):
        status, icon, color = "stopped", ":material/block:", "red"
    elif age > timedelta(minutes=config.LIVE_MINUTES):
        status, icon, color = "delayed", ":material/warning:", "orange"
    else:
        status, icon, color = "live", ":material/check_circle:", "green"

    # Continuity at hour resolution, reusing the cached hourly series: count back from the
    # last hour with data to the first hour without.
    history = congestion_history()
    hours = continuous_hours(history["anchored"].tolist() if not history.empty else [])

    _, counts, window = stay_estimates()
    measured = [c for c, n in counts.items() if trusted_stay(n, config.SERVICE_MINUTES[c], window, config.MIN_STAYS)]
    stats = db.command("dbStats")
    used_mb = (stats["dataSize"] + stats["indexSize"]) / 2**20

    stopped = status == "stopped"
    run = f"last run {hours} h" if stopped else f"{hours} h continuous"  # hours belong to the past run
    # Status colors are for status only, and always come with an icon and a word.
    st.badge(f"Data {status}" + ("" if age is None else f" · last position {fmt_age(age)}"), icon=icon, color=color)
    with st.expander(f"Data status: {status}, {run}", icon=icon):
        cols = st.columns(4)
        cols[0].metric("Last position", "never" if age is None else fmt_age(age), help=status)
        cols[1].metric("Continuous collection", "0 h" if stopped else f"{hours} h",
                       help=f"Hours in a row with data, up to now{f' (the last run lasted {hours} h)' if stopped else ''}. "
                            "The berth plan and the stay estimates need days of it.")
        cols[2].metric("Measured stays", f"{len(measured)} of {len(counts)} categories",
                       help=", ".join(f"{c}: {n} of {config.MIN_STAYS} stays" for c, n in counts.items()) +
                            f"; longest continuous collection {window // 60} h. A category needs {config.MIN_STAYS} "
                            f"stays and a continuous window of {config.STAY_WINDOW_FACTOR}x its default stay; "
                            "otherwise it uses the default.")
        cols[3].metric("Atlas storage", f"{used_mb:.1f} MB", help=f"of {config.STORAGE_LIMIT_MB} MB "
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


@st.fragment(run_every="60s")
def diagnostics_panel():
    """Which link is broken when data stop: Atlas, the ingestion or AISStream (spec section 8, point 6)."""
    st.subheader("Diagnostics")
    try:
        last = get_db().positions.find_one(sort=[("ts", -1)], projection={"ts": 1})
        atlas_ok, age = True, datetime.now(UTC) - last["ts"] if last else None
    except PyMongoError:
        atlas_ok, age = False, None
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
    """Tie each state card to the map: accent line, icon and sparkline in the state's dot color."""
    c = colors()
    return "<style>" + "".join(
        f".st-key-state-{s} [data-testid=stMetric] {{box-shadow: inset 0 3px 0 {c[s]};}}"
        f".st-key-state-{s} [data-testid=stMetricIcon] {{color: {c[s]};}}"
        f".st-key-state-{s} [data-testid=stMetricChart] path[aria-roledescription='area mark'] {{fill: {c[s]}; fill-opacity: 0.25;}}"
        f".st-key-state-{s} [data-testid=stMetricChart] path[aria-roledescription='line mark'] {{stroke: {c[s]};}}"
        for s in LABELS) + "</style>"


def legend():
    """Map legend in the exact dot colors of the current theme (HTML, for st.caption)."""
    c = colors()
    return " · ".join(f'<span style="color:{c[s]}">●</span> {LABELS[s].lower()}' for s in LABELS)


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
    """Vessels colored by state, plus one ring per berth area; both explain themselves on hover."""
    c = colors()
    known = lambda v, unit="": "?" if pd.isna(v) else f"{v:g}{unit}"
    vessels = pd.DataFrame({
        "position": df[["lon", "lat"]].values.tolist(),
        "color": df["state"].map(lambda s: rgb(c[s])),
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
            pdk.Layer("PathLayer", paths, get_path="path", get_color="color", get_width=2,
                      width_min_pixels=2, opacity=0.7, pickable=True),
            # Surface-colored ring keeps overlapping dots apart (moored ships sit side by side).
            pdk.Layer("ScatterplotLayer", vessels, get_position="position", get_fill_color="color",
                      get_radius=60, radius_min_pixels=4, stroked=True, get_line_color=rgb(c["surface"]),
                      line_width_min_pixels=1, pickable=True),
        ],
    )


def congestion_chart(history):
    """One series, so no legend: the subheader names it. Hover shows the hour and the count."""
    c = colors()
    hover = alt.selection_point(on="pointerover", nearest=True, fields=["hour"], empty=False, clear="pointerout")
    base = alt.Chart(history).encode(
        x=alt.X("hour:T", title=None, scale=alt.Scale(type="utc"), axis=alt.Axis(format="%a %H:%M", grid=False)))
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


with st.sidebar:
    diagnostics_panel()
dashboard()
