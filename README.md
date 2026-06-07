# ChipBit

**Hand a kid a card, they tap it, and parent‑approved educational software launches.** No menus to navigate, no internet rabbit holes, no escape to the desktop. When they're done, a "Home" card puts everything back. Parents curate the shelf; kids just play.

It's the cartridge ritual — pick a thing, slot it in, it runs — rebuilt for the classic edutainment that raised a generation: Reader Rabbit, the Living Books, Putt‑Putt and Freddi Fish, Math Blaster, and the Flash‑era web games that are otherwise unplayable today.

---

## How it works

1. **Flash** the image to an SD card and boot the Pi.
2. **Set up** on first boot: connect Wi‑Fi, drop your game data into the games folder (network share or a USB stick).
3. **Register cards** in the web UI (unlocked by tapping a parent card): pick a title from the auto‑detected list, tap a blank card to bind it — or import a manifest card pack.
4. **Print & laminate** the matching card art (optional).
5. **Hand the cards to the kid.** Tap a card → it launches fullscreen. Tap **Home** → back to the idle screen.

Behind the cards, a small daemon reads the RFID reader, looks the UID up in a config file, and launches the right engine — ScummVM, DOSBox, a native Linux app, a locked‑down browser, or Ruffle for Flash titles.

---

## What's bundled vs. what you bring

**Bundled (all open source / freely distributable):**
- Raspberry Pi OS image with the kiosk launcher + card‑registration UI
- **ScummVM** (Living Books, Humongous Entertainment, point‑and‑click classics)
- **DOSBox** (DOS‑era edutainment)
- **Ruffle** (resurrects dead Flash‑era educational web games)
- Native Linux educational software: **GCompris**, **Tux Paint**, **TuxMath/TuxTyping**, **KDE Edu** (Marble, KStars, KTurtle…), **Scratch**

**You bring** the game data you legally own — purchased re‑releases (e.g. GOG), freeware/shareware, or rips of your own original discs. ChipBit ships the *engines*, never the copyrighted content; the two stay cleanly separated, the same way ScummVM and DOSBox have always worked.

---

## Hardware

- **Raspberry Pi 400 or 500** (recommended): the all‑in‑one keyboard form factor is the most kid‑proof — nothing loose to unplug. The 400 is cheap and plentiful secondhand; the 500 keeps it on current silicon. The same image also boots a Pi 4 or 5.
- **USB HID RFID reader**, 13.56 MHz MIFARE — driver‑free, "types" the card UID like a keyboard. (Advertise the *spec*, not a single link; those listings rotate.)
- **MIFARE Classic 1K cards + NFC stickers** — each ships with a unique factory UID, so no card‑writing is needed; the stickers tuck inside printed card art.

---

## Principles

- **Local‑first.** No account, no cloud, no subscription. Everything lives on the Pi, owned by the parent. (Contrast the Toniebox‑style walled gardens — same physical‑token magic, none of the lock‑in.)
- **Privacy‑respecting.** No telemetry. Web cards run through a local DNS/proxy safety layer.
- **Legally grounded.** Open engines + the data you own. A preservation tool, not a piracy box.
- **Open & forkable.** Built with CustomPiOS/pi‑gen; build scripts public so makers can extend it.
- **Safe by default.** A locked kiosk a child can use unattended, gated config a child can't reach.

---

## Status

Concept / in development. Core launcher daemon and card schema designed; image build, registration UI, and manifest packs in progress.