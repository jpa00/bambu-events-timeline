# Bambu Events Timeline

A Home Assistant integration that tells you how long it is until your Bambu Lab printer next needs you.

If you add a pause or some custom G-code at a layer in the slicer, the printer will stop or do something at that point in the print. Neither the printer nor the Bambu Lab integration tells you when that will be. This integration reads the sliced G-code of the current print, finds those events, keeps a running countdown to each one, and can send a heads-up to your phone a few minutes before.

> **Status: early.** It works on my prints, but the countdown hasn't been tuned against much real data yet.

## Before you use this

I built this heavily AI-assisted with Claude Opus 5.5 for my own setup: a Bambu Lab A1 with an AMS lite, sliced in OrcaSlicer, connected to Home Assistant through [ha-bambulab](https://github.com/greghesp/ha-bambulab). It's public so it's easy to install with HACS, and in case it's useful to someone else as-is.

That also means:

- It's provided as is, with no warranty or promise of support (see [LICENSE](LICENSE)).
- I'm unlikely to add features I don't need myself. Bug reports with clear steps to reproduce are welcome. Feature requests may just sit there.
- Other printers and Bambu Studio will probably work, since they write the same markers into the G-code. I just haven't tested them.

## What it detects

| In the slicer | Counted as | Alert by default |
|---|---|---|
| Add pause (layer slider) | Pause | On |
| Add custom G-code (layer slider) | Custom G-code. If the G-code contains a pause command (`M400 U1`, `M600`, `M601`), it counts as a pause instead. | Off |
| Filament changes, AMS swaps | Ignored | – |

Filament changes are ignored on purpose. With an AMS, the slicer turns every filament change into an automatic swap, and the G-code doesn't say whether a swap actually needs a person.

## What you need

- Home Assistant 2026.1 or newer.
- [ha-bambulab](https://github.com/greghesp/ha-bambulab) set up for your printer, **with the printer's IP address and access code filled in**. That's what lets ha-bambulab download the print file from the printer. This integration reads the G-code from ha-bambulab's download folder (`/config/www/media/ha-bambulab/<serial>/prints/`) and never talks to the printer itself.
- An SD card in the printer. Without one, there's no file for ha-bambulab to download.

## Installing

1. In HACS, open the menu (⋮) → **Custom repositories**. Add `https://github.com/jpa00/bambu-events-timeline` with the type **Integration**.
2. Find **Bambu Events Timeline** in HACS, download it, and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration**, search for **Bambu Events Timeline**, and pick your printer.

You'll get a new device called "A1 Timeline" (or whatever your printer model is).

## What it gives you

| Entity | What it is |
|---|---|
| `sensor.a1_timeline_next_event` | When the next event happens. The dashboard shows it as "in 23 minutes". The attributes say what it is (`label`), how long until it (`countdown`, `minutes_until`), and its `status`. |
| `switch.a1_timeline_event_1` … `_10` | One per event in the current print, in order. The switch turns that event's alerts on or off. Its attributes are the same as above. Unused slots, and finished events, are unavailable. |
| `sensor.a1_timeline_g_code_status` | Whether the G-code for the current print was found and read. Check this first if the list stays empty. |
| `switch.a1_timeline_notify_<phone>` | One per phone or tablet with the Home Assistant app. On means that phone gets alerts for this print. |
| `sensor.a1_timeline_alert_recipients` | Who gets alerts right now, by name: "Alex", "Alex, Sam" or "no one". Each person is listed once, however many of their devices are on. |
| `button.a1_timeline_send_test_notification` | Sends a test notification to the phones that are switched on. |

An event's `status` goes from `upcoming` to `due` (it's at that point in the print, so the printer should stop any second) to `active` (the printer is paused at it) to `done`. Custom G-code that doesn't pause goes straight from `upcoming` to `done`.

## Notifications

For each event with its alert on, the phones that are switched on get:

1. **A heads-up** a few minutes before the event (5 by default), e.g. *"A1: pause in 5 min — Pause · layer 5/24 (about 14:35)."*
2. **A second alert when it happens**, e.g. *"A1 paused — Planned pause at layer 5/24. Waiting for you."* This replaces the heads-up on the phone, and it's removed again once the print resumes. You can turn this second alert off in the options.

Who gets alerts is decided **per print**. In the options you choose which phones get them by default. During a print, anyone can switch their phone on or off on the dashboard. When the print ends, everything goes back to the defaults. Switching a phone on between prints applies to the next print.

Names come from the Home Assistant user who set up the Companion app on each device (their person's name if they have one).

Notifications go through the Home Assistant Companion app (`notify.mobile_app_…`), so every phone needs the app logged in to your Home Assistant. On Android they arrive in their own "Printer events" notification channel, which you can adjust in the phone's settings. On iOS they're marked time-sensitive, so they get through Focus modes.

A phone that gets the Companion app after this integration was set up shows up once you reload the integration (**Settings → Devices & services → Bambu Events Timeline → ⋮ → Reload**).

## Dashboard

[`dashboard/timeline-cards.yaml`](dashboard/timeline-cards.yaml) is a ready-made card. It lists the print's upcoming events with a countdown for each, and marks the next one with a small clock badge. Tapping a row turns that event's alert on or off. When there's nothing to list, a status line says why (still looking for the G-code, no pauses in this print, and so on). Below that is a collapsed "Sending alerts to Alex" row; open it to switch devices on or off for this print or to send a test. It needs the [Mushroom](https://github.com/piitaya/lovelace-mushroom), [auto-entities](https://github.com/thomasloven/lovelace-auto-entities) and [collapsable-cards](https://github.com/RossMcMillan92/lovelace-collapsable-cards) cards from HACS. Open your dashboard in edit mode, add a **Manual** card, and paste the file's contents. The card writes out three entity IDs (`sensor.a1_timeline_g_code_status`, `sensor.a1_timeline_next_event` and `sensor.a1_timeline_alert_recipients`). If Home Assistant named yours differently, for example with the area in front (`sensor.workshop_a1_timeline_next_event`), change them in the YAML or rename the entities. The entity IDs in the tables above have the same caveat. The event list only shows while something is printing.

## How the countdown works

The slicer writes its own time estimate into the G-code every minute or so. From that, the integration knows how many slicer-minutes before the end of the print each event is. While printing, it compares the printer's own remaining time with where the print is on the slicer's timeline, and uses that ratio to convert the event's position into real minutes. In practice:

- **Pauses** (planned or not) stop the countdown. It continues when the print resumes.
- **Speed changes** (silent, sport, ludicrous) are followed after a few minutes.
- **It runs early on purpose.** Countdowns are shortened by a safety margin: 1 minute or 3 %, whichever is larger. You can change that in the options.

The times it finds should match what OrcaSlicer shows when you hover an event's icon on the preview's layer slider, to within a few seconds. Without hovering, Orca shows the layer's *end* time instead, which can be quite a bit later.

## Settings

**Settings → Devices & services → Bambu Events Timeline → Configure:**

- **Notify by default.** The phones that get alerts unless switched off for a print.
- **Advance warning.** How many minutes before an event the heads-up goes out. 0 turns heads-ups off.
- **Also alert when the event happens.** On by default.
- **Alerts for custom G-code on by default.** Pauses always start with alerts on.
- **Keep finished events on the list.** Off by default.
- **Safety margin.** Minutes and percent; the larger one is used.
- **Maximum number of events.** 10 by default.

## When something doesn't work

- **"File not found":** ha-bambulab didn't download the print file. Check that it has the printer's IP address and access code, and that the printer has an SD card. Look in `/config/www/media/ha-bambulab/<serial>/prints/` for a `.gcode` file with your print's name.
- **No notifications:** press **Send test notification**. If that doesn't arrive either, check that the phone's switch is on and that `notify.mobile_app_<your phone>` exists under **Developer tools → Actions**.
- **The countdown is off:** download the diagnostics (**Settings → Devices & services → Bambu Events Timeline → ⋮ → Download diagnostics**) during or right after a print. They contain a minute-by-minute log of the printer's numbers next to the slicer's. Attach them to a bug report.
- **Updates don't show up:** Home Assistant's update list works from GitHub releases. A release is published automatically whenever the version number changes, so HACS may show a newer commit before Home Assistant lists it as an update.
- **Check a G-code file without Home Assistant:**

  ```
  python custom_components/bambu_timeline/gcode_parser.py my_print.gcode
  ```

  This prints what the integration will find:

  ```
  Slicer: OrcaSlicer 2.4.2
  Estimated total: 3:48:43, layers: 24
    1:10:13  layer    5  Pause  (line 109797)
    1:57:13  layer   10  Pause  (line 244045)
    2:26:13  layer   14  Message: Hello Claude  (line 331214)
  ```

## Development

The parser, estimator and file finder are plain Python and test without Home Assistant:

```
python -m venv .venv
.venv/Scripts/python -m pip install pytest      # on Linux/macOS: .venv/bin/python
.venv/Scripts/python -m pytest
```

The integration tests need Home Assistant's test harness, which needs Python 3.14 and Linux (WSL works):

```
pip install pytest-homeassistant-custom-component
pytest
```

The test fixtures in `tests/fixtures/` are real OrcaSlicer output, trimmed down with `scripts/trim_gcode.py` so that only the lines the parser looks at are left.
