#!/usr/bin/env bash
# ChipBit CustomPiOS module — runs inside the image chroot.
# Installs engines, creates the kiosk user, installs ChipBit, enables services.
set -euo pipefail

# Load module config (sourced by CustomPiOS before calling this script).
CHIPBIT_VERSION="${CHIPBIT_VERSION:-0.1.0}"
RUFFLE_TAG="${RUFFLE_TAG:-2024-10-13}"
CHIPBIT_UID="${CHIPBIT_UID:-900}"
CHIPBIT_GID="${CHIPBIT_GID:-900}"
CHIPBIT_WEB_PORT="${CHIPBIT_WEB_PORT:-8080}"
CHIPBIT_CONTROL_PORT="${CHIPBIT_CONTROL_PORT:-8765}"

# ---------------------------------------------------------------------------
# 1. Base engines and compositor
# ---------------------------------------------------------------------------
apt-get update -y
apt-get install -y --no-install-recommends \
    cage \
    scummvm \
    dosbox-staging \
    chromium-browser \
    python3-pip \
    python3-yaml \
    curl \
    ca-certificates

# Ruffle (Rust Flash player — no apt package; download arm64 binary from GitHub).
RUFFLE_ARCHIVE="ruffle-${RUFFLE_TAG}-linux-aarch64.tar.gz"
RUFFLE_URL="https://github.com/ruffle-rs/ruffle/releases/download/${RUFFLE_TAG}/${RUFFLE_ARCHIVE}"
echo "Downloading Ruffle ${RUFFLE_TAG}..."
curl -fsSL "${RUFFLE_URL}" | tar -xz -C /usr/local/bin ruffle
chmod 0755 /usr/local/bin/ruffle

# ---------------------------------------------------------------------------
# 2. Bundled native titles (from catalog — single source of truth)
# ---------------------------------------------------------------------------
CATALOG=/usr/share/chipbit/catalog.yaml
HELPER=/usr/share/chipbit/emit_bundled_apt.py
BUNDLED_PKGS="$(python3 "${HELPER}" "${CATALOG}")"
if [ -n "${BUNDLED_PKGS}" ]; then
    # shellcheck disable=SC2086
    apt-get install -y --no-install-recommends ${BUNDLED_PKGS}
fi

# ---------------------------------------------------------------------------
# 3. Kiosk system user
# ---------------------------------------------------------------------------
if ! getent group chipbit > /dev/null; then
    groupadd --gid "${CHIPBIT_GID}" chipbit
fi

if ! id chipbit &> /dev/null; then
    useradd \
        --uid "${CHIPBIT_UID}" \
        --gid "${CHIPBIT_GID}" \
        --create-home \
        --shell /bin/bash \
        --comment "ChipBit kiosk user" \
        chipbit
fi

# Grant groups needed for evdev, audio, and GPU access.
for grp in input audio video tty; do
    if getent group "${grp}" > /dev/null; then
        usermod -aG "${grp}" chipbit
    fi
done

# ---------------------------------------------------------------------------
# 4. Runtime directories
# ---------------------------------------------------------------------------
mkdir -p /var/lib/chipbit
chown chipbit:chipbit /var/lib/chipbit
chmod 0755 /var/lib/chipbit

mkdir -p /games
chown chipbit:chipbit /games
chmod 0755 /games

# ---------------------------------------------------------------------------
# 5. Install ChipBit from PyPI
# ---------------------------------------------------------------------------
pip3 install --break-system-packages "chipbit==${CHIPBIT_VERSION}"

# ---------------------------------------------------------------------------
# 6. Drop the build helper next to the catalog so the admin can re-run it.
# ---------------------------------------------------------------------------
install -m 0755 "${HELPER}" /usr/share/chipbit/emit_bundled_apt.py

# ---------------------------------------------------------------------------
# 7. Enable systemd services
# ---------------------------------------------------------------------------
systemctl enable chipbit-launcher.service
systemctl enable chipbit-web.service

# ---------------------------------------------------------------------------
# 8. Clean up
# ---------------------------------------------------------------------------
apt-get clean
rm -rf /var/lib/apt/lists/*
