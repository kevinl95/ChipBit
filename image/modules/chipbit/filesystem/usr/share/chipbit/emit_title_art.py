#!/usr/bin/env python3
"""Emit catalog title IDs and their first apt package for art extraction.

Usage:
  python3 emit_title_art.py <catalog.yaml>

Prints one line per catalog title that has an apt install spec:
  <title-id> <first-apt-package>

The chroot script pipes this into a loop that extracts artwork for each title,
trying on-disk icon paths first and falling back to downloading the .deb.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml


def emit_title_art(catalog_path: Path) -> list[tuple[str, str]]:
    """Return (title_id, first_apt_package) for every title with an apt spec."""
    with catalog_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    results: list[tuple[str, str]] = []
    for title in data.get("titles") or []:
        if not isinstance(title, dict):
            continue
        title_id = title.get("id", "").strip()
        apt_pkgs = (title.get("install") or {}).get("apt") or []
        if title_id and apt_pkgs and isinstance(apt_pkgs[0], str):
            results.append((title_id, apt_pkgs[0].strip()))
    return results


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(f"Usage: {sys.argv[0]} <catalog.yaml>", file=sys.stderr)
        return 1
    catalog_path = Path(args[0])
    if not catalog_path.exists():
        print(f"error: catalog not found: {catalog_path}", file=sys.stderr)
        return 1
    for title_id, pkg in emit_title_art(catalog_path):
        print(f"{title_id} {pkg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
