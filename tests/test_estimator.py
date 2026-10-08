from datetime import datetime, timedelta, timezone

import pytest

from custom_components.bambu_timeline.estimator import (
    PRINTER_PAUSED,
    PRINTER_PREPARING,
    PRINTER_RUNNING,
    STATUS_ACTIVE,
    STATUS_DONE,
    STATUS_DUE,
    STATUS_UPCOMING,
    Estimator,
    EventState,
    PrinterSnapshot,
)
from custom_components.bambu_timeline.gcode_parser import parse_gcode_file

from .conftest import FIXTURES

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
PARSED = parse_gcode_file(FIXTURES / "orca_a1_pauses_and_message.gcode")
PAUSE_5, PAUSE_10, MESSAGE_14 = PARSED.events


def at(slicer_left: float, status: str = PRINTER_RUNNING, rate: float = 1.0) -> PrinterSnapshot:
    """What the printer would report at a given slicer position, running `rate` times slower than planned."""
    percent = max(p for p, r in PARSED.progress if r >= int(slicer_left))
    layer = max(
        (layer for layer, start in PARSED.layer_start_remaining.items() if start >= slicer_left),
        default=0,
    )
    return PrinterSnapshot(
        status=status,
        remaining_min=float(int(slicer_left * rate)),
        progress_pct=percent,
        layer=layer,
    )


def fresh_states() -> list[EventState]:
    return [EventState() for _ in PARSED.events]


def test_countdown_at_planned_speed():
    estimator = Estimator(PARSED)
    states = fresh_states()
    snap = at(200)
    estimator.observe(snap)
    estimator.update(snap, states, NOW)
    expected_raw = 200 - PAUSE_5.remaining_min
    assert states[0].status == STATUS_UPCOMING
    assert states[0].minutes_until == pytest.approx(estimator.with_margin(expected_raw), abs=1)
    # Always early, never late.
    assert states[0].minutes_until < expected_raw
    assert states[0].eta == NOW + timedelta(minutes=states[0].minutes_until)
    assert [s.status for s in states] == [STATUS_UPCOMING] * 3


def test_rate_factor_follows_slower_printing():
    estimator = Estimator(PARSED)
    states = fresh_states()
    for slicer_left in range(220, 180, -2):
        estimator.observe(at(slicer_left, rate=2.0))
    assert estimator.k == pytest.approx(2.0, abs=0.05)
    estimator.update(at(180, rate=2.0), states, NOW)
    planned_gap = 180 - PAUSE_5.remaining_min
    assert states[0].minutes_until == pytest.approx(estimator.with_margin(2 * planned_gap), abs=3)


def test_pause_lifecycle():
    estimator = Estimator(PARSED)
    states = fresh_states()

    estimator.update(at(PAUSE_5.remaining_min - 0.1, status=PRINTER_PAUSED), states, NOW)
    assert [s.status for s in states] == [STATUS_ACTIVE, STATUS_UPCOMING, STATUS_UPCOMING]
    assert states[0].eta == NOW
    frozen = states[1].minutes_until

    later = NOW + timedelta(minutes=20)
    estimator.update(at(PAUSE_5.remaining_min - 0.1, status=PRINTER_PAUSED), states, later)
    assert states[1].minutes_until == frozen
    assert states[1].eta == later + timedelta(minutes=frozen)

    resumed = later + timedelta(minutes=1)
    estimator.update(at(PAUSE_5.remaining_min - 0.5), states, resumed)
    assert states[0].status == STATUS_DONE
    assert states[0].status_since == resumed
    assert states[1].status == STATUS_UPCOMING


def test_missed_pause_becomes_done_after_timeout():
    estimator = Estimator(PARSED)
    states = fresh_states()
    snap = at(PAUSE_5.remaining_min - 0.1)
    estimator.update(snap, states, NOW)
    assert states[0].status == STATUS_DUE
    estimator.update(snap, states, NOW + timedelta(minutes=2))
    assert states[0].status == STATUS_DUE
    estimator.update(snap, states, NOW + timedelta(minutes=4))
    assert states[0].status == STATUS_DONE


def test_layer_past_event_means_done():
    estimator = Estimator(PARSED)
    states = fresh_states()
    estimator.update(at(PAUSE_10.remaining_min - 5), states, NOW)
    assert [s.status for s in states] == [STATUS_DONE, STATUS_DUE, STATUS_UPCOMING]


def test_custom_gcode_done_when_reached():
    estimator = Estimator(PARSED)
    states = fresh_states()
    estimator.update(at(MESSAGE_14.remaining_min - 0.2), states, NOW)
    assert states[2].status == STATUS_DONE


def test_manual_pause_elsewhere_does_not_activate_event():
    estimator = Estimator(PARSED)
    states = fresh_states()
    estimator.update(at(190), states, NOW)
    estimator.update(at(190, status=PRINTER_PAUSED), states, NOW + timedelta(minutes=10))
    assert [s.status for s in states] == [STATUS_UPCOMING] * 3


def test_preparing_counts_from_print_start():
    estimator = Estimator(PARSED)
    states = fresh_states()
    estimator.update(PrinterSnapshot(status=PRINTER_PREPARING, remaining_min=0, layer=0), states, NOW)
    assert states[0].estimate_after_start
    assert states[0].minutes_until == pytest.approx(estimator.with_margin(PAUSE_5.elapsed_min))


def test_done_state_restored_for_earlier_layer_is_reset():
    estimator = Estimator(PARSED)
    states = fresh_states()
    states[0].status = STATUS_DONE
    estimator.update(at(200), states, NOW)
    assert states[0].status == STATUS_UPCOMING


def test_only_one_event_active_at_a_time():
    estimator = Estimator(PARSED)
    states = fresh_states()
    # Paused well past both pauses' positions (e.g. after an HA restart): only the next pause can be active.
    states[0].status = STATUS_DONE
    estimator.update(at(PAUSE_10.remaining_min - 0.1, status=PRINTER_PAUSED), states, NOW)
    assert [s.status for s in states] == [STATUS_DONE, STATUS_ACTIVE, STATUS_UPCOMING]


def test_repeated_readings_count_once():
    estimator = Estimator(PARSED)
    for _ in range(20):
        estimator.observe(at(200, rate=2.0))
    assert estimator.k == pytest.approx(1.3)
