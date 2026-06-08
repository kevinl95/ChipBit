# ChipBit Image Module (M6)

This directory is a **CustomPiOS module** that turns a stock Raspberry Pi OS
Bookworm (arm64) image into a ChipBit kiosk appliance.

## Layout

```
image/
  README.md                                    # this file
  build_helpers/
    emit_bundled_apt.py                        # catalog → apt package list (also tested in CI)
  chipbit/                                     # CustomPiOS module (MODULES=chipbit)
    config.sh                                  # build-time overrideable variables
    start_chipbit.sh                           # chroot install script
    filesystem/                               # overlaid onto the target rootfs
      etc/
        systemd/system/
          chipbit-launcher.service
          chipbit-web.service
          getty@tty1.service.d/
            autologin.conf                     # autologin → chipbit user on tty1
      home/chipbit/
        .bash_profile                          # cage → chromium --kiosk on tty1
      usr/share/chipbit/
        catalog.yaml                           # frozen catalog baked into the image
        emit_bundled_apt.py                    # available post-install for admin use
```

---

## Local development (no Pi required)

### 1 — Install

```bash
pip install -e './launcher[dev]'
```

Or with Make:

```bash
make test       # installs, runs ruff + pytest
```

### 2 — Run the full stack in mock mode

Open **two terminals** from the repo root.

**Terminal A — launcher daemon** (reads UIDs from stdin, one per line):

```bash
chipbit-launcher \
  --mock-reader \
  --catalog catalog/catalog.yaml \
  --cards /tmp/chipbit-cards.yaml
```

**Terminal B — web service** (parent console + kiosk shell):

```bash
chipbit-web \
  --catalog catalog/catalog.yaml \
  --cards /tmp/chipbit-cards.yaml
```

Then open **http://127.0.0.1:8080** in a browser.

### 3 — Walk through the first-run flow

Because `/tmp/chipbit-cards.yaml` doesn't exist yet, the device is in
first-run mode. The web UI shows only one option: "tap a card to make it the
admin card."

In Terminal A, simulate a card tap by typing a UID and pressing Enter:

```
AABBCCDD
```

The launcher logs `auto-enrolled admin card AABBCCDD` and the web UI unlocks.
You can now browse the catalog and enroll more cards:

```
11223344    ← tap another card; the UI prompts you to assign it a title
```

### 4 — Simulate the kiosk shell

The kiosk page (what cage + chromium would render on the Pi) is at:

```
http://127.0.0.1:8080/kiosk
```

It updates in real time via SSE as you type UIDs in the launcher terminal.

---

## Building the image

### Prerequisites

- [CustomPiOS](https://github.com/guysoft/CustomPiOS) checked out locally
- A Raspberry Pi OS **Bookworm lite arm64** base image (`.img.xz`) — Lite is
  intentional: `cage` is a single-application Wayland compositor that talks
  directly to the GPU's DRM/KMS layer and needs no desktop environment
- Docker **or** a Linux host with QEMU user-mode binfmt registered

### Build

```bash
# From the CustomPiOS checkout directory:
sudo \
  MODULES="chipbit" \
  MODULESPATH="/path/to/chipbit-repo/image" \
  CHIPBIT_VERSION="0.1.0" \
  RUFFLE_TAG="2024-10-13" \
  ./src/build_dist_image.sh /path/to/base-image.img.xz
```

CustomPiOS overlays `image/chipbit/filesystem/` onto the rootfs, then runs
`image/chipbit/start_chipbit.sh` inside the chroot to install engines,
bundled titles, and ChipBit itself.

The resulting `.img` can be written to an SD card with `dd` or Raspberry Pi
Imager.

### Overrideable variables

| Variable | Default | Meaning |
|---|---|---|
| `CHIPBIT_VERSION` | `0.1.0` | PyPI version of `chipbit` to install |
| `RUFFLE_TAG` | `2024-10-13` | Ruffle GitHub release tag |
| `CHIPBIT_UID` / `CHIPBIT_GID` | `900` | UID/GID of the kiosk system user |
| `CHIPBIT_WEB_PORT` | `8080` | Web service port |
| `CHIPBIT_CONTROL_PORT` | `8765` | Launcher control API port |

---

## What `start_chipbit.sh` does

1. Installs `cage`, `scummvm`, `dosbox-staging`, `chromium-browser`, and Ruffle.
2. Runs `emit_bundled_apt.py catalog.yaml` to get the apt list for every title
   with `bundled: true` — the catalog is the single source of truth.
3. Creates the `chipbit` system user (UID/GID 900) in `input`, `audio`, and
   `video` groups.
4. Runs `pip install chipbit==<version>`.
5. Enables `chipbit-launcher.service` and `chipbit-web.service`.

## RFID reader auto-detection

No vendor/product IDs are baked into the image. At startup, `chipbit-launcher`
scans every evdev device and selects the first one whose key-capability set
matches the shape of an HID keyboard-mode RFID reader: digit keys + Enter, no
mouse/joystick axes, no full-keyboard markers (ESC, Tab, F-keys, etc.). The
`chipbit` user's `input` group membership gives access to all `/dev/input/event*`
nodes.

Use `--reader-device /dev/input/eventN` to override auto-detection.

## Keeping the catalog in sync

`chipbit/filesystem/usr/share/chipbit/catalog.yaml` is a copy of
`catalog/catalog.yaml` frozen at image-build time. Re-copy before building:

```bash
cp catalog/catalog.yaml image/chipbit/filesystem/usr/share/chipbit/catalog.yaml
```
