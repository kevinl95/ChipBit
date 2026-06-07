"""CLI entry points for the ChipBit scaffold."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from . import __version__


def launcher_main(argv: Sequence[str] | None = None) -> int:
    """Run the placeholder launcher entry point for milestone M0."""
    parser = argparse.ArgumentParser(prog="chipbit-launcher")
    parser.add_argument("--catalog", type=Path, default=Path("catalog/catalog.yaml"))
    parser.add_argument("--cards", type=Path, default=Path("cards.yaml"))
    parser.add_argument("--mock-reader", action="store_true")
    parser.add_argument("--control-port", type=int, default=8765)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.parse_args(list(argv) if argv is not None else None)
    print("ChipBit launcher scaffold installed. Runtime implementation lands in M2.")
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
