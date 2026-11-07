import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest


@pytest.fixture
def dashboard(monkeypatch):
    from datetime import timedelta

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

    def berth_plan_data(df, now):
        st.session_state.planned = df.mmsi.tolist()
        return {"plan": []}

    monkeypatch.setattr(app, "status_row", lambda now: True)
    monkeypatch.setattr(app, "load_states", load_states)
    monkeypatch.setattr(app, "recent_tracks", recent_tracks)
    monkeypatch.setattr(app, "get_db", lambda: None)
    monkeypatch.setattr(app, "congestion_history", lambda: pd.DataFrame())
    monkeypatch.setattr(app, "berth_plan_data", berth_plan_data)
    monkeypatch.setattr(app, "berth_plan", lambda data, now: None)

    def render_dashboard():
        import app
        app.dashboard()

    return AppTest.from_function(render_dashboard, default_timeout=15).run()


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


def run_snippet(setup):
    """Exercise the real renderers with local fixtures, without live services."""
    source = (Path(__file__).parents[1] / "src/app.py").read_text()
    return AppTest.from_string(source.replace("\n    dashboard()\n", setup), default_timeout=15).run()


def test_empty_plan_still_explains_excluded_vessels():
    app = run_snippet("""
now = pd.Timestamp(datetime.now(UTC))
berth_plan(dict(plan=[], berths=[], unfit=[dict(mmsi=1, category='cargo', length=None)],
    names={1: 'Unknown length'}, service=config.SERVICE_MINUTES,
    counts={c: 0 for c in config.SERVICE_MINUTES}, window=0), now)
""")
    assert not app.exception
    assert any("Unknown length" in c.value and "length unknown" in c.value for c in app.caption)
