"""Turn the printer's live state into a countdown for each parsed event.

No Home Assistant imports here, so this can be tested on its own.

The printer reports its own remaining time (``R_p``, whole minutes rounded down).
On an A1 at standard speed this is simply the slicer's estimate passed through:
it doesn't adapt to how the print is really going. The slicer's timeline says
how many slicer-minutes are left when each event is reached
(``event.remaining_min``). So:

    minutes until event = (R_p - event.remaining_min * k) * pace

``pace`` is measured: real minutes per printer-minute over the last stretch of
printing, pauses left out. A print running 3 % slower than the slicer thought
has a pace of 1.03. ``k`` is the ratio between the printer's remaining time and
the slicer's timeline. It stays at 1 unless the two clearly part ways, which
could happen if the firmware rescales its estimate after a speed change.

While the printer is paused, ``R_p`` stops moving, so the countdown freezes too.

Filament changes on the external spool: the printer can't swap filament there by
itself, so a change without a pause can only be a profile change on the same spool,
and it takes almost no time. The slicer still budgets time for it, which would make
events after it come earlier than counted down. While the printer feeds from the
external spool, those budgets are left out of the countdown.
"""

from __future__ import annotations

from collections import deque
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
# Below this difference from 1, k is just measurement noise and isn't applied.
K_DEADBAND = 0.03
# Pace is measured over this much printing time, and only used once there is enough of it.
PACE_WINDOW_MIN = 20.0
PACE_MIN_DATA_MIN = 10.0
PACE_LIMITS = (0.8, 1.25)
# Events within this many slicer-minutes of their layer's start happen at the layer change.
LAYER_START_TOLERANCE = 1.0


@dataclass
class PrinterSnapshot:
    status: str | None = None
    remaining_min: float | None = None
    """The printer's remaining time as reported, in whole minutes (rounded down)."""
    progress_pct: float | None = None
    layer: int | None = None
    speed: str | None = None
    remaining_precise: float | None = None
    """``remaining_min`` refined to within the current minute (see MinuteInterpolator), if known."""
    on_external_spool: bool = False
    """The printer is feeding from the external spool, not the AMS."""

    @property
    def best_remaining(self) -> float | None:
        return self.remaining_precise if self.remaining_precise is not None else self.remaining_min


class MinuteInterpolator:
    """Estimate how far into its current whole minute the printer's remaining time is.

    The printer reports remaining time rounded down to whole minutes, so the value
    drops to ``n`` the moment the true remaining time falls below ``n + 1``. From the
    moment that drop is seen, the true value is about ``n + 1 - (time since the drop)``.
    Time spent paused doesn't count. Until a drop has been seen while printing, the
    reported value is used as is, which errs on the early side.
    """

    def __init__(self) -> None:
        self._dropped_at: datetime | None = None
        self._paused_at: datetime | None = None
        self._last_value: float | None = None

    def observe(self, value: float | None, status: str | None, now: datetime) -> None:
        if status == PRINTER_PAUSED:
            if self._dropped_at is not None and self._paused_at is None:
                self._paused_at = now
            self._last_value = value
            return
        if self._paused_at is not None and self._dropped_at is not None:
            # Resumed: the pause doesn't count towards the current minute.
            self._dropped_at += now - self._paused_at
        self._paused_at = None
        if value != self._last_value:
            dropped_by_one = (
                status == PRINTER_RUNNING
                and value is not None
                and self._last_value is not None
                and self._last_value - value == 1
            )
            # Any other change (a jump after a speed change, a new print) leaves the position unknown.
            self._dropped_at = now if dropped_by_one else None
        self._last_value = value

    def refine(self, value: float | None, now: datetime) -> float | None:
        if value is None or self._dropped_at is None:
            return value
        reference = self._paused_at or now
        elapsed = (reference - self._dropped_at).total_seconds() / 60
        return value + 1 - min(max(elapsed, 0.0), 1.0)


@dataclass
class EventState:
    status: str = STATUS_UPCOMING
    status_since: datetime | None = None
    minutes_until: float | None = None
    eta: datetime | None = None
    estimate_after_start: bool = False
    """True while the printer is still preparing, so the countdown hasn't really started."""
    raw_minutes_until: float | None = None
    """The estimate without the safety margin, for calibration."""


class PaceMeter:
    """Real minutes per printer-minute, over the last ``PACE_WINDOW_MIN`` minutes of printing.

    A drop of the printer's remaining time by 1 or 2 counts as that many printer-minutes
    (two can land in one update). A bigger jump means the slicer budgeted time for
    something that went faster, like a filament change on the same spool; it counts as
    one, so it doesn't make the print look faster. Paused time and the partial minute
    before the first drop aren't counted.
    """

    def __init__(self) -> None:
        self._intervals: deque[tuple[float, float]] = deque()
        self._pending = 0.0
        self._last_time: datetime | None = None
        self._last_value: float | None = None
        self._last_status: str | None = None
        self._ticked = False

    def observe(self, value: float | None, status: str | None, now: datetime) -> None:
        if self._last_time is not None and status == PRINTER_RUNNING and self._last_status == PRINTER_RUNNING:
            self._pending += (now - self._last_time).total_seconds() / 60
        if value is not None and self._last_value is not None and value != self._last_value:
            if status == PRINTER_RUNNING and value < self._last_value:
                if self._ticked:
                    drop = self._last_value - value
                    self._intervals.append((self._pending, drop if drop <= 2 else 1.0))
                    while self._real_minutes() - self._intervals[0][0] >= PACE_WINDOW_MIN:
                        self._intervals.popleft()
                self._ticked = True
            else:
                # Went up, or changed while not printing: start the current minute over.
                self._ticked = False
            self._pending = 0.0
        self._last_time, self._last_value, self._last_status = now, value, status

    def _real_minutes(self) -> float:
        return sum(real for real, _ in self._intervals)

    @property
    def pace(self) -> float:
        measured = self._real_minutes()
        printer_minutes = sum(minutes for _, minutes in self._intervals)
        if measured < PACE_MIN_DATA_MIN or printer_minutes <= 0:
            return 1.0
        return min(max(measured / printer_minutes, PACE_LIMITS[0]), PACE_LIMITS[1])


class Estimator:
    def __init__(self, parsed: ParsedPrint, margin_min: float = 0.5, margin_pct: float = 0.0, k: float = 1.0):
        self.parsed = parsed
        self.margin_min = margin_min
        self.margin_pct = margin_pct
        self.k = k
        self.pace_meter = PaceMeter()
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

    @property
    def pace(self) -> float:
        return self.pace_meter.pace

    @property
    def effective_k(self) -> float:
        return self.k if abs(self.k - 1.0) > K_DEADBAND else 1.0

    def observe(self, snap: PrinterSnapshot, now: datetime) -> None:
        """Learn pace and rate factor from a printer update. Repeated identical readings count once."""
        self.pace_meter.observe(snap.remaining_min, snap.status, now)
        if snap.status != PRINTER_RUNNING or snap.remaining_min is None:
            return
        reading = (snap.remaining_min, snap.progress_pct, snap.layer)
        if reading == self._last_reading:
            return
        self._last_reading = reading
        slicer_left = self.slicer_remaining(snap)
        if slicer_left is None or slicer_left < MIN_SLICER_MINUTES_FOR_K:
            return
        # The printer rounds down while the slicer position is mid-minute: compare like with like.
        printer_left = snap.remaining_precise if snap.remaining_precise is not None else snap.remaining_min + 0.5
        k_now = min(max(printer_left / slicer_left, K_LIMITS[0]), K_LIMITS[1])
        self.k += K_SMOOTHING * (k_now - self.k)

    def raw_minutes_until(self, event: TimelineEvent, snap: PrinterSnapshot) -> float | None:
        """Best estimate without the safety margin. Negative means the event's position has passed."""
        if snap.status == PRINTER_PREPARING and self.parsed.total_min is not None:
            return self.parsed.total_min - event.remaining_min
        k = self.effective_k
        if (remaining := snap.best_remaining) is not None:
            skipped = self.skipped_change_time(event, remaining / k, snap)
            return (remaining - (event.remaining_min + skipped) * k) * self.pace
        slicer_left = self.slicer_remaining(snap)
        if slicer_left is not None:
            skipped = self.skipped_change_time(event, slicer_left, snap)
            return (slicer_left - event.remaining_min - skipped) * k * self.pace
        return None

    def skipped_change_time(self, event: TimelineEvent, position: float, snap: PrinterSnapshot) -> float:
        """Slicer minutes budgeted for filament changes before ``event`` that won't really take time.

        ``position`` is the current point in the print, in slicer minutes left. Only changes
        still ahead count: once one has happened, the printer's remaining time has already
        jumped past its budget.
        """
        if not snap.on_external_spool:
            return 0.0
        return sum(
            change.budget_min
            for change in self.parsed.filament_changes
            if change.event is None and event.remaining_min < change.remaining_min <= position
        )

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
            state.raw_minutes_until = raw if new == STATUS_UPCOMING else None
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
