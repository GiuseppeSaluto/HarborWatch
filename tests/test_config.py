"""Tests for config.py: the hand-written berths list (spec section 6, "Accosti", and section 10:
coherence of berths.json), and the reason behind LIVE_MINUTES.

Plain values (thresholds, intervals) are not pinned here: changing one on purpose is not a bug.

No external services needed.
"""

import config

COMMERCIAL = {"cargo", "tanker", "passenger"}


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


def test_live_minutes_covers_ais_delay_plus_flush():
    # Spec section 13: LIVE_MINUTES = AIS delay plus FLUSH_SECONDS, with margin.
    # Measured AIS delay, 90th percentile: 10.7 s (spec section 3).
    assert config.LIVE_MINUTES * 60 > 10.7 + config.FLUSH_SECONDS
