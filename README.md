# ChipBit

ChipBit is the family computer reinvented. Imagine returning to their being a shared machine in your home that plays games and runs software the whole family can enjoy. It teaches valuable computer skills, like how to use a keyboard and navigate software. Parental controls are simple, and it's easy to add new software and restrict screentime.

Best of all it is intuitive even for small children because all software is launched by simply tapping a card!

Chipbit is Raspberry Pi (400 or newer) that boots straight into a kiosk and launches software when you tap an RFID card. Each card is bound to one title. An card stops whatever is running and returns to the configuration screen meant for parents. Everything is sandboxed- you can give users access to websites without worrying about them navigating places they shouldn't be, launch games with as much ease as a game console, and make works of art they can save safely to local storage.

It runs ScummVM, Ruffle (Flash emulator), native Linux apps, and a locked-down browser, so most of your favorite edutainment catalog works if you supply the data.

## Hardware

| Part | Notes |
|---|---|
| Raspberry Pi 400 or 500 | Recommended. Everything is inside the keyboard, making setup simple. A Pi 4 or 5 works fine if you already have one. |
| USB HID RFID reader, 13.56 MHz MIFARE | Enumerates as a keyboard. No driver, no config. I used a Fissaid reader. |
| MIFARE Classic 1K cards or NFC stickers | Any 13.56 MHz card with a factory UID. Nothing gets written to the card, so blank cards are fine and you can reuse hotel keys and transit cards. |
| A monitor, and a mouse | Keyboard also recommended if not using a Pi 400 or 500 |

The reader has to type the UID and then press Enter. ChipBit keeps the characters `0-9` and `A-F` and silently drops everything else. Most RFID readers ship in a mode that types the UID as a decimal number rather than hex. On the Fissaid the fix is mode 31, which you set by following a configuration procedure in its manual.

## Install

1. Download the latest image from [Releases](https://github.com/kevinl95/ChipBit/releases).
2. Flash it with Raspberry Pi Imager or `dd`. Do not use the Imager's OS customization options, since the first-boot wizard handles Wi-Fi.
3. Boot the Pi with a keyboard, mouse and monitor attached. The first boot expands the filesystem to fill the card before the kiosk appears.
4. Pick a language. The screen shows the word "Language" in each one that is installed, and the choices are in their own languages, so nobody has to read English to get past it. English, German, French and Spanish ship today.
5. Choose your Wi-Fi country. This sets which radio channels are legal where you are, and the Pi reboots once to apply it.
6. Join Wi-Fi, or skip it. You only need a network for titles that download on first use.
7. Hold a card to the reader. That card becomes the admin card, which is the key to the parent screens. Keep it somewhere the kids cannot reach.


## Adding game data

Game data lives under `/games/` on the Pi, in a folder per engine:

```
/games/
  scummvm/monkey/          MONKEY.000, MONKEY.001, ...
  flash/mathblaster.swf
```

The fastest way to get files there is an external CD drive or USB stick. Plug it in, tap the admin card, and open **Game files** from the parent console. Drives under `/media/` are mounted for you, and unmounted ones get a Mount button. Browse into the folder that holds the game, pick the engine, and press Copy folder.

Copying does a bit of work for you. For ScummVM it runs `scummvm --detect` against the copied folder and prefills the game ID it finds, which is the field people most often get wrong: the ScummVM ID (`monkey`) is not the same thing as the ChipBit title ID (the folder name), and they are never interchangeable. For Flash it takes the `.swf` path as-is. When the copy finishes it drops you into the "Add your own" form with the paths already filled in, so you name the card and save.

Ruffle plays a single `.swf` per card. Anything that loads assets over the network at runtime will not work, since the browser is locked to an allowlist.

## Enrolling a card

1. Tap the admin card. The kiosk switches to the parent console.
2. Pick a title and press **Tap a card to bind**.
3. Hold a blank card to the reader within 30 seconds.
4. If the title installs on first use, it downloads and installs now. The screen shows what it is doing and how long it has been going.

Only one enrollment can run at a time. Starting a second while one is still installing is refused with a message rather than queued, because the reader has one slot and both enrollments would otherwise capture the same card.

Tapping an already-bound card while enrolling reassigns it. You can also reassign or disable a card from the table further down the console.

<!-- TODO: card art templates, if you want them in the repo. Art shown in the UI
     is extracted from each package's own icon at build time, not authored here. -->

## Backing up your child's work

**Your child's work** in the parent console shows everything the activities have saved, as pictures rather than filenames. Plug in a USB stick, press Copy, and it writes into a dated folder:

```
ChipBit/2026-08-30/tuxpaint/saved/2026-08-14-153022.png
```

It copies everything in those folders, not just the pictures it can preview. It runs `sync` and unmounts the drive before telling you it is safe to pull out, because that is exactly when people pull it out.

Which folders get backed up comes from `user_dirs` in `catalog.yaml`. A title that pins its save location has to declare that same location, or its work is invisible to the backup. Tux Paint saves to `/var/lib/chipbit/tuxpaint` through `--savedir`, and LibreOffice saves to `~/Documents` through an XDG pin in the image. Both are declared, and there are tests that fail if they ever drift apart.

## catalog.yaml

The catalog lives at `catalog/catalog.yaml` in the repo and ships to the Pi at `/usr/share/chipbit/catalog.yaml`. Cards you create in the UI are written to `/var/lib/chipbit/user-catalog.yaml`, which is merged over the top, so an update never clobbers what you added.

An entry looks like this:

```yaml
  - id: supertux
    label: "SuperTux"
    type: exec
    bundled: false
    install: { apt: [supertux] }
    cmd: ["supertux2"]
    subject: action
    min_age: 5
    blurb: "Classic side-scrolling platformer, free and open source."
    art: /art/supertux.png
```

`type` is one of `scummvm`, `exec`, `web` or `ruffle`, and it decides which other fields matter: `game_id` plus `data_dir` for ScummVM, `cmd` for a native app, `url` plus `allowlist` for a web card, `swf` for Ruffle. `bundled: true` bakes the packages into the image; `bundled: false` installs them the first time a card is enrolled. `install` is declarative and only understands `apt`, `flatpak` and `pip`, never a shell string, so a card pack from a stranger cannot run arbitrary commands. `data: required` blocks enrollment until the parent has actually supplied the game files.

The full field contract is commented at the top of `catalog.yaml`, and that comment is the authority if this section drifts.

## Translating

Every string in the UI is in `launcher/src/chipbit/strings.py`, and a translation is a file of the same keys in `locales/`. Anything you leave out falls back to English, so a partial file is a useful contribution.

The twelve `kiosk.*` keys are everything a child ever sees. Translate only those and a child who reads no English gets a machine that speaks to them, even with the parent console still in English.

To see your work without building an image:

```bash
chipbit-web --locale de --locales-dir ./locales
```

It prints how many strings you have translated on startup. You can also drop a file into `/var/lib/chipbit/locales/` on a running Pi and restart `chipbit-web`; that copy wins over the one in the image.

Picking a language also sets `LANGUAGE` for the titles themselves, and `LANG` once the matching locale has been generated. Gettext apps like Tux Paint follow the first. Qt apps like GCompris and KStars read the second and ignore the first, which is why the locale has to be generated at all.

## Building from source

The image is built with CustomPiOS on top of pi-gen, and it is arm64.

```bash
git clone https://github.com/kevinl95/ChipBit.git
cd ChipBit
echo "/path/to/CustomPiOS/src" > image/custompios_path
cat > image/config.local <<'EOF'
export BASE_ARCH=arm64
export BASE_ZIP_IMG="/path/to/raspios-bookworm-arm64-lite.img.xz"
EOF
sudo bash image/build_dist
```

The finished image lands in `image/workspace/`.

Tests and lint run without a Pi:

```bash
make test
make lint
```

## Troubleshooting

Start with the parent console's **Diagnostics** page. It runs the launcher log, the kiosk log, disk space, the input device list, and what the compositor can see, which covers most of what you would otherwise SSH in for.

**The reader does nothing.**
Check that it enumerates: `lsusb` should show an HID device, and `sudo evtest` should list it. If it appears but produces no events when you tap a card, the reader is probably in a serial mode rather than HID. Rescan the HID config barcode from its manual.

**A card gets read but nothing happens, or the UID looks too short.**
ChipBit keeps only `0-9` and `A-F` from whatever the reader types, and waits for Enter to end the scan. A reader that adds a prefix or a checksum will have those characters dropped, and a reader that never sends Enter will never complete a scan at all. Tap a card into any text field on another machine and look at exactly what it types, including whether it presses Enter.

**The mouse does not move.**
On a Pi 400, try a black USB 2 port instead of a blue USB 3 one. Old low-speed USB 1.1 mice enumerate badly on the xHCI controller.

**The launcher grabbed the wrong input device.**
The launcher takes an exclusive grab on whatever it decides is the reader, so a misidentified device disappears from the kiosk entirely. `journalctl -u chipbit-launcher | grep 'reader open'` names the device it grabbed. If that is your keyboard rather than your reader, pass `--reader-device` in `chipbit-launcher.service` pointing at a stable path from `/dev/input/by-id/`.

**A card launches the engine but the game does not start.**
`journalctl -u chipbit-launcher -f`, then tap the card again. Nine times out of ten the path in `catalog.yaml` does not match where the data actually is, and the engine exits immediately. For ScummVM specifically, check that `game_id` is ScummVM's ID and not the folder name.

**Enrollment says it cannot reach the internet.**
Titles with `bundled: false` download on first enroll. Check Wi-Fi in Settings and tap the card again. A download that takes too long is stopped rather than left hanging, and it tells you so instead of failing silently.

**The screen is idle and nothing responds.**
`systemctl status chipbit-kiosk` will tell you whether the compositor died. `cage` restarting takes a few seconds and the screen goes black in the meantime, which looks like a hang.

**The parent console will not open.**
`systemctl status chipbit-web`. The console is only reachable while unlocked, and the unlock times out, so tap the admin card again. If you are trying from another machine, use the Pi's IP and port 8080; `chipbit.local` will not resolve.

**I want out of the kiosk.**
SSH in. There is no key combination to escape to a desktop, because there is no desktop.

## What's in the image

Raspberry Pi OS Bookworm, 64-bit, plus:

- ScummVM, and Ruffle, all without content
- GCompris and Tux Paint, usable immediately
- Marble, KStars, SuperTux, SuperTuxKart and LibreOffice Writer, installed on first enroll
- Scratch and PBS Kids, running in the browser against an allowlist of domains
- The ChipBit daemon (Python, reads the reader through evdev), the parent web UI, and a localhost control API
- `cage`, a Wayland kiosk compositor, as the only thing on the display
- English, German, French and Spanish

## Content

ChipBit ships engines. You supply the games.

That means GOG re-releases, freeware and shareware, or your own discs that you rip yourself.

## License

Apache 2.0. See [LICENSE](LICENSE).
