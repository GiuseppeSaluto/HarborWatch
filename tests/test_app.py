import json

import pytest
from streamlit.testing.v1 import AppTest


# Replay data (spec section 8 point 11, "Nomi per i test"): vessels 777, 778 and 779 exist only
# in the replay positions; 777 and 779 are commercial in vessel_static(), 778 has no static
# data. Positions every minute over half an hour of a past day, so wherever the time cursor
# lands between the first and the last position, each vessel has one at most a minute before t.
REPLAY_STATIC = [dict(mmsi=777, name="Replay one", length=200, destination="GENOA", ship_type=70),
                 dict(mmsi=779, name="Replay two", length=180, destination="GENOA", ship_type=80)]


def stored_positions():
    from datetime import UTC, datetime, timedelta

    base = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)
    return [dict(mmsi=mmsi, ts=base + timedelta(minutes=m), sog=0.0, state="anchored",
                 location={"type": "Point", "coordinates": [8.85, 44.4]})
            for mmsi in (777, 778, 779) for m in range(31)]


@pytest.fixture
def status_calls():
    """Every call to the patched status_row lands here."""
    return []


@pytest.fixture
def open_dashboard(monkeypatch, status_calls):
    """Patch the loaders by name and run the dashboard; `replay` replaces replay_positions()."""
    from datetime import UTC, datetime, timedelta

    import pandas as pd
    import streamlit as st

    import app

    def load_states(db, now):
        return pd.DataFrame([
            dict(mmsi=i, state=state, ship_type=kind, name=str(i), length=150,
                 since=now, since_seen=True, ts=now, sog=0, lon=8.85, lat=44.4)
            for i, state, kind in [(1, "at_berth", 70), (2, "anchored", 80),
                                   (3, "underway", 70), (4, "anchored", 36)]])

    def recent_tracks():
        return ([dict(mmsi=i, paths=[[[8.85, 44.4], [8.86, 44.41]]]) for i in range(1, 5)],
                {i: timedelta(minutes=10) for i in range(1, 5)})

    def vessel_history(mmsi, hours):
        now = datetime.now(UTC)
        return [dict(ts=now - timedelta(minutes=2), state="underway", location={"coordinates": [8.84, 44.4]}),
                dict(ts=now - timedelta(minutes=1), state="anchored", location={"coordinates": [8.85, 44.4]})]

    def berth_plan_data(df, now):
        st.session_state.planned = df.mmsi.tolist()
        return {"plan": [dict(mmsi=2, berth="Test berth", start=30, wait=30)]}

    def open_(replay=None):
        positions = stored_positions() if replay is None else replay
        monkeypatch.setattr(app, "status_row", lambda now: status_calls.append(now) or True)
        # Not in app.py before the replay exists: raising=False keeps the Live tests runnable.
        monkeypatch.setattr(app, "replay_positions", lambda: positions, raising=False)
        monkeypatch.setattr(app, "vessel_static", lambda: REPLAY_STATIC, raising=False)
        monkeypatch.setattr(app, "load_states", load_states)
        monkeypatch.setattr(app, "recent_tracks", recent_tracks)
        monkeypatch.setattr(app, "get_db", lambda: None)
        monkeypatch.setattr(app, "congestion_history", lambda: pd.DataFrame())
        monkeypatch.setattr(app, "vessel_history", vessel_history)
        monkeypatch.setattr(app, "berth_plan_data", berth_plan_data)
        monkeypatch.setattr(app, "berth_plan", lambda data, now: None)

        def render_dashboard():
            import app
            app.dashboard()

        return AppTest.from_function(render_dashboard, default_timeout=15).run()

    return open_


@pytest.fixture
def dashboard(open_dashboard):
    return open_dashboard()


def map_ids(app, layer):
    chart = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    data = next(item["data"] for item in chart["layers"] if item["@@type"] == layer)
    return {row["title"] for row in data}


def test_state_filter_hides_vessels_and_tracks_only(dashboard):
    dashboard.button(key="map-anchored").click().run()
    assert not dashboard.exception
    assert map_ids(dashboard, "IconLayer") == {"1", "3"}
    assert map_ids(dashboard, "PathLayer") == {"1", "3"}
    assert dashboard.metric[1].value == "1"
    assert dashboard.session_state.planned == [1, 2, 3, 4]
    dashboard.button(key="map-anchored").click().run()
    assert map_ids(dashboard, "IconLayer") == {"1", "2", "3"}


def test_state_filters_combine_with_commercial_toggle(dashboard):
    dashboard.button(key="map-anchored").click().run()
    dashboard.toggle[0].set_value(False).run()
    assert map_ids(dashboard, "IconLayer") == {"1", "3"}
    dashboard.button(key="map-anchored").click().run()
    assert map_ids(dashboard, "IconLayer") == {"1", "2", "3", "4"}


def test_all_states_can_be_hidden(dashboard):
    for state in ("at_berth", "anchored", "underway"):
        dashboard.button(key=f"map-{state}").click().run()
    assert not dashboard.exception
    assert map_ids(dashboard, "IconLayer") == set()
    assert map_ids(dashboard, "PathLayer") == set()

@pytest.fixture
def run_snippet(monkeypatch):
    """Run a renderer through the same patched entry point as the dashboard tests."""
    import app

    def run(render):
        monkeypatch.setattr(app, "dashboard", render)

        def render_dashboard():
            import app
            app.dashboard()

        return AppTest.from_function(render_dashboard, default_timeout=15).run()

    return run


def click_map(dashboard, picked):
    # AppTest has no map-click helper; send the payload the chart would send.
    states = dashboard._tree.get_widget_states()
    states.widgets.add(id=dashboard.get("deck_gl_json_chart")[0].proto.id,
                       string_value=json.dumps({"selection": {"indices": {}, "objects": picked}}))
    dashboard._run(states)
    assert not dashboard.exception


def test_vessel_search_opens_card_with_proposal_and_history(dashboard):
    selector = dashboard.selectbox(key="detail-mmsi")
    assert "MMSI" in selector.proto.placeholder
    assert any("MMSI 2" in label for label in selector.options)
    assert not dashboard.expander[0].proto.expanded
    assert len(dashboard.get("deck_gl_json_chart")) == 1
    selector.select(2).run()
    assert not dashboard.exception
    assert dashboard.expander[0].proto.expanded
    assert all(not e.proto.expanded for e in dashboard.expander[1:])
    assert len(dashboard.get("deck_gl_json_chart")) == 2
    assert any(m.label == "Speed" and m.value == "0.0 kn" for m in dashboard.metric)
    assert any("Test berth" in m.value for m in dashboard.markdown)
    assert any(m.label == "Extra wait" and m.value == "0h 30m" for m in dashboard.metric)
    assert any("Last signal" in c.value for c in dashboard.caption)
    assert "State change observed" in dashboard.dataframe[-1].value["observation"].tolist()


def test_vessel_card_includes_noncommercial_vessels(dashboard):
    dashboard.selectbox(key="detail-mmsi").select(4).run()
    assert not dashboard.exception
    assert any("only commercial vessels" in c.value for c in dashboard.info)


def test_missing_vessel_fields_and_empty_history_stay_unknown(run_snippet):
    def render():
        from datetime import UTC, datetime, timedelta

        import pandas as pd
        import streamlit as st

        from app import vessel_card

        vessel_history = lambda mmsi, hours: []
        now = pd.Timestamp(datetime.now(UTC))
        st.session_state['detail-mmsi'] = 1
        vessel_card(pd.DataFrame([dict(mmsi=1, state='anchored', type='cargo', name=None,
            destination=None, length=None, since=now - timedelta(hours=2), since_seen=False,
            ts=now - timedelta(minutes=5), sog=None, lon=8.85, lat=44.4)]), now, vessel_history)

    app = run_snippet(render)
    assert not app.exception
    assert app.metric[1].value.startswith("≥")
    assert any(m.label == "Speed" and m.value == "Unknown" for m in app.metric)
    assert any("length unknown" in c.value for c in app.info)
    assert any("No sampled positions" in c.value for c in app.caption)
    assert any("lower bound" in c.value for c in app.caption)
    assert any(t.value == "Not received" for t in app.text)
    assert any("44.40000, 8.85000" in c.value for c in app.code)


def test_map_exposes_selectable_vessels_with_stable_layer_ids(dashboard):
    chart = dashboard.get("deck_gl_json_chart")[0].proto
    layers = json.loads(chart.json)["layers"]
    assert all("id" in layer for layer in layers)
    ships = next(layer for layer in layers if layer["id"] == "vessels")
    assert {v["mmsi"] for v in ships["data"]} == {1, 2, 3}
    assert chart.selection_mode


def test_map_click_opens_card_and_leaves_dropdown_usable(dashboard):
    click_map(dashboard, {"vessels": [{"mmsi": 2}]})
    assert dashboard.selectbox(key="detail-mmsi").value == 2
    assert any(h.value == "2" for h in dashboard.subheader)
    assert len(dashboard.get("deck_gl_json_chart")) == 2
    assert any(e.label.startswith("Observed state history") and not e.proto.expanded for e in dashboard.expander)
    dashboard.selectbox(key="detail-mmsi").select(3).run()
    assert not dashboard.exception
    assert dashboard.selectbox(key="detail-mmsi").value == 3


@pytest.mark.parametrize("picked", [
    {}, {"berth-areas": [{"mmsi": 2}]}, {"vessels": [{"mmsi": 999999999}]},
])
def test_non_vessel_or_stale_map_selection_preserves_card(dashboard, picked):
    dashboard.selectbox(key="detail-mmsi").select(3).run()
    click_map(dashboard, picked)
    assert dashboard.selectbox(key="detail-mmsi").value == 3


def test_empty_plan_still_explains_excluded_vessels(run_snippet):
    def render():
        from datetime import UTC, datetime

        import pandas as pd

        import config
        from app import berth_plan

        now = pd.Timestamp(datetime.now(UTC))
        berth_plan(dict(plan=[], berths=[], unfit=[dict(mmsi=1, category='cargo', length=None)],
            names={1: 'Unknown length'}, service=config.SERVICE_MINUTES,
            counts={c: 0 for c in config.SERVICE_MINUTES}, window=0), now)

    app = run_snippet(render)
    assert not app.exception
    assert any("Unknown length" in c.value and "length unknown" in c.value for c in app.caption)


def card_texts(app):
    return [e.value for kind in ("info", "caption", "markdown", "warning", "error", "success")
            for e in getattr(app, kind)]


def test_vessel_card_shows_plan_reason_for_vessel_outside_the_plan(dashboard):
    import config
    import optimize

    dashboard.selectbox(key="detail-mmsi").select(1).run()
    assert not dashboard.exception
    reason = optimize.plan_reason(dict(category="cargo", state="at_berth", length=150),
                                  config.BERTHS)
    assert reason is not None
    assert any(reason in text for text in card_texts(dashboard))


def test_selection_resets_when_vessel_is_no_longer_visible(dashboard, monkeypatch):
    import app

    dashboard.selectbox(key="detail-mmsi").select(2).run()
    assert dashboard.selectbox(key="detail-mmsi").value == 2
    load_states = app.load_states
    monkeypatch.setattr(app, "load_states",
                        lambda db, now: load_states(db, now).query("mmsi != 2"))
    dashboard.run()
    assert not dashboard.exception
    assert dashboard.selectbox(key="detail-mmsi").value is None
    assert not any(h.value == "2" for h in dashboard.subheader)


# --- Live / Replay (spec section 8 point 11) ---------------------------------------------
# The switch has key `view-mode` and options `Live` / `Replay`; the widget type is not
# specified, so the tests switch through session_state and find it by key in the tree.


def widget(app, key):
    def walk(node):
        yield node
        if isinstance(getattr(node, "children", None), dict):
            for child in node.children.values():
                yield from walk(child)

    return next((n for n in walk(app._tree) if getattr(n, "key", None) == key), None)


def replay_banners(app):
    # `Replay · <t>` with a Material icon: the icon may come as a leading `:material/...:`.
    import re

    return [text for text in card_texts(app)
            if re.sub(r"^:material/[a-z_]+:\s*", "", text.strip()).startswith("Replay ·")]


def vessel_ids(app):
    layers = json.loads(app.get("deck_gl_json_chart")[0].proto.json)["layers"]
    return {v["mmsi"] for v in next(layer for layer in layers if layer["id"] == "vessels")["data"]}


def switch_to_replay(app):
    app.session_state["view-mode"] = "Replay"
    app.run()
    assert not app.exception


def test_view_mode_defaults_to_live_and_keeps_the_live_page(dashboard, status_calls):
    assert not dashboard.exception
    assert widget(dashboard, "view-mode") is not None
    assert dashboard.session_state["view-mode"] == "Live"
    assert status_calls
    assert not replay_banners(dashboard)
    assert vessel_ids(dashboard) == {1, 2, 3}


def test_replay_shows_banner_and_no_status_panel(dashboard, status_calls):
    status_calls.clear()
    switch_to_replay(dashboard)
    assert replay_banners(dashboard)
    assert status_calls == []


def test_replay_map_and_counters_come_from_states_at_with_static_data(dashboard):
    live_anchored = dashboard.metric[1].value
    switch_to_replay(dashboard)
    # 777 and 779 only exist in the replay positions; vessels 1-3 only in load_states.
    # Both are commercial only through vessel_static(): without the join they would not count.
    # Step 2: the map is the replay-map component, so its data replaces the pydeck layers.
    data = replay_data(dashboard)
    assert {777, 779} <= {v["mmsi"] for v in data["vessels"]}
    assert not {1, 2, 3} & {v["mmsi"] for v in data["vessels"]}
    static = {int(mmsi): value for mmsi, value in data["static"].items()}
    assert "Replay one" in json.dumps(static[777]) and "Replay two" in json.dumps(static[779])
    assert dashboard.metric[1].value == "2" != live_anchored
    # 778 has no static data: still a vessel at t (ship_type 0, the rest empty), so it can
    # be picked in the vessel search like any non-commercial vessel.
    assert any("MMSI 778" in label for label in dashboard.selectbox(key="detail-mmsi").options)


def test_without_positions_replay_cannot_be_used_and_page_stays_live(open_dashboard, status_calls):
    app = open_dashboard(replay=[])
    assert not app.exception
    switch = widget(app, "view-mode")
    if switch is not None:
        options = [getattr(o, "content", o) for o in switch.proto.options]
        assert switch.proto.disabled or "Replay" not in options
    assert status_calls
    assert not replay_banners(app)
    assert vessel_ids(app) == {1, 2, 3}
    assert any("replay" in text.lower() for text in card_texts(app))


# --- Replay step 2: the replay-map component (spec section 8 point 11, "Nomi per i test") --
# A Streamlit v2 component shows up in the AppTest tree as an element of type
# `bidi_component` with the widget key; the `data` it was given is the JSON in `proto.json`.
# Its state is driven like the browser would with session_state["replay-map"] = {...}.


def replay_component(app):
    return next((node for node in app.get("bidi_component") if node.key == "replay-map"), None)


def replay_data(app):
    component = replay_component(app)
    assert component is not None, "no replay-map component"
    assert component.proto.json, "the component data is expected as JSON"
    return json.loads(component.proto.json)


def epoch(moment):
    return int(moment.timestamp())


def step():
    import config
    from datetime import timedelta

    return timedelta(minutes=config.REPLAY_STEP_MINUTES)


# Aligned to every REPLAY_STEP_MINUTES that divides an hour, like stored_positions().
REPLAY_BASE = stored_positions()[0]["ts"]


def anchored_every_minute(mmsi, start, end):
    from datetime import timedelta

    count = int((end - start) / timedelta(minutes=1)) + 1
    return [dict(mmsi=mmsi, ts=start + timedelta(minutes=m), sog=0.0, state="anchored",
                 location={"type": "Point", "coordinates": [8.85, 44.4]}) for m in range(count)]


def local_time(moment):
    from zoneinfo import ZoneInfo

    import config

    return moment.astimezone(ZoneInfo(config.TIMEZONE)).strftime("%H:%M")


def test_replay_map_is_the_component_not_the_pydeck_chart(dashboard):
    assert replay_component(dashboard) is None
    switch_to_replay(dashboard)
    assert replay_component(dashboard) is not None
    assert len(dashboard.get("deck_gl_json_chart")) == 0
    # Map and step-1 cursor become one component: the replay-t slider is gone.
    assert widget(dashboard, "replay-t") is None


def test_component_t_decides_counters_and_banner(open_dashboard):
    # 777 anchored for three steps, 779 only during the last step: at the last step both count.
    last = REPLAY_BASE + 3 * step()
    app = open_dashboard(replay=anchored_every_minute(777, REPLAY_BASE, last)
                         + anchored_every_minute(779, REPLAY_BASE + 2 * step(), last))
    switch_to_replay(app)
    assert app.metric[1].value == "2"
    assert any(local_time(last) in banner for banner in replay_banners(app))
    earlier = REPLAY_BASE + step()
    app.session_state["replay-map"] = {"t": epoch(earlier)}
    app.run()
    assert not app.exception
    assert app.metric[1].value == "1"
    banners = replay_banners(app)
    assert any(local_time(earlier) in banner for banner in banners)
    assert not any(local_time(last) in banner for banner in banners)


def test_component_pick_opens_vessel_card_and_leaves_dropdown_usable(dashboard):
    last = max(p["ts"] for p in stored_positions())
    switch_to_replay(dashboard)
    dashboard.session_state["replay-map"] = {"t": epoch(last), "picked": 777}
    dashboard.run()
    assert not dashboard.exception
    assert dashboard.selectbox(key="detail-mmsi").value == 777
    assert any(h.value == "Replay one" for h in dashboard.subheader)
    dashboard.selectbox(key="detail-mmsi").select(779).run()
    assert not dashboard.exception
    assert dashboard.selectbox(key="detail-mmsi").value == 779


def shown_names(data):
    """Every string in `shown`, whatever its layout (list, dict of lists, ...)."""
    def walk(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for key, item in value.items():
                if item is True:
                    yield key
                elif item is not False:
                    yield from walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from walk(item)

    return set(walk(data["shown"]))


def test_component_data_follows_commercial_toggle_and_state_filters(dashboard):
    from classify import ship_category

    states = {"at_berth", "anchored", "underway"}
    categories = {ship_category(code) for code in range(0, 101)}
    commercial = {ship_category(code) for code in (60, 70, 80)}
    switch_to_replay(dashboard)
    shown = shown_names(replay_data(dashboard))
    assert shown & states == states
    assert shown & categories == commercial
    dashboard.button(key="map-anchored").click().run()
    assert shown_names(replay_data(dashboard)) & states == states - {"anchored"}
    next(t for t in dashboard.toggle if "ommercial" in t.label).set_value(False).run()
    shown = shown_names(replay_data(dashboard))
    assert shown & categories == categories
    assert shown & states == states - {"anchored"}


def test_component_data_has_slider_limits_and_vessel_series(open_dashboard):
    # First position inside the first step, last inside the fourth: the cursor runs from the
    # first rounded down to the last rounded down. Alternating states, so thinning keeps all.
    offsets = [0.3, 1.2, 2.5, 3.7]
    positions = [dict(mmsi=777, ts=REPLAY_BASE + step() * k, sog=float(i),
                      state=("anchored", "underway")[i % 2],
                      location={"type": "Point", "coordinates": [8.85 + i / 1000, 44.4 + i / 1000]})
                 for i, k in enumerate(offsets)]
    app = open_dashboard(replay=positions)
    switch_to_replay(app)
    data = replay_data(app)
    assert (data["start"], data["end"]) == (epoch(REPLAY_BASE), epoch(REPLAY_BASE + 3 * step()))
    vessel = next(v for v in data["vessels"] if v["mmsi"] == 777)
    assert [round(data["t0"] + s) for s in vessel["times"]] == [epoch(p["ts"]) for p in positions]
    assert [[round(c, 5) for c in point] for point in vessel["positions"]] == [
        [round(c, 5) for c in p["location"]["coordinates"]] for p in positions]
    assert vessel["states"] == [p["state"] for p in positions]
    assert vessel["sog"] == [p["sog"] for p in positions]
