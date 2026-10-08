import re

import pytest

from custom_components.bambu_timeline.gcode_parser import (
    KIND_CUSTOM_GCODE,
    KIND_PAUSE,
    parse_duration,
    parse_gcode_file,
    parse_gcode_lines,
)

from .conftest import FIXTURES

AMS_FIXTURE = FIXTURES / "orca_a1_pauses_and_ams_swaps.gcode"
MESSAGE_FIXTURE = FIXTURES / "orca_a1_pauses_and_message.gcode"

# Event times read from OrcaSlicer's preview timeline (hovering the event icon).
ORCA_PAUSE_TIMES = [70 + 3 / 60, 117 + 9 / 60]
ORCA_MESSAGE_TIME = 146 + 9 / 60
TOLERANCE_MIN = 0.5


def lines(text: str) -> list[str]:
    return text.strip("\n").splitlines()


def test_header():
    parsed = parse_gcode_file(AMS_FIXTURE)
    assert parsed.slicer == "OrcaSlicer 2.4.2"
    assert parsed.total_min == pytest.approx(3 * 60 + 48 + 43 / 60)
    assert parsed.total_layers == 24


def test_pauses_found_and_ams_swaps_ignored():
    parsed = parse_gcode_file(AMS_FIXTURE)
    assert [(e.kind, e.layer) for e in parsed.events] == [(KIND_PAUSE, 5), (KIND_PAUSE, 10)]
    for event, orca in zip(parsed.events, ORCA_PAUSE_TIMES):
        assert event.elapsed_min == pytest.approx(orca, abs=TOLERANCE_MIN)
        assert event.commands == ["M400 U1"]


def test_custom_gcode_message():
    parsed = parse_gcode_file(MESSAGE_FIXTURE)
    assert [(e.kind, e.layer) for e in parsed.events] == [
        (KIND_PAUSE, 5),
        (KIND_PAUSE, 10),
        (KIND_CUSTOM_GCODE, 14),
    ]
    custom = parsed.events[2]
    assert custom.message == "Hello Claude"
    assert custom.description == "Message: Hello Claude"
    assert custom.elapsed_min == pytest.approx(ORCA_MESSAGE_TIME, abs=TOLERANCE_MIN)


def test_pause_times_agree_with_slicer_countdown():
    """The slicer writes its own "minutes to next pause" (M73 C) into the file. Use it as a cross-check."""
    parsed = parse_gcode_file(AMS_FIXTURE)
    pauses = [e for e in parsed.events if e.kind == KIND_PAUSE]
    remaining = None
    checked = 0
    with open(AMS_FIXTURE, encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if match := re.match(r"M73 P\d+ R(\d+)", line):
                remaining = int(match.group(1)) + 0.5
            elif (match := re.match(r"M73 C([\d.]+)", line)) and remaining is not None:
                minutes_to_pause = float(match.group(1))
                upcoming = [p for p in pauses if p.line > line_no]
                if minutes_to_pause >= 1 and upcoming:
                    assert remaining - minutes_to_pause == pytest.approx(upcoming[0].remaining_min, abs=1.5)
                    checked += 1
    assert checked > 0


def test_layer_and_progress_tables():
    parsed = parse_gcode_file(AMS_FIXTURE)
    assert sorted(parsed.layer_start_remaining) == list(range(1, 25))
    starts = [parsed.layer_start_remaining[layer] for layer in range(1, 25)]
    assert starts == sorted(starts, reverse=True)
    assert parsed.progress[0] == (0, 228)
    assert parsed.progress[-1] == (100, 0)


def test_bare_pause_commands_count_once_each():
    parsed = parse_gcode_lines(
        lines("""
; total estimated time: 1h 0m 0s
M73 P0 R60
M73 L1
; PAUSE_PRINTING
M400 U1
M73 P50 R30
M73 L2
M400 U1 ; added by hand
M73 L3
M600
""")
    )
    assert [(e.kind, e.layer, e.remaining_min) for e in parsed.events] == [
        (KIND_PAUSE, 1, 60.0),
        (KIND_PAUSE, 2, 30.5),
        (KIND_PAUSE, 3, 30.5),
    ]


def test_custom_gcode_containing_pause_becomes_pause():
    parsed = parse_gcode_lines(
        lines("""
; total estimated time: 10m
M73 P0 R10
M73 L1
; CUSTOM_GCODE
M117 Swap the insert
M400 U1

G1 X10
""")
    )
    assert len(parsed.events) == 1
    event = parsed.events[0]
    assert event.kind == KIND_PAUSE
    assert event.commands == ["M117 Swap the insert", "M400 U1"]
    assert event.message == "Swap the insert"


def test_config_block_is_not_gcode():
    parsed = parse_gcode_lines(
        lines("""
; CONFIG_BLOCK_START
; machine_pause_gcode = M400 U1
; PAUSE_PRINTING
M400 U1
; CONFIG_BLOCK_END
; EXECUTABLE_BLOCK_START
M73 P0 R5
; EXECUTABLE_BLOCK_END
M400 U1
""")
    )
    assert parsed.events == []


def test_ams_swap_is_not_an_event():
    parsed = parse_gcode_lines(lines("M73 P0 R5\nM73 L2\nM620 S1A\nT1\nM621 S1A\n"))
    assert parsed.events == []


@pytest.mark.parametrize(
    ("text", "minutes"),
    [("3h 48m 43s", 228 + 43 / 60), ("45m", 45.0), ("1d 2h", 1560.0), ("12s", 0.2), ("n/a", None)],
)
def test_parse_duration(text, minutes):
    assert parse_duration(text) == (pytest.approx(minutes) if minutes is not None else None)


def test_filament_changes_are_recorded_but_not_events():
    parsed = parse_gcode_file(AMS_FIXTURE)
    changes = [(c.layer, c.filament, c.budget_min) for c in parsed.filament_changes if c.layer > 0]
    assert changes == [(3, 2, 1.0), (14, 3, 1.0), (24, 2, 1.0)]
    assert all(change.event is None for change in parsed.filament_changes)


def test_pause_inside_filament_change_is_one_event():
    """Without an AMS, a pause in the printer's change-filament G-code makes each change a manual step."""
    parsed = parse_gcode_lines(
        lines("""
; total estimated time: 1h
M73 P0 R60
M73 L1
M73 P40 R35
M73 L2
M620 S1A
M400 U1
T1
M400 U1
M73 P45 R33
M621 S1A
M73 L3
""")
    )
    assert len(parsed.events) == 1
    event = parsed.events[0]
    assert (event.kind, event.layer, event.description) == (KIND_PAUSE, 2, "Filament change (filament 2)")
    assert event.remaining_min == 35.5
    assert parsed.filament_changes[0].event is event
    assert parsed.filament_changes[0].budget_min == 2.0


def test_filament_change_without_end_marker_closes_at_next_layer():
    parsed = parse_gcode_lines(lines("M73 P0 R10\nM73 L1\nM620 S2A\nT2\nM73 L2\nM400 U1\n"))
    assert [e.description for e in parsed.events] == ["Pause"]
    assert parsed.filament_changes[0].filament == 3
