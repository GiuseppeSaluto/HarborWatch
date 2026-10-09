"""Black-box tests for diagnostics.diagnose (spec section 8 point 6, section 10
and section 13).

diagnose is pure: no network, no MongoDB. probe_stream uses the network and is
deliberately never called here; only its declared async nature is checked.
Only the returned code is asserted, never the message text.
"""

import inspect
from datetime import timedelta

import pytest

import config
import diagnostics

CODES = {"atlas", "live", "unknown", "access", "ingestion", "coverage", "aisstream"}


def live_minutes():
    return timedelta(minutes=config.LIVE_MINUTES)


FRESH = timedelta(seconds=30)
OLD = timedelta(hours=2)


# Probe results in the shape of spec section 13: {confirmed, world, port}.
# Assumption: probes built here also carry "seconds", as probe_stream returns
# it and the dashboard is expected to pass that result straight to diagnose;
# diagnose must tolerate the extra key (one test checks the bare shape too).
def probe(confirmed=True, world=0, port=0, seconds=20):
    return {"confirmed": confirmed, "world": world, "port": port, "seconds": seconds}


NOT_CONFIRMED = probe(confirmed=False, world=0, port=0)
PORT_DATA = probe(world=5000, port=12)
WORLD_ONLY = probe(world=5000, port=0)
SILENT = probe(world=0, port=0)

ALL_PROBES = [None, NOT_CONFIRMED, PORT_DATA, WORLD_ONLY, SILENT]
ALL_AGES = [None, timedelta(0), FRESH, OLD]


def code(atlas_ok, position_age, probe_result):
    result = diagnostics.diagnose(atlas_ok, position_age, probe_result)
    assert isinstance(result, tuple) and len(result) == 2
    return result[0]


# --- one test per code ----------------------------------------------------------------

def test_atlas_down_is_atlas():
    assert code(False, None, None) == "atlas"


def test_fresh_position_is_live():
    assert code(True, FRESH, None) == "live"


def test_old_data_without_probe_is_unknown():
    assert code(True, OLD, None) == "unknown"


def test_unconfirmed_subscription_is_access():
    assert code(True, OLD, NOT_CONFIRMED) == "access"


def test_port_positions_in_probe_is_ingestion():
    assert code(True, OLD, PORT_DATA) == "ingestion"


def test_world_but_no_port_positions_is_coverage():
    assert code(True, OLD, WORLD_ONLY) == "coverage"


def test_nothing_even_from_the_world_is_aisstream():
    assert code(True, OLD, SILENT) == "aisstream"


def test_single_port_position_is_enough_for_ingestion():
    assert code(True, OLD, probe(world=1, port=1)) == "ingestion"


def test_single_world_position_is_enough_for_coverage():
    assert code(True, OLD, probe(world=1, port=0)) == "coverage"


def test_probe_without_seconds_key_is_accepted():
    bare = {"confirmed": True, "world": 300, "port": 0}
    assert code(True, OLD, bare) == "coverage"


# --- no positions at all ------------------------------------------------------------------

def test_no_positions_is_never_live():
    assert code(True, None, None) == "unknown"


@pytest.mark.parametrize("probe_result,expected", [
    (NOT_CONFIRMED, "access"), (PORT_DATA, "ingestion"),
    (WORLD_ONLY, "coverage"), (SILENT, "aisstream"),
])
def test_no_positions_follows_the_probe(probe_result, expected):
    assert code(True, None, probe_result) == expected


# --- LIVE_MINUTES boundary ----------------------------------------------------------------

def test_age_zero_is_live():
    assert code(True, timedelta(0), None) == "live"


def test_age_exactly_live_minutes_is_live():
    assert code(True, live_minutes(), None) == "live"


def test_age_one_second_over_live_minutes_is_not_live():
    assert code(True, live_minutes() + timedelta(seconds=1), None) == "unknown"


def test_age_one_second_under_live_minutes_is_live():
    assert code(True, live_minutes() - timedelta(seconds=1), None) == "live"


def test_age_just_over_live_minutes_uses_probe():
    assert code(True, live_minutes() + timedelta(seconds=1), SILENT) == "aisstream"


# --- precedence ---------------------------------------------------------------------------

@pytest.mark.parametrize("position_age", ALL_AGES)
@pytest.mark.parametrize("probe_result", ALL_PROBES)
def test_atlas_down_wins_over_everything(position_age, probe_result):
    assert code(False, position_age, probe_result) == "atlas"


@pytest.mark.parametrize("probe_result", ALL_PROBES)
def test_live_data_ignores_the_probe(probe_result):
    assert code(True, FRESH, probe_result) == "live"


def test_live_data_ignores_a_failed_probe_at_the_boundary():
    assert code(True, live_minutes(), NOT_CONFIRMED) == "live"


def test_unconfirmed_wins_over_counts():
    # A probe that was not confirmed but still counted something is "access".
    assert code(True, OLD, probe(confirmed=False, world=800, port=5)) == "access"


def test_unconfirmed_wins_over_world_only_counts():
    assert code(True, OLD, probe(confirmed=False, world=800, port=0)) == "access"


def test_port_positions_win_over_world_positions():
    assert code(True, OLD, probe(world=10_000, port=3)) == "ingestion"


# --- result shape ---------------------------------------------------------------------------

@pytest.mark.parametrize("atlas_ok", [True, False])
@pytest.mark.parametrize("position_age", ALL_AGES)
@pytest.mark.parametrize("probe_result", ALL_PROBES)
def test_always_returns_a_known_code_and_a_message(atlas_ok, position_age, probe_result):
    c, message = diagnostics.diagnose(atlas_ok, position_age, probe_result)
    assert c in CODES
    assert isinstance(message, str) and message.strip()


def test_every_code_is_reachable():
    seen = {code(a, age, p) for a in (True, False) for age in ALL_AGES for p in ALL_PROBES}
    assert seen == CODES


def test_diagnose_does_not_modify_the_probe():
    p = probe(world=42, port=0)
    before = dict(p)
    diagnostics.diagnose(True, OLD, p)
    assert p == before


# --- probe_stream (never called: it opens a network connection) ------------------------------

def test_probe_stream_is_async():
    # Spec section 13: probe_stream(seconds) is async. Only inspected, never awaited.
    assert inspect.iscoroutinefunction(diagnostics.probe_stream)
