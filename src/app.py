"""Streamlit dashboard: vessels by state on a map, and waiting time at anchor."""

from datetime import UTC, datetime, timedelta

import pandas as pd
import streamlit as st
from pymongo import MongoClient

import config
from classify import COMMERCIAL, ship_category

LABELS = {"at_berth": "At berth", "anchored": "At anchor", "underway": "Underway"}
COLORS = {"at_berth": "#2e7d32", "anchored": "#ef6c00", "underway": "#1565c0"}
LEGEND = "🟢 at berth · 🟠 at anchor · 🔵 underway"


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

    st.map(df.assign(color=df["state"].map(COLORS)), latitude="lat", longitude="lon", color="color", size=80)
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
    st.caption(f"Updated {now:%H:%M:%S} UTC · vessels heard from in the last {config.STALE_MINUTES} min · "
               f"{hidden} hidden (tugs, yachts, service craft, or type not received yet) · "
               "map © OpenStreetMap contributors")


dashboard()
