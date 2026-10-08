"""Turn the printer's live state into a countdown for each parsed event.

No Home Assistant imports here, so this can be tested on its own.

The printer reports its own remaining time (``R_p``). The slicer's timeline
says how many slicer-minutes are left when each event is reached
(``event.remaining_min``). The two are linked by a rate factor ``k`` = real
minutes per slicer minute, which absorbs speed-profile changes, skipped
objects and slicer error:

    minutes until event = R_p - event.remaining_min * k

``k`` is learned while printing by comparing ``R_p`` with the slicer position,
which is looked up from the reported progress percentage and layer. While the
printer is paused, ``R_p`` stops moving, so the countdown freezes as well.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .gcode_parser import KIND_PAUSE, ParsedPrint, TimelineEvent

STATUS_UPCOMING = "upcoming"
STATUS_DUE = "due"
"""The position is reached but the printer hasn't paused yet. Expected any moment."""
STATUS_ACTIVE = "active"
"""The printer is paused at this event."""
STATUS_DONE = "done"

PRINTER_PAUSED = "pause"
PRINTER_PREPARING = "prepare"
PRINTER_RUNNING = "running"

# A pause that should have happened but didn't within this time is treated as passed.
DUE_TIMEOUT = timedelta(minutes=3)
# k is only learned when enough of the print is left for the ratio to be meaningful.
MIN_SLICER_MINUTES_FOR_K = 10.0
K_LIMITS = (0.3, 3.0)
K_SMOOTHING = 0.3
# Events within this many slicer-minutes of their layer's start happen at the layer change.
LAYER_START_TOLERANCE = 1.0


@dataclass
class PrinterSnapshot:
    status: str | None = None
    remaining_min: float | None = None
    progress_pct: float | None = None
    layer: int | None = None
    speed: str | None = None


@dataclass
class EventState:
    status: str = STATUS_UPCOMING
    status_since: datetime | None = None
    minutes_until: float | None = None
    eta: datetime | None = None
    estimate_after_start: bool = False
    """True while the printer is still preparing, so the countdown hasn't really started."""


class Estimator:
    def __init__(self, parsed: ParsedPrint, margin_min: float = 0.5, margin_pct: float = 0.0, k: float = 1.0):
        self.parsed = parsed
        self.margin_min = margin_min
        self.margin_pct = margin_pct
        self.k = k
        self._percent_ranges: dict[int, tuple[int, int]] = {}
        for percent, remaining in parsed.progress:
            low, high = self._percent_ranges.get(percent, (remaining, remaining))
            self._percent_ranges[percent] = (min(low, remaining), max(high, remaining))
        self._at_layer_start = [self._is_at_layer_start(event) for event in parsed.events]
        self._last_reading: tuple | None = None

    def _is_at_layer_start(self, event: TimelineEvent) -> bool:
        start = self.parsed.layer_start_remaining.get(event.layer)
        return start is not None and abs(start - event.remaining_min) <= LAYER_START_TOLERANCE

    def slicer_remaining(self, snap: PrinterSnapshot) -> float | None:
        """Slicer-minutes left at the printer's current position, or None if unknown."""
        estimate = None
        if snap.progress_pct is not None:
            percent_range = self._percent_ranges.get(int(snap.progress_pct))
            if percent_range is not None:
                estimate = (percent_range[0] + percent_range[1]) / 2 + 0.5
        if snap.layer is not None and snap.layer > 0:
            upper = self.parsed.layer_start_remaining.get(snap.layer)
            lower = self.parsed.layer_start_remaining.get(snap.layer + 1, 0.0)
            if upper is not None:
                estimate = upper if estimate is None else min(max(estimate, lower), upper)
        return estimate

    def observe(self, snap: PrinterSnapshot) -> None:
        """Learn the rate factor from a printer update. Repeated identical readings are only counted once."""
        if snap.status != PRINTER_RUNNING or snap.remaining_min is None:
            return
        reading = (snap.remaining_min, snap.progress_pct, snap.layer)
        if reading == self._last_reading:
            return
        self._last_reading = reading
        slicer_left = self.slicer_remaining(snap)
        if slicer_left is None or slicer_left < MIN_SLICER_MINUTES_FOR_K:
            return
        k_now = min(max(snap.remaining_min / slicer_left, K_LIMITS[0]), K_LIMITS[1])
        self.k += K_SMOOTHING * (k_now - self.k)

    def raw_minutes_until(self, event: TimelineEvent, snap: PrinterSnapshot) -> float | None:
        """Best estimate without the safety margin. Negative means the event's position has passed."""
        if snap.status == PRINTER_PREPARING and self.parsed.total_min is not None:
            return self.parsed.total_min - event.remaining_min
        if snap.remaining_min is not None:
            return snap.remaining_min - event.remaining_min * self.k
        slicer_left = self.slicer_remaining(snap)
        if slicer_left is not None:
            return (slicer_left - event.remaining_min) * self.k
        return None

    def with_margin(self, minutes: float) -> float:
        """Make an estimate early rather than late."""
        if minutes <= 0:
            return 0.0
        return max(0.0, minutes - max(self.margin_min, self.margin_pct * minutes))

    def _reached(self, index: int, snap: PrinterSnapshot, raw: float | None) -> bool:
        event = self.parsed.events[index]
        if raw is not None and raw < -1.5 and snap.status != PRINTER_PREPARING:
            return True
        if snap.layer is None:
            return False
        if snap.layer > event.layer:
            return True
        if snap.layer < event.layer:
            return False
        if self._at_layer_start[index]:
            return True
        slicer_left = self.slicer_remaining(snap)
        return slicer_left is not None and slicer_left <= event.remaining_min

    def update(self, snap: PrinterSnapshot, states: list[EventState], now: datetime) -> None:
        """Update every event's status and countdown in place."""
        active_taken = False
        for index, (event, state) in enumerate(zip(self.parsed.events, states)):
            raw = self.raw_minutes_until(event, snap)
            reached = self._reached(index, snap, raw)
            previous = state.status

            if previous == STATUS_DONE and snap.layer is not None and snap.layer < event.layer:
                # Restored from storage for what turned out to be a different print.
                previous = STATUS_UPCOMING

            if event.kind != KIND_PAUSE:
                new = STATUS_DONE if reached else STATUS_UPCOMING
            elif previous == STATUS_DONE:
                new = STATUS_DONE
            elif reached and snap.status == PRINTER_PAUSED and not active_taken:
                new = STATUS_ACTIVE
            elif previous == STATUS_ACTIVE:
                # Still paused stays active; anything else means the user resumed.
                new = STATUS_ACTIVE if snap.status == PRINTER_PAUSED else STATUS_DONE
            elif reached and snap.layer is not None and snap.layer > event.layer:
                new = STATUS_DONE
            elif reached:
                waited = now - state.status_since if previous == STATUS_DUE and state.status_since else timedelta(0)
                new = STATUS_DONE if waited >= DUE_TIMEOUT else STATUS_DUE
            else:
                new = STATUS_UPCOMING

            if new == STATUS_ACTIVE:
                active_taken = True
            if new != state.status:
                state.status = new
                state.status_since = now

            state.estimate_after_start = snap.status == PRINTER_PREPARING
            if new == STATUS_UPCOMING and raw is not None:
                minutes = self.with_margin(raw)
                if snap.status == PRINTER_PAUSED and state.minutes_until is not None:
                    # A pause somewhere else: the countdown waits, the clock time moves on.
                    minutes = min(minutes, state.minutes_until)
                state.minutes_until = minutes
                state.eta = now + timedelta(minutes=minutes)
            elif new == STATUS_UPCOMING:
                state.minutes_until = None
                state.eta = None
            elif new == STATUS_DUE:
                state.minutes_until = 0.0
                state.eta = now
            else:
                state.minutes_until = 0.0
                state.eta = state.status_since
