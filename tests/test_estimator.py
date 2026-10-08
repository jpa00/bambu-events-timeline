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
    estimator.observe(snap, NOW)
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
        estimator.observe(at(slicer_left, rate=2.0), NOW)
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
        estimator.observe(at(200, rate=2.0), NOW)
    assert estimator.k == pytest.approx(1.3, abs=0.01)


def test_margin_defaults_and_percentage():
    estimator = Estimator(PARSED)
    assert estimator.with_margin(60) == pytest.approx(59.5)
    estimator = Estimator(PARSED, margin_min=1, margin_pct=0.03)
    assert estimator.with_margin(120) == pytest.approx(120 - 3.6)  # Percentage of the time left wins far out...
    assert estimator.with_margin(10) == pytest.approx(9)  # ...the minute margin close to the event.


def test_minute_interpolation():
    from custom_components.bambu_timeline.estimator import MinuteInterpolator

    clock = MinuteInterpolator()
    clock.observe(10, PRINTER_RUNNING, NOW)
    assert clock.refine(10, NOW + timedelta(seconds=30)) == 10  # No drop seen yet: the early side.

    clock.observe(9, PRINTER_RUNNING, NOW)  # Just dropped below 10.
    assert clock.refine(9, NOW + timedelta(seconds=15)) == pytest.approx(9.75)
    assert clock.refine(9, NOW + timedelta(seconds=90)) == 9  # Never below the reported value.

    # Time spent paused doesn't count.
    clock.observe(9, PRINTER_PAUSED, NOW + timedelta(seconds=15))
    assert clock.refine(9, NOW + timedelta(minutes=10)) == pytest.approx(9.75)
    clock.observe(9, PRINTER_RUNNING, NOW + timedelta(minutes=10))
    assert clock.refine(9, NOW + timedelta(minutes=10, seconds=15)) == pytest.approx(9.5)

    # A jump (e.g. after a speed change) means the position within the minute is unknown again.
    clock.observe(5, PRINTER_RUNNING, NOW + timedelta(minutes=11))
    assert clock.refine(5, NOW + timedelta(minutes=11, seconds=30)) == 5


def replay_predictions():
    """Replay a real print's readings; return (time, predicted pause time) at each reading."""
    import json
    from datetime import timedelta

    from custom_components.bambu_timeline.gcode_parser import KIND_PAUSE, ParsedPrint, TimelineEvent

    data = json.loads((FIXTURES / "replay_a1_standard_speed.json").read_text(encoding="utf-8"))
    event = TimelineEvent(
        kind=KIND_PAUSE, layer=data["pause_layer"], remaining_min=data["pause_remaining_min"], elapsed_min=0, line=0
    )
    parsed = ParsedPrint(events=[event], layer_start_remaining={data["pause_layer"]: data["pause_remaining_min"]})
    estimator = Estimator(parsed)
    predictions = []
    for offset, status, remaining, progress, layer in data["readings"]:
        now = NOW + timedelta(seconds=offset)
        snap = PrinterSnapshot(status=status, remaining_min=remaining, progress_pct=progress, layer=layer)
        estimator.observe(snap, now)
        if status == PRINTER_RUNNING and layer < data["pause_layer"]:
            raw = estimator.raw_minutes_until(event, snap)
            predictions.append((offset, offset + raw * 60))
    return data["pause_between_s"], predictions, estimator


def test_replay_of_a_real_print():
    """Replays a real A1 print (readings start mid-print, after a restart). Once the pace has
    10 minutes of data, the pause prediction is never late and at most a minute early."""
    (earliest, latest), predictions, _ = replay_predictions()
    checked = 0
    for offset, predicted in predictions:
        if latest - offset <= 13 * 60 and offset >= 10 * 60:
            assert earliest - 60 <= predicted <= latest, (offset, predicted)
            checked += 1
    assert checked >= 10


def test_pace_meter():
    from custom_components.bambu_timeline.estimator import PaceMeter

    meter = PaceMeter()
    now, remaining = NOW, 100.0
    for _ in range(15):  # Each printer-minute takes 62 real seconds.
        meter.observe(remaining, PRINTER_RUNNING, now)
        now += timedelta(seconds=62)
        remaining -= 1
    assert meter.pace == pytest.approx(62 / 60, abs=0.001)

    # A pause doesn't count, and a big jump (a skipped filament change) counts as one minute.
    meter.observe(remaining, PRINTER_PAUSED, now)
    now += timedelta(minutes=30)
    meter.observe(remaining, PRINTER_RUNNING, now)
    now += timedelta(seconds=62)
    remaining -= 3
    meter.observe(remaining, PRINTER_RUNNING, now)
    assert meter.pace == pytest.approx(62 / 60, abs=0.001)


def test_pace_needs_ten_minutes_of_data():
    from custom_components.bambu_timeline.estimator import PaceMeter

    meter = PaceMeter()
    now, remaining = NOW, 100.0
    for _ in range(5):
        meter.observe(remaining, PRINTER_RUNNING, now)
        now += timedelta(seconds=90)
        remaining -= 1
    assert meter.pace == 1.0


def test_profile_changes_on_external_spool_take_no_time():
    """The fixture has a filament change at layer 3 (slicer budget 1 min) before the pause at layer 5."""
    change = next(c for c in PARSED.filament_changes if c.layer == 3)
    assert change.budget_min == 1.0
    estimator = Estimator(PARSED)
    before_change = at(190)
    ams = estimator.raw_minutes_until(PAUSE_5, before_change)
    before_change.on_external_spool = True
    external = estimator.raw_minutes_until(PAUSE_5, before_change)
    assert ams - external == pytest.approx(change.budget_min)

    # Once the change is behind, the printer's remaining time has already jumped: nothing to subtract.
    after_change = at(170)
    after_change.on_external_spool = True
    assert estimator.skipped_change_time(PAUSE_5, after_change.best_remaining, after_change) == 0

    # Events before the change aren't affected either.
    assert estimator.skipped_change_time(PAUSE_5, 165, before_change) == 0


def test_change_with_a_pause_is_not_skipped():
    from custom_components.bambu_timeline.gcode_parser import parse_gcode_lines

    parsed = parse_gcode_lines(
        "M73 P0 R60\nM73 L1\nM73 P10 R50\nM620 S1A\nM400 U1\nM73 P20 R45\nM621 S1A\nM73 L2\nM73 P50 R30\nM400 U1\n".splitlines()
    )
    change_event, later_pause = parsed.events
    estimator = Estimator(parsed)
    snap = PrinterSnapshot(status=PRINTER_RUNNING, remaining_min=55, layer=1, on_external_spool=True)
    assert estimator.skipped_change_time(later_pause, 55, snap) == 0  # A real (manual) change takes real time.
