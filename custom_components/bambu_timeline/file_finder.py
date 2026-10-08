"""Find the current print's G-code in ha-bambulab's file cache.

ha-bambulab downloads the print's 3MF from the printer and extracts the plate's
G-code next to it, as ``<cache>/prints/<printer folder>/<size>-<name>.gcode``.
No Home Assistant imports here.
"""

from __future__ import annotations

import re
from pathlib import Path

_SIZE_PREFIX = re.compile(r"^\d+-")
_SUFFIXES = (".gcode", ".3mf")
# A file written up to this long before the print was noticed still counts as fresh.
_FRESH_TOLERANCE_S = 60


def normalize_name(name: str) -> str:
    """``"/cache/123-My print.gcode.3mf"`` and ``"My print"`` both become ``"my print"``."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _SIZE_PREFIX.sub("", name)
    changed = True
    while changed:
        changed = False
        for suffix in _SUFFIXES:
            if name.lower().endswith(suffix):
                name = name[: -len(suffix)]
                changed = True
    return name.strip().casefold()


def find_gcode(
    roots: list[Path],
    names: list[str],
    started_ts: float,
    now_ts: float,
    stale_grace_s: float,
) -> Path | None:
    """Return the best-matching cached G-code file, or None if it isn't there (yet).

    A file written after the print started is preferred. An older file with the
    right name (a reprint) is only accepted once ``stale_grace_s`` has passed,
    to give ha-bambulab time to download a newer copy.
    """
    wanted = {normalize_name(name) for name in names if name}
    wanted.discard("")
    candidates: list[tuple[float, Path]] = []
    for root in roots:
        prints = root / "prints"
        if not prints.is_dir():
            continue
        for path in prints.rglob("*.gcode"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            candidates.append((mtime, path))

    fresh_after = started_ts - _FRESH_TOLERANCE_S
    if wanted:
        matches = [(mtime, path) for mtime, path in candidates if normalize_name(path.name) in wanted]
    else:
        # The printer didn't report a name: only trust a file that appeared for this print.
        matches = [(mtime, path) for mtime, path in candidates if mtime >= fresh_after]

    fresh = [item for item in matches if item[0] >= fresh_after]
    if fresh:
        return max(fresh)[1]
    if matches and now_ts - started_ts >= stale_grace_s:
        return max(matches)[1]
    return None
