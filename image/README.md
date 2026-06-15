# ChipBit Image Module (M6)

This directory is a **CustomPiOS module** that turns a stock Raspberry Pi OS
Bookworm (arm64) image into a ChipBit kiosk appliance.

## Layout

```
image/
  README.md
  build_dist                                   # entry point: sets DIST_PATH, calls CustomPiOS
  config                                       # distro config (MODULES=chipbit, BASE_ARCH, …)
  custompios_path                              # ← not committed; path to your CustomPiOS src/
  config.local                                 # ← not committed; your BASE_ZIP_IMG override
  build_helpers/
    emit_bundled_apt.py                        # catalog → apt list (standalone, tested in CI)
  modules/
    chipbit/                                   # CustomPiOS module — found at ${DIST_PATH}/modules/chipbit/
      config                                   # module-level variables (CHIPBIT_VERSION, …)
      start_chroot_script                      # chroot install script (required name)
      filesystem/                              # overlaid onto the rootfs before the script runs
        etc/
          systemd/system/
            chipbit-launcher.service
            chipbit-web.service
            getty@tty1.service.d/
              autologin.conf                   # autologin → chipbit user on tty1
        home/chipbit/
          .bash_profile                        # cage → chromium --kiosk on tty1
        usr/share/chipbit/
          catalog.yaml                         # frozen catalog baked into the image
          emit_bundled_apt.py                  # available post-install for admin use
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
- A Raspberry Pi OS **Bookworm lite** base image (`.img.xz`) — use **arm64**
  for Pi 4/5/500, **armhf** for Pi 400 or older hardware. Lite is intentional:
  `cage` is a single-application Wayland compositor that talks directly to the
  GPU's DRM/KMS layer and needs no desktop environment
- A Linux host with `qemu-user-static` and binfmt support installed, or Docker

### Build

**One-time setup** (from the repo root):

```bash
# 1. Tell the build where your CustomPiOS checkout lives.
echo "/path/to/CustomPiOS/src" > image/custompios_path

# 2. Tell it where your downloaded Pi OS base image is.
#    armhf for Pi 3/400/older, arm64 for Pi 4/5/500.
cat > image/config.local << 'EOF'
export BASE_ZIP_IMG="/path/to/2026-04-13-raspios-bookworm-armhf-lite.img.xz"
# export BASE_ARCH=arm64   # uncomment for arm64 image
EOF
```

**Build:**

```bash
sudo bash image/build_dist
```

`build_dist` sets `DIST_PATH` (which is what CustomPiOS actually uses) and
hands off to `build_custom_os`. CustomPiOS copies `modules/chipbit/filesystem/`
into the chroot as `/filesystem/`; `start_chroot_script` then merges that into
the root at startup with `cp -a /filesystem/. /`. The Ruffle binary is selected
automatically for the chroot architecture (aarch64 or x86_64; skipped on armhf).

The resulting image is written to `image/workspace/<base-image-name>.img`.
Flash it to an SD card:

```bash
sudo dd if=image/workspace/2026-04-13-raspios-bookworm-armhf-lite.img \
     of=/dev/sdX bs=4M status=progress conv=fsync
```

Or use Raspberry Pi Imager → "Use custom image" and point it at that file.

### Overrideable variables

| Variable | Default | Meaning |
|---|---|---|
| `CHIPBIT_VERSION` | `0.1.0` | PyPI version of `chipbit` to install |
| `RUFFLE_TAG` | `2024-10-13` | Ruffle GitHub release tag |
| `CHIPBIT_UID` / `CHIPBIT_GID` | `900` | UID/GID of the kiosk system user |
| `CHIPBIT_WEB_PORT` | `8080` | Web service port |
| `CHIPBIT_CONTROL_PORT` | `8765` | Launcher control API port |

---

## What `start_chroot_script` does

1. Merges the `/filesystem/` overlay into `/` (CustomPiOS stages it there, not at root).
2. Installs `cage`, `scummvm`, `dosbox-staging`, `chromium-browser`, and Ruffle (aarch64/x86_64 only).
3. Runs `emit_bundled_apt.py catalog.yaml` to get the apt list for every title
   with `bundled: true` — the catalog is the single source of truth.
4. Creates the `chipbit` system user (UID/GID 900) in `input`, `audio`, and `video` groups.
5. Runs `pip install /tmp/chipbit-*.whl` (wheel built from source by `build_dist` — not on PyPI yet).
6. Enables `chipbit-launcher.service` and `chipbit-web.service`.

## RFID reader auto-detection

No vendor/product IDs are baked into the image. At startup, `chipbit-launcher`
scans every evdev device and selects the first one whose key-capability set
matches the shape of an HID keyboard-mode RFID reader: digit keys + Enter, no
mouse/joystick axes, no full-keyboard markers (ESC, Tab, F-keys, etc.). The
`chipbit` user's `input` group membership gives access to all `/dev/input/event*`
nodes.

Use `--reader-device /dev/input/eventN` to override auto-detection.

## Keeping the catalog in sync

`modules/chipbit/filesystem/usr/share/chipbit/catalog.yaml` is a copy of
`catalog/catalog.yaml` frozen at image-build time. Re-copy before building:

```bash
cp catalog/catalog.yaml image/modules/chipbit/filesystem/usr/share/chipbit/catalog.yaml
```
