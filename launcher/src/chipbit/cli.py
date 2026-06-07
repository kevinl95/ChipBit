"""CLI entry points for the ChipBit launcher and web service."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .control_api import create_control_server
from .launcher import (
    DEFAULT_POLL_SECS,
    DEFAULT_STOP_GRACE_SECS,
    FileBackedConfig,
    LauncherService,
    LaunchSettings,
    poll_config,
)
from .reader import EvdevReader, MockReader, pump_reader

log = logging.getLogger(__name__)


def launcher_main(argv: Sequence[str] | None = None) -> int:
    """Run the ChipBit launcher daemon."""
    parser = argparse.ArgumentParser(prog="chipbit-launcher")
    parser.add_argument("--catalog", type=Path, default=Path("catalog/catalog.yaml"))
    parser.add_argument("--cards", type=Path, default=Path("cards.yaml"))
    parser.add_argument("--reader-device")
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
    parser.add_argument("--scummvm-bin", default="scummvm")
    parser.add_argument("--dosbox-bin", default="dosbox-staging")
    parser.add_argument("--chromium-bin", default="chromium-browser")
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
        parser.error("--reader-device is required unless --mock-reader is used")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = FileBackedConfig(args.catalog, args.cards)
    if not config.load(force=True):
        log.error("could not load initial config; exiting")
        return 1

    service = LauncherService(
        config,
        settings=LaunchSettings(
            while_running=args.while_running,
            stop_grace_secs=args.stop_grace_secs,
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

    reader = EvdevReader(args.reader_device)
    reader_thread = threading.Thread(
        target=pump_reader,
        args=(reader, service.on_scan, stop),
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


def web_main(argv: Sequence[str] | None = None) -> int:
    """Run the placeholder web entry point for milestone M0."""
    parser = argparse.ArgumentParser(prog="chipbit-web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.parse_args(list(argv) if argv is not None else None)
    print("ChipBit web scaffold installed. Service implementation lands in M4.")
    return 0


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
