"""CLI entry points for the ChipBit launcher and web service."""

from __future__ import annotations

import argparse
import logging
import os
import pwd
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from . import __version__
from .control_api import create_control_server
from .launcher import (
    DEFAULT_POLL_SECS,
    DEFAULT_STOP_GRACE_SECS,
    DEFAULT_UNLOCK_TIMEOUT_SECS,
    FileBackedConfig,
    LauncherService,
    LaunchSettings,
    poll_config,
)
from .models import ConfigLoadError, load_cards, load_catalog_merged
from .reader import EvdevReader, MockReader, find_rfid_reader, pump_reader
from .strings import load_locale, read_language
from .web import create_web_server

log = logging.getLogger(__name__)


def launcher_main(argv: Sequence[str] | None = None) -> int:
    """Run the ChipBit launcher daemon."""
    # Systemd system services don't set HOME even with User=. Without it,
    # launched apps (TuxPaint etc.) can't locate their home directories.
    if "HOME" not in os.environ:
        os.environ["HOME"] = pwd.getpwuid(os.getuid()).pw_dir

    parser = argparse.ArgumentParser(prog="chipbit-launcher")
    parser.add_argument("--catalog", type=Path, default=Path("catalog/catalog.yaml"))
    parser.add_argument("--cards", type=Path, default=Path("cards.yaml"))
    parser.add_argument("--user-catalog", type=Path, default=None)
    parser.add_argument("--reader-device")
    parser.add_argument(
        "--language-file",
        type=Path,
        default=None,
        help="File holding the parent's language choice (for launched titles).",
    )
    parser.add_argument(
        "--mock-reader",
        nargs="?",
        const="-",
        default=None,
        metavar="PATH",
    )
    parser.add_argument("--control-host", default="127.0.0.1")
    parser.add_argument("--control-port", type=int, default=8765)
    parser.add_argument(
        "--while-running",
        choices=["home_only", "swap", "ignore"],
        default="home_only",
    )
    parser.add_argument("--poll-secs", type=float, default=DEFAULT_POLL_SECS)
    parser.add_argument(
        "--stop-grace-secs",
        type=float,
        default=DEFAULT_STOP_GRACE_SECS,
    )
    parser.add_argument(
        "--unlock-timeout-secs",
        type=float,
        default=DEFAULT_UNLOCK_TIMEOUT_SECS,
    )
    parser.add_argument("--scummvm-bin", default="scummvm")
    parser.add_argument("--dosbox-bin", default="dosbox-staging")
    parser.add_argument("--chromium-bin", default="chromium")
    parser.add_argument("--ruffle-bin", default="ruffle")
    parser.add_argument("--allow-shutdown", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.mock_reader is None and not args.reader_device:
        args.reader_device = find_rfid_reader()
        if not args.reader_device:
            # Don't exit — keep the control API running so the web service stays
            # connected. A reader thread will retry discovery every few seconds.
            log.warning(
                "no RFID reader detected at startup; will retry until one appears"
            )

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = FileBackedConfig(args.catalog, args.cards, args.user_catalog)
    if not config.load(force=True):
        log.error("could not load initial config; exiting")
        return 1

    service = LauncherService(
        config,
        language_path=args.language_file,
        settings=LaunchSettings(
            while_running=args.while_running,
            stop_grace_secs=args.stop_grace_secs,
            unlock_timeout_secs=args.unlock_timeout_secs,
            scummvm_bin=args.scummvm_bin,
            dosbox_bin=args.dosbox_bin,
            chromium_bin=args.chromium_bin,
            ruffle_bin=args.ruffle_bin,
            allow_shutdown=args.allow_shutdown,
        ),
    )
    stop = threading.Event()
    httpd = create_control_server(args.control_host, args.control_port, service)

    poll_thread = threading.Thread(
        target=poll_config,
        args=(config, stop, args.poll_secs),
        daemon=True,
    )
    poll_thread.start()
    control_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    control_thread.start()
    log.info("control API on %s:%d", args.control_host, httpd.server_port)

    if args.mock_reader is not None:
        return _run_mock_mode(args.mock_reader, service, stop, httpd)

    reader_thread = threading.Thread(
        target=_find_and_pump_reader,
        args=(args.reader_device, service.on_scan, stop),
        daemon=True,
    )
    reader_thread.start()

    def shutdown(signum: int, frame: object | None) -> None:
        log.info("signal %d -> shutting down", signum)
        _shutdown(stop, service, httpd)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        shutdown(signal.SIGINT, None)
    finally:
        _shutdown(stop, service, httpd)
    return 0


def web_main(
    argv: Sequence[str] | None = None,
    *,
    stop_event: threading.Event | None = None,
) -> int:
    """Run the ChipBit parent console and kiosk shell service."""
    parser = argparse.ArgumentParser(prog="chipbit-web")
    parser.add_argument("--catalog", type=Path, default=Path("catalog/catalog.yaml"))
    parser.add_argument("--cards", type=Path, default=Path("cards.yaml"))
    parser.add_argument("--user-catalog", type=Path, default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--control-url", default="http://127.0.0.1:8765")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--locale",
        default=None,
        metavar="CODE",
        help="UI language, e.g. de. Omit for English.",
    )
    parser.add_argument(
        "--language-file",
        type=Path,
        default=None,
        help="File holding the parent's language choice.",
    )
    parser.add_argument(
        "--locales-dir",
        type=Path,
        action="append",
        default=None,
        metavar="DIR",
        help=(
            "Directory of <code>.yaml locale files; repeatable, later wins. "
            "Defaults to the system and user locale directories. Point this "
            "at a checkout to preview a translation before installing it."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    locale_dirs = tuple(args.locales_dir) if args.locales_dir else None
    # --locale is the translator's override; otherwise honour whatever the
    # parent picked in the console, which the launcher reads from the same file.
    language = args.locale or read_language(args.language_file)
    if language != "en":
        report = load_locale(language, locale_dirs)
        # Logged at INFO so a translator sees their coverage on every start
        # without having to go looking for it.
        log.info("locale %s", report.summary())

    try:
        load_catalog_merged(args.catalog, args.user_catalog)
        load_cards(args.cards)
    except ConfigLoadError as exc:
        log.error("could not load initial web config: %s", exc)
        return 1

    httpd = create_web_server(
        args.host,
        args.port,
        catalog_path=args.catalog,
        cards_path=args.cards,
        control_base_url=args.control_url,
        user_catalog_path=args.user_catalog,
        language_path=args.language_file,
        locale_dirs=locale_dirs,
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    log.info(
        "web service on %s:%d -> %s",
        args.host,
        httpd.server_port,
        args.control_url,
    )

    stop = stop_event or threading.Event()

    def shutdown(signum: int, frame: object | None) -> None:
        log.info("signal %d -> shutting down web service", signum)
        stop.set()

    if stop_event is None:
        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        stop.set()
    finally:
        httpd.shutdown()
        httpd.server_close()

    return 0


def _find_and_pump_reader(
    initial_path: str | None,
    on_scan: Callable[[str], None],
    stop: threading.Event,
) -> None:
    """Discover an RFID reader (retrying if absent) then pump events from it."""
    device_path = initial_path
    while not stop.is_set():
        if not device_path:
            device_path = find_rfid_reader()
            if not device_path:
                log.warning("no RFID reader found; retrying in 5s")
                stop.wait(5.0)
                continue
            log.info("RFID reader found: %s", device_path)
        reader = EvdevReader(device_path)
        pump_reader(reader, on_scan, stop)
        # pump_reader only returns when stop fires — break to exit cleanly
        break


def _run_mock_mode(
    source_spec: str,
    service: LauncherService,
    stop: threading.Event,
    httpd: object,
) -> int:
    source, should_close = _open_mock_source(source_spec)
    try:
        pump_reader(MockReader(source), service.on_scan, stop)
    except KeyboardInterrupt:
        pass
    finally:
        if should_close:
            source.close()
        _shutdown(stop, service, httpd)
    return 0


def _open_mock_source(source_spec: str):
    if source_spec == "-":
        return sys.stdin, False
    handle = Path(source_spec).open("r", encoding="utf-8")
    return handle, True


def _shutdown(stop: threading.Event, service: LauncherService, httpd: object) -> None:
    stop.set()
    service.stop_current()
    httpd.shutdown()
    httpd.server_close()
