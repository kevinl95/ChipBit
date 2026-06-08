#!/usr/bin/env python3
"""Emit the apt package list for catalog titles with bundled: true.

Usage:
  python3 emit_bundled_apt.py <catalog.yaml>

Prints a space-separated, deduplicated list of apt packages to stdout.
The image install script passes this directly to apt-get install.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml


def emit_bundled_apt(catalog_path: Path) -> list[str]:
    """Return deduplicated apt packages for all bundled catalog titles."""
    with catalog_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    titles = data.get("titles") or []
    seen: dict[str, None] = {}  # ordered set via dict keys

    for title in titles:
        if not isinstance(title, dict):
            continue
        if not title.get("bundled"):
            continue
        install = title.get("install")
        if not isinstance(install, dict):
            continue
        for pkg in install.get("apt") or []:
            if isinstance(pkg, str) and pkg.strip():
                seen[pkg.strip()] = None

    return list(seen)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(f"Usage: {sys.argv[0]} <catalog.yaml>", file=sys.stderr)
        return 1
    catalog_path = Path(args[0])
    if not catalog_path.exists():
        print(f"error: catalog not found: {catalog_path}", file=sys.stderr)
        return 1
    packages = emit_bundled_apt(catalog_path)
    print(" ".join(packages))
    return 0


if __name__ == "__main__":
    sys.exit(main())
