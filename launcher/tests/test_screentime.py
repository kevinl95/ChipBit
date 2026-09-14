"""Daily screen-time accounting and the promises it makes to a family."""

from __future__ import annotations

from pathlib import Path

import pytest

from chipbit import screentime


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "limit", tmp_path / "usage"


def test_no_limit_by_default(paths) -> None:
    """A device that never had a limit set must behave as it always did."""
    limit_path, usage_path = paths
    assert screentime.read_limit(limit_path) == 0
    usage = screentime.read_usage(usage_path)
    assert usage.seconds == 0
    assert screentime.remaining_seconds(0, usage) is None
    assert screentime.is_exhausted(0, usage) is False


def test_usage_accrues_and_persists(paths) -> None:
    _limit, usage_path = paths
    screentime.add_seconds(90, usage_path)
    screentime.add_seconds(30, usage_path)
    assert screentime.read_usage(usage_path).seconds == 120


def test_usage_survives_a_restart_but_not_a_new_day(paths) -> None:
    """Power-cycling must not be a way around the limit; midnight must be."""
    _limit, usage_path = paths
    screentime.add_seconds(600, usage_path, day="2026-08-30")

    # same day, fresh read (as after a reboot)
    assert screentime.read_usage(usage_path, day="2026-08-30").seconds == 600
    # next day
    assert screentime.read_usage(usage_path, day="2026-08-31").seconds == 0


def test_rollover_needs_no_write(paths) -> None:
    """A Pi asleep overnight should roll over without touching the card."""
    _limit, usage_path = paths
    screentime.add_seconds(600, usage_path, day="2026-08-30")
    before = usage_path.read_text()
    assert screentime.read_usage(usage_path, day="2026-09-05").seconds == 0
    assert usage_path.read_text() == before


def test_exhaustion_is_computed_from_limit_and_usage(paths) -> None:
    usage = screentime.Usage("2026-08-30", 30 * 60)
    assert screentime.is_exhausted(30, usage) is True
    assert screentime.is_exhausted(45, usage) is False
    assert screentime.remaining_seconds(45, usage) == 15 * 60


def test_reset_clears_today_only(paths) -> None:
    """"Five more minutes" must not also move tomorrow's allowance."""
    limit_path, usage_path = paths
    screentime.write_limit(30, limit_path)
    screentime.add_seconds(30 * 60, usage_path)
    assert screentime.is_exhausted(
        screentime.read_limit(limit_path), screentime.read_usage(usage_path)
    )

    screentime.reset_usage(usage_path)
    assert screentime.read_usage(usage_path).seconds == 0
    assert screentime.read_limit(limit_path) == 30, "the limit itself is untouched"


def test_limit_is_clamped_and_round_trips(paths) -> None:
    limit_path, _usage = paths
    assert screentime.write_limit(45, limit_path) == 45
    assert screentime.read_limit(limit_path) == 45
    # a whole day or more is indistinguishable from no limit
    assert screentime.write_limit(5000, limit_path) == screentime.MAX_LIMIT_MINUTES
    with pytest.raises(ValueError):
        screentime.write_limit(-1, limit_path)


def test_corrupt_files_read_as_no_limit_rather_than_locking_a_child_out(
    paths,
) -> None:
    """Failing open is the safe direction: the worst case is an unenforced
    limit, not a device that refuses to launch anything."""
    limit_path, usage_path = paths
    limit_path.write_text("half an hour\n")
    usage_path.write_text("2026-08-30 lots\n")
    assert screentime.read_limit(limit_path) == 0
    assert screentime.read_usage(usage_path, day="2026-08-30").seconds == 0


def test_accrual_never_raises_when_the_counter_cannot_be_written(
    tmp_path: Path,
) -> None:
    """Accrual runs from stop_current(); it must not be able to prevent a stop."""
    unwritable = tmp_path / "nope" / "usage"
    unwritable.parent.mkdir()
    unwritable.parent.chmod(0o500)
    try:
        result = screentime.add_seconds(60, unwritable)
        assert result.seconds == 60, "still reports the in-memory total"
    finally:
        unwritable.parent.chmod(0o700)


def test_snapshot_shape(paths) -> None:
    usage = screentime.Usage("2026-08-30", 10 * 60)
    snap = screentime.snapshot(30, usage)
    assert snap == {
        "limit_minutes": 30,
        "used_seconds": 600,
        "remaining_seconds": 20 * 60,
        "exhausted": False,
        "day": "2026-08-30",
    }


def test_clock_sync_is_read_from_the_timesyncd_marker(tmp_path: Path) -> None:
    marker = tmp_path / "synchronized"
    assert screentime.clock_is_synced(marker) is False
    marker.touch()
    assert screentime.clock_is_synced(marker) is True
