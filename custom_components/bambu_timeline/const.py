"""Constants for Bambu Events Timeline."""

from __future__ import annotations

import logging

DOMAIN = "bambu_timeline"
LOGGER = logging.getLogger(__package__)

BAMBU_DOMAIN = "bambu_lab"
MOBILE_APP_DOMAIN = "mobile_app"

CONF_BAMBU_ENTRY_ID = "bambu_entry_id"
CONF_SERIAL = "serial"
CONF_MODEL = "model"

CONF_LEAD_TIME = "lead_time"
CONF_ALERT_ON_EVENT = "alert_on_event"
CONF_DEFAULT_RECIPIENTS = "default_recipients"
CONF_CUSTOM_GCODE_ALERTS = "custom_gcode_alerts"
CONF_MARGIN_MIN = "early_margin_minutes"
CONF_MARGIN_PCT = "early_margin_percent"
CONF_EVENT_SLOTS = "event_slots"
CONF_SHOW_FINISHED = "show_finished"
CONF_SHOW_SECONDS = "show_seconds"

DEFAULT_OPTIONS = {
    CONF_LEAD_TIME: 5,
    CONF_ALERT_ON_EVENT: True,
    CONF_DEFAULT_RECIPIENTS: [],
    CONF_CUSTOM_GCODE_ALERTS: False,
    CONF_MARGIN_MIN: 0.5,
    CONF_MARGIN_PCT: 0.0,
    CONF_EVENT_SLOTS: 10,
    CONF_SHOW_FINISHED: False,
    CONF_SHOW_SECONDS: False,
}

# ha-bambulab printer sensors we read, by entity description key.
# Their unique IDs are "<serial>_<key>".
BAMBU_KEYS = (
    "print_status",
    "remaining_time",
    "print_progress",
    "current_layer",
    "total_layers",
    "speed_profile",
    "subtask_name",
    "gcode_file",
    "stage",
    "active_tray",
)

# ha-bambulab's active tray sensor reports the external spool as AMS index 254 or 255.
EXTERNAL_SPOOL_AMS_INDEXES = (254, 255)

# ha-bambulab keeps downloaded print files here, relative to the HA config folder.
BAMBU_CACHE_DIR = "www/media/ha-bambulab"

PRINT_ACTIVE_STATES = ("prepare", "slicing", "running", "pause")
PRINT_ENDED_STATES = ("idle", "finish", "failed")

# How long to keep looking for the G-code after a print starts, and how often.
FILE_SEARCH_TIMEOUT_S = 300
FILE_SEARCH_INTERVAL_S = 10
# A cached file from before the print started is only accepted after this long,
# because ha-bambulab may still be downloading a newer copy of it.
STALE_FILE_GRACE_S = 120

UPDATE_INTERVAL_S = 30
# How often to refresh during an event's last minute, when seconds are shown.
FAST_UPDATE_INTERVAL_S = 5
CALIBRATION_SAMPLE_INTERVAL_S = 60
CALIBRATION_MAX_SAMPLES = 720

PARSE_IDLE = "idle"
PARSE_WAITING = "waiting_for_file"
PARSE_PARSING = "parsing"
PARSE_OK = "ok"
PARSE_NO_EVENTS = "no_events"
PARSE_NOT_FOUND = "file_not_found"
PARSE_ERROR = "error"
PARSE_STATES = [PARSE_IDLE, PARSE_WAITING, PARSE_PARSING, PARSE_OK, PARSE_NO_EVENTS, PARSE_NOT_FOUND, PARSE_ERROR]

STORAGE_VERSION = 1
