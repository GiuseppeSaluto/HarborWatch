"""Streamlit dashboard: vessels by state on a map, and waiting time at anchor."""

import math
from collections import Counter
from datetime import UTC, datetime, timedelta

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st
from pymongo import MongoClient

import config
from classify import COMMERCIAL, ship_category
from optimize import measure_stays, plan_berths, prepare, service_minutes

LABELS = {"at_berth": "At berth", "anchored": "At anchor", "underway": "Underway"}
# Categorical slots 1-3 of the dataviz skill's validated reference palette, stepped per theme.
# Same color for a state everywhere: map dots and berth-plan bars.
PALETTE = {"light": {"at_berth": "#2a78d6", "anchored": "#eb6834", "underway": "#1baf7a", "surface": "#ffffff"},
           "dark": {"at_berth": "#3987e5", "anchored": "#d95926", "underway": "#199e70", "surface": "#0e1117"}}
LEGEND = "🔵 at berth · 🟠 at anchor · 🟢 underway"
MOORED, PROPOSED = "Moored now (estimated stay)", "Proposed mooring"


def colors():
    return PALETTE["dark" if st.context.theme.type == "dark" else "light"]


@st.cache_resource
def get_db():
    """One MongoClient for the whole app, shared across reruns and sessions."""
    # tz_aware: dates come back as UTC-aware datetimes instead of naive ones.
    return MongoClient(config.MONGODB_URI, tz_aware=True)[config.DB_NAME]


def load_states(db, now):
    """Vessels heard from in the last STALE_MINUTES, with their static data."""
    rows = db.states.aggregate([
        {"$match": {"ts": {"$gte": now - timedelta(minutes=config.STALE_MINUTES)}}},
        {"$lookup": {"from": "vessels", "localField": "mmsi", "foreignField": "mmsi", "as": "vessel"}},
        {"$project": {
            "_id": 0, "mmsi": 1, "state": 1, "since": 1, "ts": 1, "sog": 1,
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


st.set_page_config(page_title="HarborWatch", page_icon="⚓", layout="wide")
st.title("⚓ HarborWatch: Port of Genoa")


@st.fragment(run_every="60s")  # reruns only this function, so the page doesn't flicker
def dashboard():
    now = pd.Timestamp(datetime.now(UTC))
    data_status(now)  # before the empty check: it matters most when the ingestion is down
    df = load_states(get_db(), now)
    if df.empty:
        st.warning(f"No vessel heard from in the last {config.STALE_MINUTES} minutes: is ingest.py running?")
        return

    df["type"] = df["ship_type"].map(ship_category)
    everyone = df  # the berth plan needs every moored vessel, whatever the toggle says
    heard = len(df)
    if st.toggle("Commercial vessels only (cargo, tanker, passenger)", value=True):
        df = df[df["type"].isin(COMMERCIAL)]
    hidden = heard - len(df)

    # ponytail: `since` is the first time *we* saw the vessel in this state, so vessels
    # already at anchor when the ingestion started show a shorter wait than the real one.
    anchored = df[df["state"] == "anchored"].assign(wait=lambda d: now - pd.to_datetime(d["since"], utc=True))
    counts = df["state"].value_counts()

    cols = st.columns(5)
    for col, state in zip(cols, LABELS):
        col.metric(LABELS[state], int(counts.get(state, 0)))
    cols[3].metric("Mean wait at anchor", fmt(anchored["wait"].mean()) if len(anchored) else "-")
    cols[4].metric("Max wait at anchor", fmt(anchored["wait"].max()) if len(anchored) else "-")

    st.pydeck_chart(port_map(df), height=520)
    st.caption(LEGEND + " · ◯ berth areas from data/berths.json · hover for details")

    st.subheader("Commercial vessels at anchor, per hour")
    history = congestion_history()
    if history.empty:
        st.write("No history yet.")
    else:
        st.altair_chart(congestion_chart(history), width="stretch")
        st.caption("Distinct commercial vessels seen at anchor during each hour (UTC). "
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

    st.caption(f"Updated {now:%H:%M:%S} UTC · vessels heard from in the last {config.STALE_MINUTES} min · "
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
        return pd.DataFrame()
    commercial = {v["mmsi"] for v in db.vessels.find({}, {"mmsi": 1, "ship_type": 1})
                  if ship_category(v.get("ship_type")) in COMMERCIAL}
    counts = Counter(pd.Timestamp(d["_id"]["hour"]).tz_convert("UTC") for d in db.positions.aggregate([
        {"$match": {"state": "anchored", "ts": {"$gte": since}}},
        {"$group": {"_id": {"hour": hour, "mmsi": "$mmsi"}}}]) if d["_id"]["mmsi"] in commercial)
    hours = pd.date_range(min(covered), max(covered), freq="h")
    # None (not 0) for hours without data: the line breaks there instead of diving to zero.
    history = pd.DataFrame({"hour": hours, "label": [f"{h:%a %H:%M}" for h in hours],
                            "anchored": [counts.get(h, 0) if h in covered else None for h in hours]})
    # A line needs two neighbours: an hour between gaps (e.g. the first after a restart)
    # would be invisible, so it gets a dot.
    has = history["anchored"].notna()
    history["alone"] = has & ~has.shift(1, fill_value=False) & ~has.shift(-1, fill_value=False)
    return history


@st.cache_data(ttl=3600)  # a week of positions: recompute hourly, not every minute
def stay_estimates():
    """Expected stay per category, measured from the position history where possible."""
    db = get_db()
    # ponytail: reads every position with a state (up to ~300k at a 7-day TTL) once an hour.
    # Upgrade path: an aggregation pipeline, or storing stays as the ingestion sees them end.
    positions = list(db.positions.find({"state": {"$exists": True}}, {"_id": 0, "mmsi": 1, "ts": 1, "state": 1})
                     .sort([("mmsi", 1), ("ts", 1)]))
    category_of = {v["mmsi"]: ship_category(v.get("ship_type"))
                   for v in db.vessels.find({"length": {"$gte": config.MIN_STAY_VESSEL_M}}, {"mmsi": 1, "ship_type": 1})}
    stays = [(m, start, end) for m, start, end in measure_stays(positions, config.STALE_MINUTES)
             if end - start >= timedelta(minutes=config.MIN_STAY_MINUTES)]
    return service_minutes(stays, category_of, config.SERVICE_MINUTES, config.MIN_STAYS)


def berth_plan(df, now):
    """Phase 2: proposed berth and start time for every commercial vessel at anchor."""
    st.subheader("Proposed berth plan")
    rows = df.astype(object).where(df.notna(), None)  # NaN (unknown length, name) -> None
    states = [{"name": r["name"] or str(r["mmsi"]), "state": r["state"], "category": r["type"],
               "length": r["length"], "since": r["since"], "location": [r["lon"], r["lat"]]}
              for _, r in rows.iterrows()]
    service, counts = stay_estimates()
    vessels, berths = prepare(states, config.BERTHS, now, service)
    plan = plan_berths(vessels, berths)
    if not plan:
        st.write("No commercial vessel at anchor that fits a known berth.")
        return
    st.altair_chart(gantt(plan, berths, now), width="stretch")
    with st.expander("Table view"):
        st.dataframe(pd.DataFrame([{
            "vessel": p["vessel"], "berth": p["berth"],
            "moors at (UTC)": f"{now + timedelta(minutes=p['start']):%a %H:%M}",
            "more wait": fmt(timedelta(minutes=p["wait"])),
        } for p in plan]), hide_index=True)
    busy = sum(1 for b in berths if b["free_from"])
    st.caption(f"Minimizes the total wait over {len(berths)} berths ({busy} busy now) in data/berths.json. "
               "Expected stays: " + ", ".join(
                   f"{c} {m // 60} h " + (f"(median of {counts[c]} observed)" if counts[c] >= config.MIN_STAYS
                                         else f"(default, {counts[c]} of {config.MIN_STAYS} stays observed)")
                   for c, m in service.items()) + "; counted from when we first saw each vessel moored.")


def data_status(now):
    """How far to trust the page: data freshness, continuity, measured stays, storage."""
    db = get_db()
    last = db.positions.find_one(sort=[("ts", -1)], projection={"ts": 1})
    age = now - pd.Timestamp(last["ts"]) if last else None
    if age is None or age > timedelta(minutes=config.STALE_MINUTES):
        status = "⛔ stopped"
    elif age > timedelta(minutes=2):  # AIS delay (~10 s) + FLUSH_SECONDS, with margin
        status = "⚠️ delayed"
    else:
        status = "✅ live"

    # Continuity at hour resolution, reusing the cached hourly series: count back from the
    # last hour with data to the first hour without.
    history = congestion_history()
    hours = 0
    for value in reversed(history["anchored"].tolist() if not history.empty else []):
        if pd.isna(value):
            break
        hours += 1

    _, counts = stay_estimates()
    measured = [c for c, n in counts.items() if n >= config.MIN_STAYS]
    stats = db.command("dbStats")
    used_mb = (stats["dataSize"] + stats["indexSize"]) / 2**20

    stopped = status.startswith("⛔")
    run = f"last run {hours} h" if stopped else f"{hours} h continuous"  # hours belong to the past run
    with st.expander(f"Data status: {status}, {run}"):
        cols = st.columns(4)
        cols[0].metric("Last position", "never" if age is None else fmt_age(age), help=status)
        cols[1].metric("Continuous collection", "0 h" if stopped else f"{hours} h",
                       help=f"Hours in a row with data, up to now{f' (the last run lasted {hours} h)' if stopped else ''}. "
                            "The berth plan and the stay estimates need days of it.")
        cols[2].metric("Measured stays", f"{len(measured)} of {len(counts)} categories",
                       help=", ".join(f"{c}: {n} of {config.MIN_STAYS} stays" for c, n in counts.items()) +
                            ". Categories below the threshold use the default stay.")
        cols[3].metric("Atlas storage", f"{used_mb:.1f} MB", help=f"of {config.STORAGE_LIMIT_MB} MB "
                       f"({100 * used_mb / config.STORAGE_LIMIT_MB:.1f}%); the TTL keeps it near ~70 MB")


def fmt_age(delta):
    seconds = int(delta.total_seconds())
    return f"{seconds} s ago" if seconds < 120 else f"{seconds // 60} min ago" if seconds < 7200 else f"{seconds // 3600} h ago"


def rgb(hex_color):
    return [int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]


def port_map(df):
    """Vessels colored by state, plus one ring per berth area; both explain themselves on hover."""
    c = colors()
    known = lambda v, unit="": "?" if pd.isna(v) else f"{v:g}{unit}"
    vessels = pd.DataFrame({
        "position": df[["lon", "lat"]].values.tolist(),
        "color": df["state"].map(lambda s: rgb(c[s])),
        # One "tip" field per row: pydeck has a single tooltip template for every layer.
        "tip": [f"<b>{r['name'] if isinstance(r['name'], str) else r['mmsi']}</b><br>{r['type']}, "
                f"{known(r['length'], ' m')}<br>{LABELS[r['state']]}, {known(r['sog'], ' kn')}"
                for _, r in df.iterrows()],
    })
    by_area = {}
    for b in config.BERTHS:
        by_area.setdefault(b["area"], []).append(b)
    areas = pd.DataFrame([{
        "position": bs[0]["location"], "name": area,
        "tip": f"<b>{area}</b><br>{len(bs)} berth{'s' * (len(bs) > 1)}: "
               f"{', '.join(str(b['length']) for b in bs)} m<br>accepts {', '.join(bs[0]['accepts'])}",
    } for area, bs in by_area.items()])
    # Direct labels only where they fit: the passenger terminals and SECH sit a few hundred
    # metres apart, so their names would pile up; hovering their rings tells them apart.
    km = lambda p, q: math.dist([p[0] * 111 * math.cos(math.radians(p[1])), p[1] * 111],
                                [q[0] * 111 * math.cos(math.radians(q[1])), q[1] * 111])
    labeled = areas[[all(km(p, q) > 1 for q in areas["position"] if q is not p) for p in areas["position"]]]
    ink = [255, 255, 255] if st.context.theme.type == "dark" else [11, 11, 11]
    return pdk.Deck(
        map_style=None,  # Streamlit's basemap, light or dark with the theme
        initial_view_state=pdk.ViewState(latitude=44.405, longitude=8.85, zoom=11.3),
        tooltip={"html": "{tip}"},
        layers=[
            pdk.Layer("ScatterplotLayer", areas, get_position="position", get_radius=220, filled=False,
                      stroked=True, get_line_color=ink, line_width_min_pixels=1.5, pickable=True),
            pdk.Layer("TextLayer", labeled, get_position="position", get_text="name", get_size=13,
                      get_color=ink, get_pixel_offset=[0, -20]),
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
        tooltip=[alt.Tooltip("label:N", title="hour (UTC)"), alt.Tooltip("anchored:Q", title="at anchor")],
    ).add_params(hover)
    alone = base.mark_point(filled=True, size=64, color=c["anchored"]).encode(y="anchored:Q").transform_filter(
        alt.datum.alone)
    rule = base.mark_rule(color="gray").encode(opacity=alt.condition(hover, alt.value(0.5), alt.value(0)))
    return (line + alone + rule + points).properties(height=220)


def gantt(plan, berths, now):
    """One row per berth: the moored vessel's estimated stay, then the proposed moorings."""
    at = lambda minutes: now + timedelta(minutes=minutes)
    bars = pd.DataFrame(
        [{"berth": b["name"], "vessel": b["occupied_by"], "kind": MOORED, "from": now, "to": at(b["free_from"])}
         for b in berths if b["free_from"]] +
        [{"berth": p["berth"], "vessel": p["vessel"], "kind": PROPOSED, "from": at(p["start"]), "to": at(p["end"])}
         for p in plan])
    # Tooltips have no scale to make them UTC, so they get the times as text.
    bars["from (UTC)"], bars["to (UTC)"] = (bars[t].map(lambda d: f"{d:%a %H:%M}") for t in ("from", "to"))
    c = colors()
    names = [b["name"] for b in berths]  # berths.json order, west to east; free berths keep their row
    # A 2px stroke in the page color keeps a moored bar and the proposed one after it apart.
    return alt.Chart(bars).mark_bar(cornerRadius=4, height={"band": 0.7}, stroke=c["surface"], strokeWidth=2).encode(
        # utc scale + plain format = UTC labels; a formatType would switch to browser time.
        x=alt.X("from:T", title=None, scale=alt.Scale(type="utc"), axis=alt.Axis(format="%a %H:%M", grid=False)),
        x2="to:T",
        y=alt.Y("berth:N", title=None, scale=alt.Scale(domain=names), sort=names),
        color=alt.Color("kind:N", title=None, legend=alt.Legend(orient="top"),
                        scale=alt.Scale(domain=[MOORED, PROPOSED], range=[c["at_berth"], c["anchored"]])),
        tooltip=["vessel:N", "berth:N", "kind:N", "from (UTC):N", "to (UTC):N"],
    ).properties(height=22 * len(berths))


dashboard()
