"""Downloadable diagnostics: the parsed events and a minute-by-minute log of the current print.

The log is what is needed to tune the countdown: it pairs the printer's own
remaining time with the slicer's timeline, including across speed changes.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .coordinator import TimelineCoordinator
from .presentation import event_attributes


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coordinator: TimelineCoordinator = entry.runtime_data
    session = coordinator.session
    data: dict[str, Any] = {
        "options": dict(coordinator.options),
        "printer_entities": coordinator.entity_ids,
        "cache_folders": [str(root) for root in coordinator.cache_roots()],
        "gcode_status": coordinator.parse_status,
        "error": coordinator.parse_error,
        "printer_now": vars(coordinator.snapshot()),
        "print": None,
    }
    if session is None:
        return data
    parsed = session.parsed
    data["print"] = {
        "key": session.key,
        "started": session.started.isoformat(),
        "file": session.file,
        "slicer": parsed.slicer if parsed else None,
        "slicer_total_minutes": parsed.total_min if parsed else None,
        "total_layers": parsed.total_layers if parsed else None,
        "rate_factor": session.estimator.k if session.estimator else None,
        "events": [
            {**event_attributes(event, state, parsed), "alert": alert, "remaining_at_event": event.remaining_min}
            for event, state, alert in zip(session.events, session.states, session.alerts)
        ],
        "log": session.samples,
    }
    return data
