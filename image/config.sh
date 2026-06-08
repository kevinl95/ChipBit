#!/usr/bin/env bash
# ChipBit image module — build-time configuration.
# Override any variable in the CustomPiOS config file before building.

# Pinned ChipBit release installed from PyPI.
CHIPBIT_VERSION="${CHIPBIT_VERSION:-0.1.0}"

# Ruffle release tag (date-based, e.g. "2024-10-13").
# Release binaries: https://github.com/ruffle-rs/ruffle/releases
RUFFLE_TAG="${RUFFLE_TAG:-2024-10-13}"

# UID/GID for the kiosk system user created in the image.
CHIPBIT_UID="${CHIPBIT_UID:-900}"
CHIPBIT_GID="${CHIPBIT_GID:-900}"

# TCP port the web service binds to (must match chipbit-web.service).
CHIPBIT_WEB_PORT="${CHIPBIT_WEB_PORT:-8080}"

# TCP port the launcher control API binds to (must match chipbit-launcher.service).
CHIPBIT_CONTROL_PORT="${CHIPBIT_CONTROL_PORT:-8765}"
