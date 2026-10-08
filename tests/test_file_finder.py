import os
import time

import pytest

from custom_components.bambu_timeline.file_finder import find_gcode, normalize_name

NOW = time.time()
GRACE = 120


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("6556-My Print.gcode.gcode", "my print"),
        ("/cache/My Print.gcode.3mf", "my print"),
        ("My Print", "my print"),
        ("12-My Print.3mf", "my print"),
        ("C:\\x\\2024-plate.gcode", "plate"),
        ("", ""),
    ],
)
def test_normalize_name(name, expected):
    assert normalize_name(name) == expected


def make(tmp_path, relative: str, age_s: float):
    path = tmp_path / "prints" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("; gcode\n")
    os.utime(path, (NOW - age_s, NOW - age_s))
    return path


def test_prefers_file_written_for_this_print(tmp_path):
    make(tmp_path, "cache/100-Box.gcode.gcode", age_s=3600)
    fresh = make(tmp_path, "cache/200-Box.gcode.gcode", age_s=10)
    make(tmp_path, "cache/300-Other.gcode.gcode", age_s=5)
    assert find_gcode([tmp_path], ["Box"], started_ts=NOW - 30, now_ts=NOW, stale_grace_s=GRACE) == fresh


def test_old_copy_only_after_grace_period(tmp_path):
    old = make(tmp_path, "cache/100-Box.gcode.gcode", age_s=3600)
    assert find_gcode([tmp_path], ["Box"], started_ts=NOW - 30, now_ts=NOW, stale_grace_s=GRACE) is None
    assert find_gcode([tmp_path], ["Box"], started_ts=NOW - 300, now_ts=NOW, stale_grace_s=GRACE) == old


def test_without_a_name_only_fresh_files_count(tmp_path):
    make(tmp_path, "cache/100-Box.gcode.gcode", age_s=3600)
    assert find_gcode([tmp_path], ["", ""], started_ts=NOW - 600, now_ts=NOW, stale_grace_s=GRACE) is None
    fresh = make(tmp_path, "200-Anything.gcode", age_s=5)
    assert find_gcode([tmp_path], [""], started_ts=NOW - 30, now_ts=NOW, stale_grace_s=GRACE) == fresh


def test_missing_cache_folder(tmp_path):
    assert find_gcode([tmp_path / "nope"], ["Box"], started_ts=NOW, now_ts=NOW, stale_grace_s=GRACE) is None
