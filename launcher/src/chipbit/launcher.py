"""Launcher daemon state machine and file-backed configuration."""

from __future__ import annotations

import logging
import os
import pwd
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from . import screentime
from .models import (
    CardsConfig,
    Catalog,
    CatalogTitle,
    ConfigLoadError,
    SystemCard,
    load_cards,
    load_catalog_merged,
    normalize_uid,
    resolve_title_content_path,
    save_cards,
)
from .strings import child_locale_env, generated_locales, read_language

log = logging.getLogger(__name__)

DEFAULT_CAPTURE_TIMEOUT_SECS = 30.0
DEFAULT_POLL_SECS = 2.0
DEFAULT_STOP_GRACE_SECS = 5.0
DEFAULT_UNLOCK_TIMEOUT_SECS = 300.0
DEFAULT_STATUS_EVENT_TTL_SECS = 5.0
WhileRunningPolicy = Literal["home_only", "swap", "ignore"]
_VALID_WHILE_RUNNING = frozenset({"home_only", "swap", "ignore"})


@dataclass(frozen=True)
class LaunchSettings:
    while_running: WhileRunningPolicy = "home_only"
    capture_timeout_secs: float = DEFAULT_CAPTURE_TIMEOUT_SECS
    stop_grace_secs: float = DEFAULT_STOP_GRACE_SECS
    unlock_timeout_secs: float = DEFAULT_UNLOCK_TIMEOUT_SECS
    scummvm_bin: str = "scummvm"
    chromium_bin: str = "chromium"
    ruffle_bin: str = "ruffle"
    allow_shutdown: bool = False
    shutdown_command: tuple[str, ...] = ("systemctl", "poweroff")
    volume_command: tuple[str, ...] = ("amixer", "set", "Master", "toggle")

    def __post_init__(self) -> None:
        if self.while_running not in _VALID_WHILE_RUNNING:
            raise ValueError(f"invalid while_running policy: {self.while_running!r}")
        if self.unlock_timeout_secs <= 0:
            raise ValueError("unlock_timeout_secs must be greater than zero")


class FileBackedConfig:
    """Load catalog/cards from disk and refresh them when mtimes change."""

    def __init__(
        self,
        catalog_path: Path,
        cards_path: Path,
        user_catalog_path: Path | None = None,
    ) -> None:
        self.catalog_path = catalog_path
        self.cards_path = cards_path
        self.user_catalog_path = user_catalog_path
        self.catalog = Catalog(catalog_version=None, titles={})
        self.cards = CardsConfig()
        self._catalog_mtime: int | None = None
        self._cards_mtime: int | None = None
        self._user_catalog_mtime: int | None = None

    def load(self, *, force: bool = False) -> bool:
        """Reload the catalog/cards if their mtimes changed or force is set."""
        try:
            catalog_mtime = _stat_mtime(self.catalog_path, allow_missing=False)
            cards_mtime = _stat_mtime(self.cards_path, allow_missing=True)
            user_catalog_mtime = _stat_mtime(
                self.user_catalog_path, allow_missing=True
            ) if self.user_catalog_path else None
        except OSError as exc:
            log.error("config stat failed: %s", exc)
            return False

        if (
            not force
            and catalog_mtime == self._catalog_mtime
            and cards_mtime == self._cards_mtime
            and user_catalog_mtime == self._user_catalog_mtime
        ):
            return False

        try:
            catalog = load_catalog_merged(self.catalog_path, self.user_catalog_path)
            cards = load_cards(self.cards_path)
        except ConfigLoadError as exc:
            log.error("config load failed, keeping previous: %s", exc)
            return False

        self.catalog = catalog
        self.cards = cards
        self._catalog_mtime = catalog_mtime
        self._cards_mtime = cards_mtime
        self._user_catalog_mtime = user_catalog_mtime
        log.info(
            "loaded %d title(s), %d title card(s), %d system card(s)",
            len(self.catalog.titles),
            len(self.cards.title_cards),
            len(self.cards.system_cards),
        )
        return True

    def cards_snapshot(self) -> dict[str, dict[str, str]]:
        """Return the enrolled/system cards in a JSON-friendly shape."""
        return {
            "cards": {
                uid: card.title_id
                for uid, card in sorted(self.cards.title_cards.items())
            },
            "system": {
                action: card.uid
                for action, card in sorted(self.cards.system_cards.items())
            },
        }


class LauncherService:
    """Own the launch/capture state machine and current child process."""

    def __init__(
        self,
        config: FileBackedConfig,
        *,
        settings: LaunchSettings | None = None,
        popen_factory: Callable[..., object] = subprocess.Popen,
        killpg: Callable[[int, int], None] = os.killpg,
        getpgid: Callable[[int], int] = os.getpgid,
        thread_factory: Callable[..., threading.Thread] | None = threading.Thread,
        monotonic: Callable[[], float] = time.monotonic,
        language_path: Path | None = None,
        screen_time_limit_path: Path | None = None,
        screen_time_usage_path: Path | None = None,
    ) -> None:
        self.config = config
        self.settings = settings or LaunchSettings()
        self._popen_factory = popen_factory
        self._killpg = killpg
        self._getpgid = getpgid
        self._thread_factory = thread_factory
        self._monotonic = monotonic
        self._generated_locales: frozenset[str] | None = None
        self._language_path = language_path
        self._screen_time_limit_path = screen_time_limit_path
        self._screen_time_usage_path = screen_time_usage_path
        # Monotonic reading at the last accrual, or None when nothing is
        # running.  Monotonic so a clock change cannot hand back free time.
        self._screen_time_mark: float | None = None

        self._lock = threading.RLock()
        self._current_process: object | None = None
        self._current_title: CatalogTitle | None = None
        self._capture_armed = False
        self._capture_uid: str | None = None
        self._capture_event = threading.Event()
        self._unlock_deadline: float | None = None
        self._last_event: dict[str, str] | None = None
        self._last_event_deadline: float | None = None

    def on_scan(self, uid: str) -> None:
        """Handle a completed UID scan from either reader implementation."""
        normalized_uid = normalize_uid(uid)

        with self._lock:
            if self._capture_armed:
                self._capture_uid = normalized_uid
                self._capture_armed = False
                self._clear_last_event_locked()
                self._capture_event.set()
                log.info("captured uid %s", normalized_uid)
                # If the admin card was tapped during enrollment, keep the unlock
                # active so the enrollment itself still goes through.
                if self.config.cards.system_action_for_uid(normalized_uid) == "unlock":
                    self._unlock_deadline = (
                        self._monotonic() + self.settings.unlock_timeout_secs
                    )
                return
            if "unlock" not in self.config.cards.system_cards:
                self._auto_enroll_admin_locked(normalized_uid)
                return

        system_action = self.config.cards.system_action_for_uid(normalized_uid)
        if system_action is not None:
            self._handle_system_action(system_action)
            return

        title_id = self.config.cards.title_id_for_uid(normalized_uid)
        if title_id is None:
            with self._lock:
                self._set_last_event_locked("unknown-card", uid=normalized_uid)
            log.info("unknown card %s", normalized_uid)
            return

        title = self.config.catalog.titles.get(title_id)
        if title is None:
            with self._lock:
                self._set_last_event_locked("unknown-card", uid=normalized_uid)
            log.warning(
                "card %s points to unknown catalog id %s",
                normalized_uid,
                title_id,
            )
            return

        with self._lock:
            self._clear_last_event_locked()

        self._launch_title(title)

    def capture(self, timeout: float | None = None) -> str | None:
        """Arm one-shot capture mode and return the next scanned UID."""
        wait_timeout = (
            self.settings.capture_timeout_secs if timeout is None else timeout
        )
        with self._lock:
            self._capture_uid = None
            self._capture_armed = True
            self._clear_last_event_locked()
            self._capture_event.clear()

        captured = self._capture_event.wait(wait_timeout)
        with self._lock:
            self._capture_armed = False
            return self._capture_uid if captured else None

    def reload(self, *, force: bool = True) -> bool:
        """Reload catalog/cards from disk."""
        return self.config.load(force=force)

    def lock(self) -> None:
        """Immediately relock configuration mode."""
        with self._lock:
            self._unlock_deadline = None

    def unlock(self) -> None:
        """Grant a fresh unlock window without a card tap (e.g. after WiFi setup)."""
        with self._lock:
            self._unlock_deadline = (
                self._monotonic() + self.settings.unlock_timeout_secs
            )

    def stop_current(self) -> None:
        """Stop the current child process group, escalating if needed."""
        self._accrue_screen_time()
        with self._lock:
            process = self._current_process

        if process is None:
            return
        if process.poll() is not None:
            self._clear_current(process)
            return

        try:
            process_group = self._getpgid(process.pid)
            self._killpg(process_group, signal.SIGTERM)
            try:
                process.wait(timeout=self.settings.stop_grace_secs)
            except subprocess.TimeoutExpired:
                self._killpg(process_group, signal.SIGKILL)
                try:
                    process.wait(timeout=self.settings.stop_grace_secs)
                except subprocess.TimeoutExpired:
                    pass
        except ProcessLookupError:
            pass

        self._clear_current(process)
        log.info("stopped current app -> idle")

    def is_running(self) -> bool:
        """Return whether a child process is still active."""
        with self._lock:
            return (
                self._current_process is not None
                and self._current_process.poll() is None
            )

    def _child_env(self) -> dict[str, str]:
        """Environment for a launched title, including its language.

        Read per launch rather than cached at startup so a parent changing the
        language in the console takes effect on the next card tap, with no
        service restart.  The generated-locale list is cached, since that only
        changes when a locale is installed.
        """
        env = dict(os.environ)
        if self._generated_locales is None:
            self._generated_locales = generated_locales()
        env.update(
            child_locale_env(
                read_language(self._language_path),
                generated=self._generated_locales,
            )
        )
        return env

    def status(self) -> dict[str, object]:
        """Return daemon state for the control API and kiosk shell."""
        with self._lock:
            running = (
                self._current_process is not None
                and self._current_process.poll() is None
            )
            current = None if self._current_title is None else self._current_title.label
            return {
                "running": running,
                "current": current,
                "current_id": (
                    None if self._current_title is None else self._current_title.id
                ),
                "current_art": (
                    None if self._current_title is None else self._current_title.art
                ),
                "unlocked": self._is_unlocked_locked(),
                "cards": len(self.config.cards.title_cards),
                "capture_mode": self._capture_armed,
                "last_event": self._last_event_locked(),
                "screen_time": screentime.snapshot(
                    screentime.read_limit(self._screen_time_limit_path),
                    screentime.read_usage(self._screen_time_usage_path),
                ),
            }

    def cards_snapshot(self) -> dict[str, dict[str, str]]:
        """Return the current cards mapping for the control API."""
        return self.config.cards_snapshot()

    def _launch_title(self, title: CatalogTitle) -> None:
        if screentime.is_exhausted(
            screentime.read_limit(self._screen_time_limit_path),
            screentime.read_usage(self._screen_time_usage_path),
        ):
            # The admin card is a system action handled before we ever get
            # here, so a parent can still unlock and change the limit.
            log.info("screen time used up; refusing to launch %s", title.label)
            return

        policy = self.settings.while_running
        if self.is_running():
            if policy in {"home_only", "ignore"}:
                log.info("ignoring %s (app running, policy=%s)", title.label, policy)
                return
            self.stop_current()

        argv = build_launch_argv(
            title,
            self.config.catalog.settings.games_root,
            self.settings,
        )

        home = Path(os.environ.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir)
        for rel in title.user_dirs:
            try:
                (home / rel).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warning("could not create user_dir %s: %s", rel, exc)

        try:
            process = self._popen_factory(
                argv, start_new_session=True, env=self._child_env()
            )
        except (OSError, ValueError) as exc:
            log.error("launch failed for %s: %s", title.label, exc)
            return

        with self._lock:
            self._current_process = process
            self._current_title = title
            self._screen_time_mark = self._monotonic()
        log.info("launched %s (pid %d)", title.label, process.pid)

        if self._thread_factory is not None:
            reaper = self._thread_factory(
                target=self._reap_process,
                args=(process,),
                daemon=True,
            )
            reaper.start()

    def _reap_process(self, process: object) -> None:
        process.wait()
        self._clear_current(process)
        log.info("app exited -> idle")

    def screen_time(self) -> dict[str, object]:
        """Today's allowance, usage and whether it has run out."""
        limit = screentime.read_limit(self._screen_time_limit_path)
        usage = screentime.read_usage(self._screen_time_usage_path)
        return screentime.snapshot(limit, usage)

    def clear_screen_time(self) -> None:
        """Start today's allowance over.

        The accrual mark moves to now as well, so that time banked against the
        old total does not land on the new one the next time a tick runs.
        """
        with self._lock:
            self._screen_time_mark = self._monotonic() if self.is_running() else None
            screentime.reset_usage(self._screen_time_usage_path)

    def _accrue_screen_time(self) -> None:
        """Bank the time since the last accrual, if a title is running."""
        with self._lock:
            mark = self._screen_time_mark
            running = (
                self._current_process is not None
                and self._current_process.poll() is None
            )
            now = self._monotonic()
            self._screen_time_mark = now if running else None
        if mark is None or not running:
            return
        elapsed = now - mark
        if elapsed <= 0:
            return
        screentime.add_seconds(elapsed, self._screen_time_usage_path)

    def tick_screen_time(self) -> bool:
        """Bank elapsed time and stop the title if the allowance is gone.

        Returns True if this tick ended a session.  Called on a timer rather
        than at launch/stop alone, because the limit has to bite part way
        through a long session, not only when one ends.
        """
        self._accrue_screen_time()
        if not self.is_running():
            return False
        if not screentime.is_exhausted(
            screentime.read_limit(self._screen_time_limit_path),
            screentime.read_usage(self._screen_time_usage_path),
        ):
            return False
        log.info("screen time used up; stopping current title")
        self.stop_current()
        return True

    def _clear_current(self, process: object) -> None:
        with self._lock:
            if self._current_process is process:
                self._current_process = None
                self._current_title = None

    def _handle_system_action(self, action: str) -> None:
        log.info("system action: %s", action)
        if action == "home":
            self.stop_current()
            return
        if action == "unlock":
            self.stop_current()
            with self._lock:
                self._unlock_deadline = (
                    self._monotonic() + self.settings.unlock_timeout_secs
                )
            return
        if action == "shutdown":
            if self.settings.allow_shutdown:
                self._popen_factory(
                    list(self.settings.shutdown_command),
                    start_new_session=True,
                )
            return
        if action == "volume":
            self._popen_factory(
                list(self.settings.volume_command),
                start_new_session=True,
            )

    def _is_unlocked_locked(self) -> bool:
        if self._unlock_deadline is None:
            return False
        if self._monotonic() >= self._unlock_deadline:
            self._unlock_deadline = None
            return False
        return True

    def _auto_enroll_admin_locked(self, uid: str) -> None:
        cards = self.config.cards
        system_cards = dict(cards.system_cards)
        system_cards["unlock"] = SystemCard(action="unlock", uid=uid)
        save_cards(
            self.config.cards_path,
            CardsConfig(
                title_cards=dict(cards.title_cards),
                system_cards=system_cards,
            ),
        )
        self._clear_last_event_locked()
        self.config.load(force=True)
        # The card tap is proof of physical possession — start an unlock session
        # immediately so the setup page (shown right after enrollment) can
        # configure WiFi without asking the admin to tap again.
        self._unlock_deadline = self._monotonic() + self.settings.unlock_timeout_secs
        log.info("first-run admin card enrolled as %s", uid)

    def _last_event_locked(self) -> dict[str, str] | None:
        if self._last_event is None or self._last_event_deadline is None:
            return None
        if self._monotonic() >= self._last_event_deadline:
            self._clear_last_event_locked()
            return None
        return dict(self._last_event)

    def _set_last_event_locked(self, kind: str, *, uid: str | None = None) -> None:
        event = {"kind": kind}
        if uid is not None:
            event["uid"] = uid
        self._last_event = event
        self._last_event_deadline = self._monotonic() + DEFAULT_STATUS_EVENT_TTL_SECS

    def _clear_last_event_locked(self) -> None:
        self._last_event = None
        self._last_event_deadline = None


def build_launch_argv(
    title: CatalogTitle,
    games_root: Path,
    settings: LaunchSettings,
) -> list[str]:
    """Return the argv used to launch a title."""
    if title.type == "scummvm":
        content_path = resolve_title_content_path(title, games_root)
        if content_path is None or title.game_id is None:
            raise ValueError("scummvm title is missing a resolved content path")
        return [
            settings.scummvm_bin,
            "-f",
            "-p",
            str(content_path),
            title.game_id,
        ]
    if title.type == "exec":
        return list(title.cmd)
    if title.type == "ruffle":
        content_path = resolve_title_content_path(title, games_root)
        if content_path is None:
            raise ValueError("ruffle title is missing a resolved content path")
        try:
            swf_rel = Path(content_path).relative_to(games_root)
        except ValueError:
            swf_rel = Path(content_path).name
        player_url = (
            "http://127.0.0.1:8080/ruffle/player.html"
            f"?swf=/swf/{quote(str(swf_rel), safe='/')}"
        )
        return [
            settings.chromium_bin,
            "--ozone-platform=wayland",
            "--kiosk",
            "--noerrdialogs",
            "--no-first-run",
            "--disable-features=TranslateUI",
            "--user-data-dir=/tmp/chipbit-ruffle",
            f"--app={player_url}",
        ]
    return [
        settings.chromium_bin,
        "--ozone-platform=wayland",
        "--kiosk",
        "--noerrdialogs",
        "--no-first-run",
        "--disable-pinch",
        "--disable-features=TranslateUI",
        "--overscroll-history-navigation=0",
        "--user-data-dir=/tmp/chipbit-web-app",
        f"--app={title.url or ''}",
    ]


DEFAULT_SCREEN_TIME_POLL_SECS = 20.0

# How long to give the network to correct the clock before concluding it never
# will.  Long enough to cover WiFi association plus an NTP round trip on a cold
# boot; short enough that a child is not staring at a lockout screen for the
# whole of it.
DEFAULT_CLOCK_WAIT_SECS = 120.0


def await_clock_sync(
    service: LauncherService,
    stop: threading.Event,
    wait_secs: float = DEFAULT_CLOCK_WAIT_SECS,
    poll_secs: float = 2.0,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """Drop a stale allowance when the clock turns out to be unknowable.

    A Pi has no battery-backed clock, so one switched off overnight boots
    believing it is still last night: the same calendar day, with yesterday's
    usage still counting against it.  The child is locked out of a day that
    already ended.

    If the network corrects the clock we do nothing at all -- ``read_usage``
    compares against the real day and rolls over by itself, including for a
    reboot part way through today, which must *not* hand back spent time.
    Only when sync never arrives is the day genuinely unknowable, and a daily
    allowance cannot be enforced honestly against a date that might be wrong.
    Then it starts fresh for this boot.

    Returns True if the allowance was cleared.
    """
    started = monotonic()
    while True:
        if screentime.clock_is_synced():
            return False
        if monotonic() - started >= wait_secs:
            break
        if stop.wait(poll_secs):
            return False
    log.warning(
        "clock still unsynced after %.0fs; clearing screen time for this boot",
        wait_secs,
    )
    try:
        service.clear_screen_time()
    except OSError as exc:
        # Same bargain as accrual: losing the count is bad, but refusing to
        # boot a usable device over it would be worse.
        log.warning("could not clear screen time: %s", exc)
        return False
    return True


def poll_screen_time(
    service: LauncherService,
    stop: threading.Event,
    poll_secs: float = DEFAULT_SCREEN_TIME_POLL_SECS,
) -> None:
    """Bank screen time and enforce the daily limit.

    The interval is the worst-case overshoot past the limit, and also how much
    usage a hard power-off can lose. Twenty seconds keeps both small without
    writing to the SD card constantly.
    """
    while not stop.wait(poll_secs):
        try:
            service.tick_screen_time()
        except Exception:
            log.exception("screen time tick failed; continuing")


def poll_config(
    config: FileBackedConfig,
    stop: threading.Event,
    poll_secs: float = DEFAULT_POLL_SECS,
) -> None:
    """Poll config files for mtime changes."""
    while not stop.wait(poll_secs):
        config.load(force=False)


def _stat_mtime(path: Path, *, allow_missing: bool) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
