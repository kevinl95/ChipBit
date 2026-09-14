"""Daily screen-time accounting.

What counts as screen time is *time a title is actually running*. The idle
kiosk does not count: a Pi left switched on in a family room should not eat a
child's allowance while nobody is using it.

The counter is written to disk as it accrues, so power-cycling the Pi is not a
way around it. It resets on the local calendar day, which is the boundary a
family already thinks in -- but only once the clock is worth believing. See
``clock_is_synced``.

There is no limit by default. A device that has never had one set behaves
exactly as it did before this existed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

LIMIT_FILE = Path("/var/lib/chipbit/screen_time_limit")
USAGE_FILE = Path("/var/lib/chipbit/screen_time_usage")

# Guard rails on what a parent can type into the box.  A day is 1440 minutes;
# anything at or beyond that is indistinguishable from "no limit", so the box
# stops short of it.
MAX_LIMIT_MINUTES = 1439

# systemd-timesyncd creates this once it has corrected the clock from the
# network.  It is the same marker systemd-time-wait-sync.service waits on.
CLOCK_SYNCED_MARKER = Path("/run/systemd/timesync/synchronized")


@dataclass(frozen=True)
class Usage:
    """How much screen time has been used on ``day``."""

    day: str
    seconds: int


def today() -> str:
    """Local calendar day, the boundary the allowance resets on."""
    return date.today().isoformat()


def clock_is_synced(path: Path | None = None) -> bool:
    """Whether the clock has been corrected from the network since boot.

    A Pi has no battery-backed clock.  At boot it restores roughly the time it
    was last shut down at, so a device switched off overnight wakes up still
    on yesterday's calendar day -- and yesterday's usage still applies, which
    locks a child out of a day that has already ended.  Until this is true,
    nothing ``today()`` says about the calendar can be trusted.
    """
    return (path or CLOCK_SYNCED_MARKER).exists()


def read_limit(path: Path | None = None) -> int:
    """Daily limit in minutes. 0 means no limit, and is the default."""
    target = path or LIMIT_FILE
    try:
        raw = target.read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    try:
        minutes = int(raw)
    except ValueError:
        log.warning("ignoring unreadable screen time limit in %s: %r", target, raw)
        return 0
    return max(0, min(minutes, MAX_LIMIT_MINUTES))


def write_limit(minutes: int, path: Path | None = None) -> int:
    """Store the daily limit, clamped. Returns what was actually stored."""
    if minutes < 0:
        raise ValueError("screen time limit cannot be negative")
    clamped = min(int(minutes), MAX_LIMIT_MINUTES)
    target = path or LIMIT_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{clamped}\n", encoding="utf-8")
    return clamped


def read_usage(path: Path | None = None, *, day: str | None = None) -> Usage:
    """Usage so far today.

    A stored day that is not today reads as zero rather than being rewritten,
    so a device that sits unused overnight does not need a write to roll over.
    """
    current = day or today()
    target = path or USAGE_FILE
    try:
        raw = target.read_text(encoding="utf-8").strip()
    except OSError:
        return Usage(current, 0)
    stored_day, _, stored_seconds = raw.partition(" ")
    if stored_day != current:
        return Usage(current, 0)
    try:
        return Usage(current, max(0, int(stored_seconds)))
    except ValueError:
        log.warning("ignoring unreadable screen time usage in %s: %r", target, raw)
        return Usage(current, 0)


def write_usage(usage: Usage, path: Path | None = None) -> None:
    target = path or USAGE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{usage.day} {usage.seconds}\n", encoding="utf-8")


def add_seconds(
    seconds: float, path: Path | None = None, *, day: str | None = None
) -> Usage:
    """Add elapsed time to today's total and persist it."""
    current = day or today()
    usage = read_usage(path, day=current)
    updated = Usage(current, usage.seconds + max(0, int(seconds)))
    try:
        write_usage(updated, path)
    except OSError as exc:
        # Accrual runs on a timer and from stop_current(). Losing the count is
        # bad; failing to stop a title because the count could not be written
        # would be worse.
        log.warning("could not record screen time usage: %s", exc)
    return updated


def reset_usage(path: Path | None = None, *, day: str | None = None) -> Usage:
    """Clear today's total.

    This is how a parent grants "five more minutes" without also changing
    tomorrow, which raising the limit would do.
    """
    cleared = Usage(day or today(), 0)
    write_usage(cleared, path)
    return cleared


def remaining_seconds(limit_minutes: int, usage: Usage) -> int | None:
    """Seconds left today, or None when no limit is set."""
    if limit_minutes <= 0:
        return None
    return max(0, limit_minutes * 60 - usage.seconds)


def is_exhausted(limit_minutes: int, usage: Usage) -> bool:
    remaining = remaining_seconds(limit_minutes, usage)
    return remaining is not None and remaining <= 0


def snapshot(limit_minutes: int, usage: Usage) -> dict[str, object]:
    """The shape the control API and the kiosk read."""
    return {
        "limit_minutes": limit_minutes,
        "used_seconds": usage.seconds,
        "remaining_seconds": remaining_seconds(limit_minutes, usage),
        "exhausted": is_exhausted(limit_minutes, usage),
        "day": usage.day,
    }
