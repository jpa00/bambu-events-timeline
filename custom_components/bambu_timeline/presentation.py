"""How events are described on entities and in notifications."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

from .estimator import STATUS_ACTIVE, STATUS_DONE, STATUS_DUE, EventState
from .gcode_parser import KIND_PAUSE, ParsedPrint, TimelineEvent


def layer_text(event: TimelineEvent, parsed: ParsedPrint | None) -> str:
    total = parsed.total_layers if parsed else None
    return f"layer {event.layer}/{total}" if total else f"layer {event.layer}"


def event_label(event: TimelineEvent, parsed: ParsedPrint | None) -> str:
    return f"{event.description} · {layer_text(event, parsed)}"


def whole_minutes(minutes: float | None) -> int | None:
    """Round down, so a countdown never shows more time than the estimate."""
    return None if minutes is None else max(0, math.floor(minutes))


def to_minute(moment: datetime | None) -> datetime | None:
    """Drop seconds, so attributes only change once a minute."""
    return moment.replace(second=0, microsecond=0) if moment else None


def clock(moment: datetime) -> str:
    return dt_util.as_local(moment).strftime("%H:%M")


def duration_text(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest:02d} min" if hours else f"{rest} min"


def countdown_text(event: TimelineEvent, state: EventState) -> str:
    """A short line for the dashboard, e.g. "in 23 min · 14:35"."""
    if state.status == STATUS_ACTIVE:
        return "Paused, waiting for you" if event.kind == KIND_PAUSE else "Now"
    if state.status == STATUS_DUE:
        return "Any moment now"
    if state.status == STATUS_DONE:
        return f"Done {clock(state.status_since)}" if state.status_since else "Done"
    minutes = whole_minutes(state.minutes_until)
    if minutes is None:
        return "Waiting for the printer"
    if state.estimate_after_start:
        return f"{duration_text(minutes)} after printing starts"
    when = f" · {clock(state.eta)}" if state.eta else ""
    if minutes == 0:
        return f"in less than a minute{when}"
    return f"in {duration_text(minutes)}{when}"


def event_attributes(event: TimelineEvent, state: EventState, parsed: ParsedPrint | None) -> dict[str, Any]:
    eta = to_minute(state.eta)
    return {
        "label": event_label(event, parsed),
        "countdown": countdown_text(event, state),
        "description": event.description,
        "kind": event.kind,
        "layer": event.layer,
        "total_layers": parsed.total_layers if parsed else None,
        "status": state.status,
        "eta": eta.isoformat() if eta else None,
        "minutes_until": whole_minutes(state.minutes_until),
        "estimate_after_start": state.estimate_after_start,
        "slicer_time": round(event.elapsed_min, 1),
        "commands": event.commands,
    }
