"""Vessel selection and its collapsible detail card, independent of data storage."""

from datetime import timedelta

import pandas as pd
import streamlit as st

import config
from app_charts import fmt, fmt_age, local, port_map, vessel_name
from app_theme import LABELS, vessel_detail_css
from history import observations
from optimize import plan_reason


def select_map_vessel():
    # Callbacks run before widgets, so the selector can be updated here.
    event = st.session_state.get("port-map", {})
    picked = event.get("selection", {}).get("objects", {}).get("vessels", [])
    if picked and picked[0].get("mmsi") in st.session_state.get("mapped-mmsis", []):
        st.session_state["detail-mmsi"] = picked[0]["mmsi"]
        open_vessel_details()


def open_vessel_details():
    # A new key reopens the expander even if the user previously collapsed it.
    st.session_state["detail-open-request"] = st.session_state.get("detail-open-request", 0) + 1


def vessel_card(df, now, history_loader, plan=()):
    rows = df.astype(object).where(df.notna(), None)
    vessels = {r["mmsi"]: r for _, r in rows.iterrows()}
    selected = st.session_state.get("detail-mmsi")
    if selected is not None and selected not in vessels:
        st.session_state["detail-mmsi"] = None
    st.html(vessel_detail_css(vessels[selected]["state"] if selected in vessels else None))
    request = st.session_state.get("detail-open-request", 0)
    title = "Vessel details" + (f" · {vessel_name(vessels[selected]['name'], selected)}" if selected in vessels else "")
    with st.expander(title, expanded=selected in vessels, key=f"vessel-panel-{request}"):
        with st.container(border=False, key="vessel-detail"):
            options = sorted(vessels, key=lambda m: (str(vessel_name(vessels[m]["name"], m)), m))
            mmsi = st.selectbox("Vessel", options, index=None, key="detail-mmsi", label_visibility="collapsed",
                                on_change=open_vessel_details,
                                placeholder="Search vessel name or MMSI", filter_mode="contains",
                                icon=":material/search:",
                                format_func=lambda m: f"{vessel_name(vessels[m]['name'], m)} · MMSI {m}")
            if mmsi is None:
                st.caption("Select a vessel on the map to see its route, observed wait and berth-plan eligibility.")
                return
            v = vessels[mmsi]
            left, right = st.columns([1, 1.6], gap="large")
            with left:
                st.subheader(vessel_name(v["name"], mmsi))
                st.caption(f"MMSI {mmsi} · {v['type']} · " + (
                    f"{v['length']:g} m" if v["length"] is not None else "length unknown"))
                cols = st.columns(3)
                cols[0].metric("Current state", LABELS[v["state"]], border=False)
                elapsed = max(timedelta(0), v["ts"] - v["since"])
                observed = v.get("since_seen") is True
                cols[1].metric("Observed wait" if v["state"] == "anchored" else "Time in state",
                               ("" if observed else "≥ ") + fmt(elapsed), border=False,
                               help="Measured through the latest received position. ≥ means the state began before observation.")
                speed = v.get("sog")
                cols[2].metric("Speed", "Unknown" if speed is None else f"{speed:.1f} kn", border=False)
                age = max(timedelta(0), now - v["ts"])
                st.caption(f"Last signal · {fmt_age(age)}" + (" · delayed" if age > timedelta(minutes=config.LIVE_MINUTES) else ""))
                st.caption(f"In this state since {local(v['since']):%d %b %H:%M %Z}" + (
                               "" if observed else " · ≥ start not observed"))
                proposal = next((p for p in plan if p["mmsi"] == mmsi), None)
                if proposal:
                    moors = local(now + timedelta(minutes=proposal["start"]))
                    st.caption("BERTH PROPOSAL · ESTIMATED")
                    st.markdown(f"**{proposal['berth']}**")
                    berth_cols = st.columns(2)
                    berth_cols[0].metric("Proposed mooring", f"{moors:%H:%M}", help=f"{moors:%a %d %b %H:%M %Z}")
                    berth_cols[1].metric("Extra wait", fmt(timedelta(minutes=proposal["wait"])))
                else:
                    reason = plan_reason({**v, "category": v["type"]}, config.BERTHS)
                    st.info(reason or "Eligible for the berth plan.", icon=":material/anchor:")
            with right:
                hours = st.radio("Route history", [3, 24, 7 * 24], horizontal=True,
                                 format_func=lambda h: "7 days" if h == 168 else f"{h} hours", key="detail-hours")
            positions = history_loader(int(mmsi), hours)
            paths, events = observations(positions)
            tracks = [{"mmsi": mmsi, "paths": paths}] if paths else []
            stamps = [p["ts"] for p in positions]
            first, last = (min(stamps), max(stamps)) if stamps else (None, None)
            span = last - first if stamps else timedelta(0)
            with right:
                st.pydeck_chart(port_map(rows[rows["mmsi"] == mmsi], (tracks, {mmsi: span})), height=300)
                st.caption(f"Sampled route · gaps over {config.STALE_MINUTES} min are not joined · latest known position")
                if not positions:
                    st.caption("No sampled positions available in this window.")
            with st.expander("Position & data quality", icon=":material/info:"):
                position_col, quality_col = st.columns(2)
                with position_col:
                    st.caption("LATITUDE, LONGITUDE · copy using the icon")
                    st.code(f"{v['lat']:.5f}, {v['lon']:.5f}", language=None)
                    st.caption(f"Last position: {local(v['ts']):%d %b %Y %H:%M:%S %Z}")
                    st.caption("AIS DESTINATION")
                    st.text(v.get("destination") or "Not received")
                    st.caption("Declared by the vessel · may be outdated")
                with quality_col:
                    st.caption("State start observed." if observed else
                               "State start not observed: the displayed duration is a lower bound.")
                    gaps = sum(e["kind"] == "Seen after data gap" for e in events)
                    st.caption(f"{len(positions)} sampled positions · {gaps} data gaps in this window")
                    if positions:
                        st.caption(f"Coverage: {local(first):%d %b %H:%M} to {local(last):%d %b %H:%M %Z}")
            if events:
                with st.expander(f"Observed state history · {len(events)} records", icon=":material/timeline:"):
                    st.dataframe(pd.DataFrame([{
                        "time": f"{local(e['ts']):%d %b %H:%M:%S %Z}",
                        "state": LABELS.get(e["state"], "Unknown"), "observation": e["kind"],
                    } for e in events]), hide_index=True, width="stretch")
                    st.caption("Raw sampled states can include AIS noise. First sightings and returns after a gap "
                               "do not establish when a state began.")
