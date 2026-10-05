"""Streamlit dashboard: vessels by state on a map, and waiting time at anchor."""

from datetime import UTC, datetime, timedelta

import altair as alt
import pandas as pd
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

    st.map(df.assign(color=df["state"].map(colors())), latitude="lat", longitude="lon", color="color", size=80)
    st.caption(LEGEND)

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
