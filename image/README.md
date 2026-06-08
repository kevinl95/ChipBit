# ChipBit Image Module (M6)

This directory is a **CustomPiOS module** that turns a stock Raspberry Pi OS
Bookworm (arm64) image into a ChipBit kiosk appliance.

## Module layout

```
image/
  config.sh                        # Build-time overrideable variables
  start_chipbit.sh                 # Chroot install script (run by CustomPiOS)
  build_helpers/
    emit_bundled_apt.py            # Parses catalog.yaml → apt list for bundled titles
  filesystem/                      # Overlaid onto the target rootfs
    etc/
      systemd/system/
        chipbit-launcher.service   # Launcher daemon (evdev reader + process mgmt)
        chipbit-web.service        # Parent console + kiosk shell web service
        getty@tty1.service.d/
          autologin.conf           # Autologin to the chipbit kiosk user on tty1
      udev/rules.d/
        99-chipbit-rfid.rules      # Stable /dev/input/chipbit-rfid symlink
    home/chipbit/
      .bash_profile                # Starts cage → chromium kiosk on tty1
    usr/share/chipbit/
      catalog.yaml                 # Frozen catalog baked into the image
      emit_bundled_apt.py          # Helper available post-install for admin use
```

## What the module does

1. **Installs base engines** — `cage`, `scummvm`, `dosbox-staging`,
   `chromium-browser`, and Ruffle (arm64 binary from GitHub releases).
2. **Installs bundled native titles** — `emit_bundled_apt.py` parses
   `catalog.yaml` and produces the apt package list for every title with
   `bundled: true`, keeping the catalog as the single source of truth.
3. **Creates the `chipbit` system user** (UID/GID 900) with `input`, `audio`,
   and `video` group membership so the launcher can open the RFID reader and
   launched apps can use sound and graphics.
4. **Installs ChipBit from PyPI** at the pinned version in `config.sh`.
5. **Enables systemd services** — `chipbit-launcher` and `chipbit-web` start
   at boot as the `chipbit` user.
6. **Wires up the RFID reader** — udev rules create
   `/dev/input/chipbit-rfid → /dev/input/eventN` for every supported HID
   keyboard-mode reader; the launcher service points to this stable path so
   production reader selection never lives in Python code.
7. **Boots into the kiosk** — getty autologin on tty1 → `.bash_profile` →
   `cage -- chromium-browser --kiosk http://127.0.0.1:8080/kiosk`.

## Building

```bash
# From the repo root — substitute your CustomPiOS checkout path.
export MODULES="chipbit"
export MODULESPATH="$(pwd)/image"
export CHIPBIT_VERSION="0.1.0"   # overrides config.sh default
./BuildImage.sh
```

## Adding a new RFID reader

1. Plug the reader in and run `lsusb` to find the vendor:product ID.
2. Add a rule to
   `filesystem/etc/udev/rules.d/99-chipbit-rfid.rules` following the existing
   pattern.
3. Rebuild the image (or `scp` the updated rules file and run
   `sudo udevadm control --reload-rules` on a running Pi).

## Keeping the catalog in sync

`filesystem/usr/share/chipbit/catalog.yaml` is a copy of `catalog/catalog.yaml`
frozen at image-build time.  When you update the catalog, re-copy it before
building:

```bash
cp catalog/catalog.yaml image/filesystem/usr/share/chipbit/catalog.yaml
```
