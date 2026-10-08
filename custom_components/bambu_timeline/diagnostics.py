"""Downloadable diagnostics, for tuning the countdown.

Covers the current print and the last finished one. Each has a minute-by-minute log
that pairs the printer's own remaining time with the slicer's timeline, and a
calibration report: what was predicted 5 and 1 minutes before each event, next to
when it actually happened.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .coordinator import TimelineCoordinator


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coordinator: TimelineCoordinator = entry.runtime_data
    session = coordinator.session
    return {
        "options": dict(coordinator.options),
        "printer_entities": coordinator.entity_ids,
        "missing_printer_sensors": coordinator.missing_printer_sensors,
        "external_spool_entities": coordinator.spool_entity_ids,
        "cache_folders": [str(root) for root in coordinator.cache_roots()],
        "gcode_status": coordinator.parse_status,
        "error": coordinator.parse_error,
        "printer_now": vars(coordinator.snapshot()),
        "time_now": dt_util.utcnow().isoformat(timespec="seconds"),
        "print": coordinator.print_summary(session) if session and session.parsed else None,
        "last_finished_print": coordinator.last_print,
    }
