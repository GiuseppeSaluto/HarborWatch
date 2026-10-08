"""Black-box tests for config.py: the values the spec fixes, and the berths list
(spec section 6, "Accosti", and section 10: coherence of berths.json).

No external services needed.
"""

import config

COMMERCIAL = {"cargo", "tanker", "passenger"}


def test_speed_threshold_is_half_a_knot():
    assert config.SPEED_THRESHOLD_KN == 0.5


def test_quay_distance_is_100_m():
    assert config.QUAY_DISTANCE_M == 100


def test_stale_minutes_is_30():
    assert config.STALE_MINUTES == 30


def test_flush_seconds_is_10():
    assert config.FLUSH_SECONDS == 10


def test_sample_seconds_is_60():
    assert config.SAMPLE_SECONDS == 60


def test_min_stays_is_5():
    assert config.MIN_STAYS == 5


def test_stay_window_factor_is_2():
    assert config.STAY_WINDOW_FACTOR == 2


def test_default_service_minutes_per_category():
    # Assumption: SERVICE_MINUTES is a dict category -> minutes (spec: "cargo 24 h,
    # tanker 36 h, passenger 12 h"); the name "minutes" implies the unit.
    assert config.SERVICE_MINUTES["cargo"] == 24 * 60
    assert config.SERVICE_MINUTES["tanker"] == 36 * 60
    assert config.SERVICE_MINUTES["passenger"] == 12 * 60


# --- berths (config.BERTHS, loaded from data/berths.json) -------------------------
# Assumption: each berth is a dict with keys "name", "zone", "length" (metres),
# "categories" (list of str) and "source"; the spec lists these attributes
# ("nome, zona, lunghezza, categorie accettate, un punto per zona e la fonte")
# without naming the JSON keys.


def _label(berth):
    return f"{berth.get('zone', '')} {berth.get('name', '')}".lower()


def test_berths_not_empty():
    assert len(config.BERTHS) > 0


def test_berth_names_are_unique():
    names = [b["name"] for b in config.BERTHS]
    assert len(names) == len(set(names))


def test_every_berth_has_positive_length():
    for b in config.BERTHS:
        assert b["length"] > 0, b


def test_every_berth_accepts_only_known_categories():
    for b in config.BERTHS:
        assert b["categories"], b
        assert set(b["categories"]) <= COMMERCIAL, b


def test_every_berth_declares_a_source():
    for b in config.BERTHS:
        assert b.get("source"), b


def test_pra_has_four_berths_totalling_1675_m():
    pra = [b for b in config.BERTHS if "pra" in _label(b)]
    assert len(pra) == 4
    assert sum(b["length"] for b in pra) == 1675


def test_sech_is_split_300_plus_226():
    sech = [b for b in config.BERTHS if "sech" in _label(b)]
    assert sorted(b["length"] for b in sech) == [226, 300]


def test_multedo_has_six_tanker_berths_between_230_and_330_m():
    multedo = [b for b in config.BERTHS if "multedo" in _label(b)]
    assert len(multedo) == 6
    for b in multedo:
        assert 230 <= b["length"] <= 330, b
        assert "tanker" in b["categories"], b


def test_container_terminals_do_not_accept_tankers():
    # Spec: "a tanker does not go to the container terminal".
    for b in config.BERTHS:
        label = _label(b)
        if "pra" in label or "sech" in label:
            assert "tanker" not in b["categories"], b


def test_cruise_berths_accept_passengers():
    cruise = [b for b in config.BERTHS
              if "mille" in _label(b) or "doria" in _label(b)]
    assert cruise, "expected Ponte dei Mille / Ponte Andrea Doria berths"
    for b in cruise:
        assert "passenger" in b["categories"], b


def test_track_hours_is_3():
    # Spec section 8 point 5: tracks cover the last TRACK_HOURS (3) hours.
    assert config.TRACK_HOURS == 3


def test_track_min_move_is_500_m():
    # Spec section 8 point 5: only vessels that moved at least 500 m are drawn.
    assert config.TRACK_MIN_MOVE_M == 500
