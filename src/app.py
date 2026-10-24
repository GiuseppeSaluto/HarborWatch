"""Streamlit dashboard: vessels by state on a map, and waiting time at anchor."""

import asyncio
from datetime import UTC, datetime, timedelta

import pandas as pd
import streamlit as st
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

import config
from app_charts import chart_time, congestion_chart, fmt, fmt_age, gantt, legend, local, port_map, vessel_name
from app_theme import ICONS, LABELS, RUNNING_ICON_CSS, header_grid_css, state_card_css
from classify import COMMERCIAL, ship_category
from diagnostics import diagnose, probe_stream
from history import continuous_hours, hourly_series, longest_run, vessel_tracks
from optimize import plan_berths, prepare, service_minutes, trusted_stay, usable_stays


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


def toggle_map_state(state):
    key = f"show_{state}"
    st.session_state[key] = not st.session_state.get(key, True)


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
    unseen = int((~anchored["seen"]).sum())
    state_cards(df["state"].value_counts(), anchored, unseen)

    visible_states = [s for s in LABELS if st.session_state.get(f"show_{s}", True)]
    mapped = df[df["state"].isin(visible_states)]
    left, right = st.columns([2, 1], gap="medium")
    with left:
        st.pydeck_chart(port_map(mapped, recent_tracks() if show_tracks else ([], {})), height=560)
        if not visible_states:
            st.caption("All vessel states hidden. Select a card to show vessels.")
        st.caption(legend(mapped) + f" · dashed lines: last {config.TRACK_HOURS} h · circles: berth areas · "
                   "hover for details", unsafe_allow_html=True)
    with right:
        anchorage_panel(anchored, unseen)

    berth_plan(everyone, now)

    st.caption(f"Updated {local(now):%H:%M:%S %Z} · vessels heard from in the last {config.STALE_MINUTES} min · "
               f"{hidden} other vessels hidden (tugs, yachts, service craft, or type not received yet) · "
               "map © OpenStreetMap contributors")


def state_cards(counts, anchored, unseen):
    """One card per state (selecting it hides or shows those vessels on the map), then the mean wait."""
    # A wrapping row instead of columns: four cards in a row on a wide screen, two by two on a
    # phone (columns would stack them one per row, a whole screen before the map).
    with st.container(horizontal=True, key="cards"):
        for state in LABELS:
            with st.container(key=f"state-{state}"):
                visible = st.session_state.get(f"show_{state}", True)
                st.metric(LABELS[state], int(counts.get(state, 0)), border=True, icon=ICONS[state])
                action = f"{'Hide' if visible else 'Show'} {LABELS[state].lower()} on map"
                st.button(action, key=f"map-{state}", icon=":material/visibility:" if visible else ":material/visibility_off:",
                          on_click=toggle_map_state, args=(state,), width="stretch")
        st.metric("Mean wait", fmt(anchored["wait"].mean()) if len(anchored) else "-",
                  border=True, icon=":material/hourglass_top:",
                  help="Mean wait of the vessels at anchor now." + (
                      f" {unseen} of {len(anchored)} arrived before we saw them: the real mean is longer." if unseen else ""))
    st.html(state_card_css())


def anchorage_panel(anchored, unseen):
    """The vessels waiting at anchor now, longest wait first, and the anchorage's hourly history."""
    st.subheader("Waiting at anchor")
    if anchored.empty:
        st.write("No vessel at anchor.")
    else:
        waiting = anchored.sort_values("wait", ascending=False)
        st.dataframe(pd.DataFrame({
            "vessel": [vessel_name(r["name"], r["mmsi"]) for _, r in waiting.iterrows()],
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
    names = {r["mmsi"]: vessel_name(r["name"], r["mmsi"]) for _, r in rows.iterrows()}
    service, counts, window = stay_estimates()
    ships, berths, unfit = prepare(vessels, config.BERTHS, now, service)
    plan = plan_berths(ships, berths)
    if not plan:
        st.write("No commercial vessel at anchor that fits a known berth.")
        return
    shown = {b["name"] for b in berths if b["free_from"]} | {p["berth"] for p in plan}
    st.altair_chart(gantt(plan, [b for b in berths if b["name"] in shown], now, names), width="stretch")
    with st.expander("Proposed moorings as a table"):
        st.dataframe(pd.DataFrame([{
            "vessel": names[p["mmsi"]], "berth": p["berth"],
            "moors at": f"{local(now + timedelta(minutes=p['start'])):%a %H:%M %Z}",
            "extra wait": fmt(timedelta(minutes=p["wait"])),
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
                   f"({100 * used_mb / config.STORAGE_LIMIT_MB:.1f}%); the TTL keeps it near ~57 MB")


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


st.html(header_grid_css())  # style only: st.html applies it without taking space on the page
st.html(RUNNING_ICON_CSS)  # its own call: st.html keeps only the first <style> it is given
dashboard()
