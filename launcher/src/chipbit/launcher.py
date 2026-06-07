"""Launcher daemon state machine and file-backed configuration."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .models import (
    CardsConfig,
    Catalog,
    CatalogTitle,
    ConfigLoadError,
    load_cards,
    load_catalog,
    normalize_uid,
)

log = logging.getLogger(__name__)

DEFAULT_CAPTURE_TIMEOUT_SECS = 30.0
DEFAULT_POLL_SECS = 2.0
DEFAULT_STOP_GRACE_SECS = 5.0
WhileRunningPolicy = Literal["home_only", "swap", "ignore"]
_VALID_WHILE_RUNNING = frozenset({"home_only", "swap", "ignore"})


@dataclass(frozen=True)
class LaunchSettings:
    while_running: WhileRunningPolicy = "home_only"
    capture_timeout_secs: float = DEFAULT_CAPTURE_TIMEOUT_SECS
    stop_grace_secs: float = DEFAULT_STOP_GRACE_SECS
    scummvm_bin: str = "scummvm"
    dosbox_bin: str = "dosbox-staging"
    chromium_bin: str = "chromium-browser"
    ruffle_bin: str = "ruffle"
    allow_shutdown: bool = False
    shutdown_command: tuple[str, ...] = ("systemctl", "poweroff")
    volume_command: tuple[str, ...] = ("amixer", "set", "Master", "toggle")

    def __post_init__(self) -> None:
        if self.while_running not in _VALID_WHILE_RUNNING:
            raise ValueError(f"invalid while_running policy: {self.while_running!r}")


class FileBackedConfig:
    """Load catalog/cards from disk and refresh them when mtimes change."""

    def __init__(self, catalog_path: Path, cards_path: Path) -> None:
        self.catalog_path = catalog_path
        self.cards_path = cards_path
        self.catalog = Catalog(catalog_version=None, titles={})
        self.cards = CardsConfig()
        self._catalog_mtime: int | None = None
        self._cards_mtime: int | None = None

    def load(self, *, force: bool = False) -> bool:
        """Reload the catalog/cards if their mtimes changed or force is set."""
        try:
            catalog_mtime = _stat_mtime(self.catalog_path, allow_missing=False)
            cards_mtime = _stat_mtime(self.cards_path, allow_missing=True)
        except OSError as exc:
            log.error("config stat failed: %s", exc)
            return False

        if (
            not force
            and catalog_mtime == self._catalog_mtime
            and cards_mtime == self._cards_mtime
        ):
            return False

        try:
            catalog = load_catalog(self.catalog_path)
            cards = load_cards(self.cards_path)
        except ConfigLoadError as exc:
            log.error("config load failed, keeping previous: %s", exc)
            return False

        self.catalog = catalog
        self.cards = cards
        self._catalog_mtime = catalog_mtime
        self._cards_mtime = cards_mtime
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
    ) -> None:
        self.config = config
        self.settings = settings or LaunchSettings()
        self._popen_factory = popen_factory
        self._killpg = killpg
        self._getpgid = getpgid
        self._thread_factory = thread_factory

        self._lock = threading.RLock()
        self._current_process: object | None = None
        self._current_title: CatalogTitle | None = None
        self._capture_armed = False
        self._capture_uid: str | None = None
        self._capture_event = threading.Event()
        self.unlocked = False

    def on_scan(self, uid: str) -> None:
        """Handle a completed UID scan from either reader implementation."""
        normalized_uid = normalize_uid(uid)

        with self._lock:
            if self._capture_armed:
                self._capture_uid = normalized_uid
                self._capture_armed = False
                self._capture_event.set()
                log.info("captured uid %s", normalized_uid)
                return

        system_action = self.config.cards.system_action_for_uid(normalized_uid)
        if system_action is not None:
            self._handle_system_action(system_action)
            return

        title_id = self.config.cards.title_id_for_uid(normalized_uid)
        if title_id is None:
            log.info("unknown card %s", normalized_uid)
            return

        title = self.config.catalog.titles.get(title_id)
        if title is None:
            log.warning(
                "card %s points to unknown catalog id %s",
                normalized_uid,
                title_id,
            )
            return

        self._launch_title(title)

    def capture(self, timeout: float | None = None) -> str | None:
        """Arm one-shot capture mode and return the next scanned UID."""
        wait_timeout = (
            self.settings.capture_timeout_secs if timeout is None else timeout
        )
        with self._lock:
            self._capture_uid = None
            self._capture_armed = True
            self._capture_event.clear()

        captured = self._capture_event.wait(wait_timeout)
        with self._lock:
            self._capture_armed = False
            return self._capture_uid if captured else None

    def reload(self, *, force: bool = True) -> bool:
        """Reload catalog/cards from disk."""
        return self.config.load(force=force)

    def stop_current(self) -> None:
        """Stop the current child process group, escalating if needed."""
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
                "unlocked": self.unlocked,
                "cards": len(self.config.cards.title_cards),
                "capture_mode": self._capture_armed,
            }

    def cards_snapshot(self) -> dict[str, dict[str, str]]:
        """Return the current cards mapping for the control API."""
        return self.config.cards_snapshot()

    def _launch_title(self, title: CatalogTitle) -> None:
        policy = self.settings.while_running
        if self.is_running():
            if policy in {"home_only", "ignore"}:
                log.info("ignoring %s (app running, policy=%s)", title.label, policy)
                return
            self.stop_current()

        argv = build_launch_argv(title, self.settings)
        try:
            process = self._popen_factory(argv, start_new_session=True)
        except (OSError, ValueError) as exc:
            log.error("launch failed for %s: %s", title.label, exc)
            return

        with self._lock:
            self._current_process = process
            self._current_title = title
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
            with self._lock:
                self.unlocked = True
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


def build_launch_argv(title: CatalogTitle, settings: LaunchSettings) -> list[str]:
    """Return the argv used to launch a title."""
    if title.type == "scummvm":
        return [settings.scummvm_bin, "-f", title.game_id or ""]
    if title.type == "dosbox":
        return [settings.dosbox_bin, "-conf", title.conf or "", "-fullscreen"]
    if title.type == "exec":
        return list(title.cmd)
    if title.type == "ruffle":
        return [settings.ruffle_bin, "--fullscreen", title.swf or ""]
    return [
        settings.chromium_bin,
        "--kiosk",
        "--noerrdialogs",
        "--no-first-run",
        "--disable-pinch",
        "--disable-features=TranslateUI",
        "--overscroll-history-navigation=0",
        f"--app={title.url or ''}",
    ]


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
