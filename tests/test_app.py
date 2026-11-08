import json

import pytest
from streamlit.testing.v1 import AppTest


@pytest.fixture
def dashboard(monkeypatch):
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

    monkeypatch.setattr(app, "status_row", lambda now: True)
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
