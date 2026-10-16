import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest


@pytest.fixture
def dashboard():
    source = (Path(__file__).parents[1] / "src/app.py").read_text()
    setup = '''
def status_row(now):
    return True

def load_states(db, now):
    return pd.DataFrame([
        dict(mmsi=i, state=state, ship_type=kind, name=str(i), length=150,
             since=now, since_seen=True, ts=now, sog=0, lon=8.85, lat=44.4)
        for i, state, kind in [(1, "at_berth", 70), (2, "anchored", 80),
                               (3, "underway", 70), (4, "anchored", 36)]])

def recent_tracks():
    return ([dict(mmsi=i, paths=[[[8.85, 44.4], [8.86, 44.41]]]) for i in range(1, 5)],
            {i: timedelta(minutes=10) for i in range(1, 5)})

get_db = lambda: None
congestion_history = lambda: pd.DataFrame()
def berth_plan(df, now):
    st.session_state.planned = df.mmsi.tolist()
dashboard()
'''
    return AppTest.from_string(source.replace("\ndashboard()\n", setup)).run()


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
