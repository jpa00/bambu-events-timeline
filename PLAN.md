# Bambu Events Timeline – implementation plan

A Home Assistant custom integration, `bambu_timeline`, installed through HACS. It reads the plate G-code that ha-bambulab already caches. It lists the planned pauses and custom G-code events of the current print, keeps a live countdown to each one, and sends phone alerts to the people who opt in for that print.

## 1. Scope

| In scope | Out of scope |
|---|---|
| Pauses (`; PAUSE_PRINTING` + `M400 U1`, a bare `M400 U1`, `M600`, `M601`) | Filament / AMS swaps (`M620 S<n>A` / `T<n>`): ignored completely (decision "c") |
| Custom G-code at layer (`; CUSTOM_GCODE` + the lines that follow it) | Manual time-entry list (not needed) |
| Live countdown per event, plus a "next event" summary | Controlling the printer (pause/resume) |
| Lead-time alert (default 5 min) and an "it happened" alert (default on) | |
| Per-print opt-in of notification recipients | |

### Verified G-code facts (OrcaSlicer 2.4.2, A1, test files)

```
; HEADER_BLOCK_START … ; total estimated time: 3h 48m 43s … ; total layer number: 24
M73 P<pct> R<min-left>        ; slicer progress, emitted ~every minute
; layer num/total_layer_count: 5/24
M73 L5                         ; layer start marker (printer reports layer_num from this)
; PAUSE_PRINTING
M400 U1                        ; A1 pause (machine_pause_gcode)
; CUSTOM_GCODE
M117 Hello Claude              ; user custom G-code at layer
M73 C<min>                     ; slicer's own "minutes to next pause". Used only to cross-check the parser in tests
```

Parsed values from the test print (slicer time elapsed since start = total − R at the event):

| Event | Layer | R at event | Elapsed | Orca timeline |
|---|---|---|---|---|
| Pause | 5 | 158 | ~1:10.7 | 1:10:03 ✔ |
| Pause | 10 | 111 | ~1:57.7 | 1:57:09 ✔ |
| Custom G-code (`M117 Hello Claude`) | 14 | 82 | ~2:26.7 | 2:26:09 ✔ |

`R` is the remaining time **rounded down**, so the parser uses `R + 0.5`. With that, the parsed times land 4–10 s after Orca's (1:10:13, 1:57:13, 2:26:13).
Orca shows an event's own time only while you hover its icon on the layer slider; otherwise the slider shows the layer's end time.

## 2. Architecture

```
ha-bambulab entities ──state changes──▶ coordinator (state machine)
    print_status, current_stage,            │
    current_layer, remaining_time,          ├─▶ gcode_parser (pure Python): runs once per print, in the executor
    print_progress, speed_profile,          ├─▶ estimator (pure Python): time-to-event, every update + every 30 s
    subtask_name, gcode_file                ├─▶ notifier: mobile_app notify services
ha-bambulab cache folder ──────────────────▶│
  /config/www/media/ha-bambulab/<serial>/prints/**/<size>-<name>.gcode
                                            └─▶ entities (switch / sensor)  ──▶  dashboard (mushroom + auto-entities)
                         helpers.storage.Store: per-print state survives HA restarts
```

Design rules:
- **Use only public surfaces of ha-bambulab**: entity states and the cache folder, never its Python internals. Find the printer's entities through the device and entity registries (by device plus `translation_key`), so a user renaming an entity does not break anything.
- **The parser and the estimator import no HA code.** They are unit-tested against fixtures taken from your real G-code.
- **One config entry per printer** (works with multiple printers, although you have one).

## 3. Repository layout

```
bambu-events-timeline/
├─ hacs.json
├─ README.md                  install, configure, dashboard YAML
├─ custom_components/bambu_timeline/
│  ├─ manifest.json           after_dependencies: bambu_lab, mobile_app
│  ├─ __init__.py             setup / unload
│  ├─ const.py
│  ├─ config_flow.py          printer device picker + options flow
│  ├─ coordinator.py          print lifecycle state machine, file discovery, timers
│  ├─ gcode_parser.py         pure: stream file → ParsedPrint(events, layer table, M73 table)
│  ├─ estimator.py            pure: printer snapshot + ParsedPrint → per-event ETA/status
│  ├─ notifier.py             recipients, message building, de-duplication
│  ├─ switch.py               event alert switches, recipient switches
│  ├─ sensor.py               next-event timestamp, parse status (diagnostic)
│  ├─ diagnostics.py          downloadable debug dump (parsed events, estimator inputs)
│  ├─ strings.json, translations/en.json
├─ tests/
│  ├─ fixtures/*.gcode        trimmed from your files: header, M73, layer, pause, custom lines only
│  ├─ test_gcode_parser.py
│  └─ test_estimator.py
├─ dashboard/timeline-cards.yaml
└─ .github/workflows/validate.yaml   hassfest + HACS action + pytest
```

## 4. Components

### 4.1 G-code parser (`gcode_parser.py`)
- Streams the file line by line and only looks at lines that start with `;` or `M`. Expected run time for 11 MB on a Pi 4 is about 1–3 s, run in the executor.
- Collects:
  - the header: total estimated time and total layers;
  - an `M73 P/R` table as (line number, P, R);
  - a layer-start table, layer → R, from the `M73 L` lines;
  - events.
- Event types:
  - `pause`: a `; PAUSE_PRINTING` tag, or any `M400 U1`, `M600` or `M601` not already covered by a tag.
  - `custom_gcode`: a `; CUSTOM_GCODE` tag. Its label is the next non-empty G-code lines (up to 3, e.g. `M117 Hello Claude`). If the block contains a pause command, the event becomes `pause`.
- Each event's position:
  - its layer;
  - `r_event` = the last `M73 R` before the event. That is the upper value, so estimates lean early;
  - the source line number.
- Ignores the start and end G-code, `M620/T` swaps, and `M400` without `U1`.
- Tests:
  - expected events for both fixtures;
  - pause positions agree with the slicer's own `M73 C` countdown (within 1 min);
  - AMS swaps produce no events.

### 4.2 Finding the file (coordinator)
- Trigger: `print_status` leaves idle/finish/failed (prepare/running), or HA starts while a print is active.
- Search `…/ha-bambulab/<serial>/prints/**/*.gcode` for files newer than the print start whose stem ends with `subtask_name` (fallback: the `gcode_file` name). If several match, take the newest.
- Poll every 10 s for up to 5 min, because ha-bambulab downloads with a delay. Before parsing, require the file size to stay the same across two checks.
- Expose a parse-status sensor: `waiting_for_file` / `parsing` / `ok (N events)` / `no_events` / `file_not_found` / `error`.

### 4.3 Estimator (`estimator.py`)
Inputs per update: printer remaining time `R_p` (minutes, converted from the sensor's unit), progress `P` (%), current layer `L`, status, speed profile, and the parsed tables.

1. **Slicer position now, `R_s`.** Take the R values the `M73` table holds for progress `P`, and clamp them to the R range of the current layer (from the layer-start table). Between updates, keep it constant.
2. **Rate factor `k`** (real minutes per slicer minute), calculated as `R_p / R_s`, then smoothed and clamped to 0.3–3. Firmware reportedly recalculates remaining time from actual progress, which would make this account for speed-profile changes, skipped objects and slicer error.
   - **Fallback, if calibration (phase 2) shows that `R_p` does *not* react to speed changes:** k = speed factor (silent 2.0, standard 1.0, sport 0.8, ludicrous 0.6, tuned during calibration) × the measured drift.
3. **Time to event** `t = max(0, (R_s − r_event) · k − margin)`, where `margin` = max(1 min, 3 % of t) to keep the estimate early.
4. **While paused:** `t` stays frozen and the ETA (now + t) moves forward. Preparation phase: k = 1 and the ETA is flagged as `estimate_after_start`.
5. **Event status:** `upcoming`, then `active` (printer paused at the event's layer / position), then `done` (position passed: `L > layer`, or `R_s < r_event`, or resumed after being active). A custom G-code event goes straight from `upcoming` to `done`.

### 4.4 Entities (device "A1 Timeline", `has_entity_name`)
| Entity | Purpose |
|---|---|
| `switch.a1_timeline_event_1…N` (N = 10 slots, configurable) | **One row per event.** On/off = alerts for this event. Attributes: `type`, `label` ("Pause · layer 5/24"), `layer`, `eta` (ISO), `minutes_until`, `status`, `gcode`. An unused slot is `unavailable` (filtered out on the dashboard). Defaults: pause → on, custom G-code → off. |
| `sensor.a1_timeline_next_event` | Timestamp (shown as "in 23 minutes"). Attributes: label, type, layer, minutes_until. |
| `switch.a1_timeline_notify_<device>` | One per Companion-app device. **Per-print opt-in**: at every print start each one is reset to the configured default (your phone on, others off). |
| `sensor.a1_timeline_parse_status` | Diagnostic. |

Event slots with stable IDs keep the dashboard YAML fixed. Slots are filled in time order at each print start and cleared at finish, failure or cancel.

### 4.5 Notifications (`notifier.py`)
- **Lead alert:** sent once, when `t ≤ lead_time` (default 5 min) and the event's alert is on. Example: "A1: pause in ~5 min — layer 5/24 (≈14:35)".
- **Event alert** (option "Alert on actual event", default on): sent when the event becomes `active`. Example: "A1 paused at layer 5 — planned pause, waiting for you". For custom G-code: "A1 reached layer 14: M117 Hello Claude".
- Sent to every recipient switch that is on, through `notify.mobile_app_*`. Both alerts carry the tag `bambu_timeline_<event>`, so the event alert replaces the lead alert on the phone. Delivery is time-sensitive on iOS and a high-importance channel on Android.
- The "already sent" flags are persisted, so an HA restart does not cause duplicates.
- Recipient discovery: the registered `notify.mobile_app_*` services, refreshed on reload. A device added later shows up after reloading the integration.

### 4.6 Config / options flow
- Setup: pick the ha-bambulab printer device. The serial is read from it, and the integration checks that the cache folder exists.
- Options:
  - lead time (5 min);
  - alert on actual event (on);
  - default recipients (multi-select, preselected: your phone);
  - custom G-code alerts default (off);
  - early margin (1 min / 3 %);
  - event slots (10);
  - show finished events (off: they disappear; on: they stay, greyed, until the print ends). Each event switch gets a `visible` attribute (status + this option), and the dashboard filters on `visible: true`. That means no YAML changes and no extra entities.

### 4.7 Dashboard (added to the printer's dashboard view, under the print-status card)
- Wrapped in a `conditional` card, visible when `print_status` is prepare/running/pause.
- **Next event:** a `mushroom-template-card` reading `sensor.a1_timeline_next_event`.
- **Event list:** `auto-entities` filtering `switch.a1_timeline_event_*` to status upcoming/active, sorted by `eta`. Each row is a `mushroom-template-card`:
  - primary: label;
  - secondary: "in 23 min · 14:35";
  - icon: bell or bell-off, color by type;
  - tap: turn the alert on or off.
- **Notify on this print:** `auto-entities` listing `switch.a1_timeline_notify_*` as toggles.
- Parse status is shown only when it is not `ok`.

## 5. Delivery phases

| Phase | Content | Done when |
|---|---|---|
| 0 | Repo scaffold, parser, fixtures, tests, CI | `pytest` green locally; parser output matches the table in §1 |
| 1 | Integration skeleton: config flow, entity lookup, file discovery, event slots, k = 1 estimator, parse-status sensor | Installed through HACS; a real print fills the event slots |
| 2 | Full estimator plus a calibration print. Diagnostics record `R_p`, `R_s`, `P`, `L` and speed each minute while you switch speed profiles once or twice. Tune k or the fallback factors from that data. | ETA error ≤ 2 min, always early, across one speed change |
| 3 | Notifier, recipient switches, options flow, persistence across restart | Lead and event alerts reach only the opted-in phones; no duplicates after restarting HA mid-print |
| 4 | Dashboard YAML, README, first tagged release `v0.1.0` | Your phone view shows the next event, the list and the recipients |

## 6. Risks / to verify
- **Firmware behaviour of `R_p` on a speed change:** checked during phase 2 calibration; a fallback is defined.
- **Remaining-time update rate in cloud mode:** if the cloud MQTT sends updates less often, the 30 s local timer covers the gaps.
- **Which `current_stage` value `M400 U1` produces** (`paused_user_gcode`?): the event status does not depend on it (it uses `print_status == pause` plus position), but it is logged.
- **HACS cannot install private repositories:** resolved; the repo will be public (github.com/jpa00), and the README states it is provided as is and built for personal use.

## 7. Progress
- **Phase 0 done:** the parser (`gcode_parser.py`, with a command-line mode), trimmed fixtures, tests, and a pytest CI workflow.
- **Phase 1 done (v0.1.0):** config and options flow, coordinator (print lifecycle, file search, persistence), the full estimator (learned rate factor, early margin), event switches, next-event and G-code status sensors, diagnostics with a minute-by-minute calibration log, dashboard YAML, and CI (tests with HA, hassfest, HACS). 42 tests pass against HA 2026.10.0b4.
  - Changes from the plan: a finished event's switch becomes **unavailable** (unless "keep finished events" is on) rather than getting a `visible` attribute, so the dashboard filter is just "exclude unavailable". Each event also gets a ready-made `countdown` text ("in 23 min · 14:35", "Paused, waiting for you").
  - The lead-time and "alert on event" options are hidden until phase 3 implements notifications.
- **Phase 3 done (v0.2.0), moved ahead of phase 2** so that long prints already waiting can test the display and alerts:
  - per-phone recipient switches (opt-in per print, reset to the default phones when a print ends);
  - a 5-minute heads-up and an "it happened" alert (replaces the heads-up; cleared on resume);
  - no alerts for events already past when the print was first seen, and no duplicates after a restart;
  - a test-notification button, plus a "Send alerts to" section in the dashboard YAML.
  - Fixed while testing: printer sensor updates are now evaluated as one set (they used to be read one at a time, which skewed the rate factor), and a blip in the task-name sensor no longer counts as a new print.
- **Calibration support (v0.2.5):** the printer reports remaining time rounded down to whole minutes; the estimator now interpolates within the minute from the moment the value ticks down (pauses excluded). Each event gets a predicted-vs-actual record (raw estimate 5 and 1 minutes ahead, time reached, time paused), and the last finished print stays in the diagnostics. An optional "in ~45 s" display for the final minute refreshes every 5 seconds.
- **Pace measurement (v0.2.6), from the first long print's diagnostics:** the A1's remaining time is the slicer estimate passed through (no live re-estimate); real printing ran about 3 % slower. The countdown now multiplies by a measured pace (real minutes per printer-minute over the last 20 printing minutes, pauses excluded, after 10 minutes of data). The rate factor k had a rounding bias (printer rounds down, slicer position is mid-minute), now fixed, and is only applied beyond ±3 %. A replay test uses the anonymised readings. Filament changes are recorded with their slicer budget; a pause inside a change block becomes one "Filament change" event. README has a prominent "How accurate is it?" section.
- **Open idea:** filament changes on the external spool (profile-only changes) take almost no time, so the printer's remaining time jumps by the slicer's budget. Subtracting the budget ahead of time for events after such a change would avoid late predictions there. The active-spool sensor (ams_index 254/255 = external) can tell when it applies.
- **Phase 2 next:** the long prints' diagnostics already contain the calibration log. A speed change during one of them covers the calibration; otherwise a small dedicated test print.
