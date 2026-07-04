# ChipBit

Hand a kid a card, they tap it, the game launches. No menus, no browser, no escape to the desktop. When they're done, a Home card puts everything back.

It's the cartridge ritual rebuilt for software — pick a thing, slot it in, it runs — applied to the classic edutainment that raised a generation: Reader Rabbit, the Living Books, Putt-Putt and Freddi Fish, Math Blaster, and the Flash-era web games that are otherwise unplayable today.

---

## How it works

1. Flash the image to an SD card and boot the Pi.
2. On first boot, connect Wi-Fi and point it at your game data (network share or USB stick).
3. Tap a parent card to unlock, pick a title from the catalog, tap a blank card to bind it.
4. Print and laminate the matching card art (optional but satisfying).
5. Hand the cards to the kid.

Behind the scenes: a daemon reads the RFID reader, looks up the card UID, and launches the right engine — ScummVM, DOSBox, Ruffle, a native app, or a locked-down browser. The parent web UI runs on the Pi itself; no cloud account needed.

---

## What's included

**Bundled in the image:**
- Raspberry Pi OS (Bookworm, 32-bit) with the ChipBit kiosk and parent UI
- **ScummVM**, **DOSBox Staging**, **Ruffle** — bring your own game data
- **GCompris** and **Tux Paint** — ready to use out of the box
- **Scratch** and **PBS Kids** — web titles with allowlisted domains

**Installed on first card enroll:**
- Marble, KStars, SuperTux, SuperTuxKart (downloaded from the Pi OS repos on demand)

**You bring:**
Game data you legally own — purchased re-releases (GOG), freeware/shareware, or rips of your own discs. ChipBit ships the engines, not the content. Same deal as ScummVM and DOSBox have always had.

---

## Hardware

- **Raspberry Pi 400 or 500** (recommended) — the keyboard form factor means nothing loose to unplug or knock over. A Pi 4 or newer works too.
- **USB HID RFID reader**, 13.56 MHz MIFARE — shows up as a keyboard, no driver needed.
- **MIFARE Classic 1K cards or NFC stickers** — each has a unique factory UID so you don't need to write anything to the card; the UID is the key.

---

## Status

Beta. The image builds, boots, and the core loop (tap card → launch → tap Home → stop) works. Card enrollment through the parent UI works. The title catalog is small but the infrastructure to extend it is solid — adding a new title is a few lines in `catalog.yaml`.

Rough spots: user-supplied content (ScummVM games, DOSBox titles, Flash SWFs) requires manual file placement rather than a guided wizard. That's the next thing.
