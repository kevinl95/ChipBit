"""Every user-visible string in the ChipBit web UI, in one place.

Why this file exists
--------------------
Copy used to live inline in the HTML templates in ``web.py``.  That made two
things hard: translating the UI at all, and keeping the same idea worded the
same way in two places (we shipped a screen telling parents to press a button
that had been renamed months earlier).  One catalog fixes both.

Adding a language
-----------------
Every entry below is the English reference text.  A translation is a file of
the same keys with translated values; anything it leaves out falls back to the
English here, so a partial translation is safe to ship and a contributor can
send in ten strings without breaking a screen.

The child-facing kiosk is only the ``kiosk.*`` keys -- translate those and a
child who cannot read English gets a fully native machine, even if the parent
console is still in English.

Notes for translators
---------------------
* ``{name}`` placeholders are substituted at runtime.  Keep them exactly as
  written; you may move them within the sentence.
* A few strings contain inline HTML such as ``<code>/games/</code>``.  Keep the
  tags, translate the words around them.  Paths like ``/games/`` are real
  filesystem locations and must not be translated.
* Text here is written into the page without HTML-escaping, because some of it
  is deliberately marked up.  Only ship strings you would put in the repo.
* "ChipBit" is the product name and is deliberately not in this catalog.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

# Where translations are looked for, lowest priority first.  This mirrors the
# catalog's split exactly (--catalog / --user-catalog): the image ships its
# locales read-only under /usr/share, and /var/lib is writable, so a translator
# can drop a file onto a running Pi and restart the service to see their work
# without rebuilding an image.  That loop is the whole point -- a translation
# nobody can preview is a translation nobody finishes.
SYSTEM_LOCALE_DIR = Path("/usr/share/chipbit/locales")
USER_LOCALE_DIR = Path("/var/lib/chipbit/locales")

# The language actually in effect.  Read through language_tag(); do NOT import
# this name directly, or callers keep a stale copy from before load_locale().
_language_tag = "en"

# Translated strings for the active language.  Keys absent here fall back to
# STRINGS, which is why a partial translation is safe to ship.
_active: dict[str, str] = {}

# Keys are <surface>.<context>.<element>.  Surfaces map to what the reader is
# looking at, not to which function renders it.
STRINGS: dict[str, str] = {
    # --- shared -----------------------------------------------------------
    "common.diagnostics": "Diagnostics",
    "common.shutdown": "Shut down",
    "common.wifi_setup": "Wi-Fi setup",
    "common.scanning": "Scanning…",
    "common.back": "Back",
    "common.network": "Network",
    "common.network_name": "Network name",
    "common.password": "Password",
    "common.name": "Name",
    "console.settings.language": "Language",
    # --- masthead ---------------------------------------------------------
    "layout.parent_controls": "Parent controls:",
    "layout.checking": "checking…",
    "layout.mode.first-run": "not set up yet",
    "layout.mode.locked": "locked",
    "layout.mode.unlocked": "unlocked",
    # --- the kiosk: everything a child ever sees --------------------------
    "kiosk.idle.title": "Tap a card",
    "kiosk.idle.body": "Hold a card against the reader to start playing.",
    "kiosk.enroll.title": "Tap a card now",
    "kiosk.enroll.body": "This card is about to become a game card.",
    "kiosk.first_run.title": "Tap a card to make it the admin card",
    "kiosk.first_run.body": "No network needed for first-run setup.",
    "kiosk.loading.body": "Getting it ready…",
    "kiosk.unknown.title": "Ask a grown-up",
    "kiosk.unknown.body": "This card isn't set up yet.",
    "kiosk.unknown.body_uid": (
        "This card isn't set up yet. Card {uid} can be added in the "
        "parent console."
    ),
    # Shown by the kiosk's own JavaScript when the event stream drops.
    "kiosk.offline.title": "Reconnecting to ChipBit",
    "kiosk.offline.body": "Just a moment.",
    # --- first run --------------------------------------------------------
    "firstrun.heading": "Make this card the admin card",
    "firstrun.lede": (
        "Pick one card and keep it somewhere the kids can't reach — it's the "
        "key to this page. Hold it against the reader now."
    ),
    "firstrun.note": (
        "Nothing to click. This page updates by itself once the reader sees "
        "the card."
    ),
    # --- lock screen ------------------------------------------------------
    "lock.heading": "Tap your admin card to unlock",
    "lock.lede": (
        "Hold the card you set aside against the reader. This page updates "
        "by itself."
    ),
    "lock.playing": "Playing right now:",
    # --- parent console ---------------------------------------------------
    "console.heading": "Game cards",
    "console.lede": (
        "Pick a title, hold a blank card against the reader, and that card "
        "launches the title from then on. The color on each tile is the one "
        "the screen turns when your child taps it."
    ),
    "console.back_to_play": "Back to play mode",
    "console.admin_card": "Admin card",
    "console.tap_banner": (
        "Hold the card against the reader now — waiting up to 30 seconds."
    ),
    "console.tile.bind": "Tap a card to bind",
    "console.tile.no_card": "No card yet",
    # Readiness chips.  Deliberately in a parent's words, not the catalog's.
    "console.state.ready": "Ready",
    "console.state.needs_files": "Needs game files",
    "console.state.downloads": "Downloads on first use",
    "console.cards.heading": "Cards you've made",
    "console.cards.col_card": "Card",
    "console.cards.col_launches": "Launches",
    "console.cards.col_change": "Change",
    "console.cards.empty": (
        "Nothing bound yet — pick a title above and tap a blank card."
    ),
    "console.cards.reassign": "Reassign",
    "console.cards.reassign_label": "Title for card {uid}",
    "console.cards.disable": "Disable",
    "console.cards.disable_confirm": "Stop this card launching anything?",
    "console.files.heading": "Game files",
    "console.files.body": (
        "Copy game data from a USB drive into <code>/games/</code> so ScummVM, "
        "DOSBox, and Ruffle titles can find it."
    ),
    "console.files.open": "Open the file browser",
    "console.custom.heading": "Add your own",
    "console.custom.body": (
        "Cards for software you own or a website you'd like your child to "
        "visit. Save it here first, then bind a card to it from the tiles "
        "above."
    ),
    "console.custom.web.summary": "A website",
    "console.custom.web.url": "URL",
    "console.custom.web.save": "Save website card",
    "console.custom.exec.summary": "An app already installed on the Pi",
    "console.custom.exec.cmd": "Launch command",
    "console.custom.exec.apt": "Apt package to install (optional)",
    "console.custom.exec.save": "Save app card",
    "console.custom.scummvm.summary": "A ScummVM game (you supply the game data)",
    "console.custom.scummvm.game_id": "ScummVM game ID",
    "console.custom.scummvm.data_dir": (
        "Data folder under /games/ (blank for scummvm/&lt;name&gt;)"
    ),
    "console.custom.scummvm.save": "Save ScummVM card",
    "console.custom.dosbox.summary": "A DOSBox game (you supply the game data)",
    "console.custom.dosbox.conf": "DOSBox config file path under /games/",
    "console.custom.dosbox.save": "Save DOSBox card",
    "console.custom.ruffle.summary": "A Flash game (you supply the .swf)",
    "console.custom.ruffle.swf": "SWF path under /games/",
    "console.custom.ruffle.save": "Save Ruffle card",
    "console.settings.heading": "Settings",
    "console.settings.wifi": "Wi-Fi",
    "console.settings.connect": "Connect",
    "console.settings.keyboard": "Keyboard layout",
    "console.settings.layout": "Layout",
    "console.settings.apply": "Apply",
    "console.settings.this_pi": "This Pi",
    "console.settings.lock": "Lock parent controls",
    "console.settings.shutdown_confirm": "Shut down the Pi now?",
    # Keyboard layout names.  The parenthesised part is the physical layout
    # and is the same word in most languages; translate the language name.
    "keyboard.us": "US (QWERTY)",
    "keyboard.gb": "UK (QWERTY)",
    "keyboard.de": "German (QWERTZ)",
    "keyboard.fr": "French (AZERTY)",
    "keyboard.es": "Spanish",
    "keyboard.it": "Italian",
    "keyboard.pt": "Portuguese",
    "keyboard.nl": "Dutch",
    # --- file browser -----------------------------------------------------
    "files.crumb_back": "← Parent console",
    "files.heading": "Game files",
    "files.lede": (
        "Find a game folder on a USB drive and copy it into "
        "<code>/games/</code>."
    ),
    "files.drives": "Drives",
    "files.rescan": "Rescan",
    "files.none": "No drives detected. Plug in a drive and click Rescan.",
    "files.mount": "Mount: {label}",
    "files.copy_heading": "Copy this folder to /games/",
    "files.copy_type": "Game type",
    "files.copy_type.scummvm": "ScummVM",
    "files.copy_type.dosbox": "DOSBox",
    "files.copy_type.flash": "Flash / Ruffle",
    "files.copy_type.other": "Other",
    "files.copy_dest": "Destination in /games/",
    "files.copy_button": "Copy folder",
    "files.contents": "Contents",
    "files.up": "[up]",
    "files.up_drives": "[up — drives]",
    "files.empty": "Empty folder",
    "files.back_to_drives": "Back to drives",
    # --- copy progress ----------------------------------------------------
    "copy.heading": "Copying…",
    "copy.body": "Large games can take a few minutes. Leave this page open.",
    "copy.unknown_job": "Unknown copy job — it may have already completed.",
    "copy.done": "Copy complete. Taking you to the card form…",
    # --- first-run setup pages -------------------------------------------
    "setup.title": "ChipBit setup",
    "setup.country.heading": "Wi-Fi country",
    "setup.country.body": (
        "Choose the country where this ChipBit is being used. This sets the "
        "Wi-Fi radio channels available on your network. The device will "
        "reboot once to apply the setting."
    ),
    "setup.country.label": "Country",
    "setup.country.placeholder": "Select a country…",
    "setup.country.submit": "Set country and reboot",
    "setup.reboot.heading": "Rebooting…",
    "setup.reboot.body": (
        "Applying Wi-Fi country settings and rebooting. This page will reload "
        "automatically in about 20 seconds."
    ),
    "setup.wifi.heading": "Connect to Wi-Fi",
    "setup.wifi.body": (
        "Some activities (like Marble, KStars, and SuperTux) download and "
        "install when a card is first enrolled. They need an internet "
        "connection the first time. You can skip this and connect later in "
        "Settings."
    ),
    "setup.wifi.submit": "Connect and continue",
    "setup.wifi.connecting": "Connecting…",
    "setup.wifi.skip": "Skip — I'll connect later",
    # --- strings used by the console's own JavaScript ---------------------
    "js.enrolling.title": "Enrolling…",
    "js.enrolling.body": "Tap your card to the reader now",
    "js.waiting_button": "Waiting for card…",
    "js.failed.title": "Enrollment failed",
    "js.failed.body": "Something went wrong.",
    "js.try_again": "Try again",
    "js.working": "Working…",
    # --- results of an action --------------------------------------------
    "msg.language_set": "Language updated.",
    "msg.bound": "Bound {uid} to {title}",
    "msg.added": (
        "Added “{label}” — use “Tap a card to bind” on its tile above to "
        "assign a card"
    ),
}


# Languages ChipBit can be set to.  A contributor adding a translation adds one
# line here and one <code>.yaml file; nothing else in the codebase needs to
# know about it.
#
# The second value is the POSIX locale to request for *launched titles*.  It is
# only used if the system has actually generated that locale -- on Debian,
# LANG=de_DE.UTF-8 silently falls back to C when locale-gen has not run, which
# is the classic reason "I set LANG and nothing happened".
LANGUAGES: dict[str, tuple[str, str]] = {
    "en": ("English", "en_US.UTF-8"),
    "de": ("Deutsch", "de_DE.UTF-8"),
    "fr": ("Français", "fr_FR.UTF-8"),
    "es": ("Español", "es_ES.UTF-8"),
    "it": ("Italiano", "it_IT.UTF-8"),
    "pt": ("Português", "pt_PT.UTF-8"),
    "nl": ("Nederlands", "nl_NL.UTF-8"),
}

# The parent's choice, read by both the web service (for its own UI) and the
# launcher (for the environment it hands to titles).  A file rather than a
# service setting so a change takes effect on the next launch without a
# restart, matching how wifi_country already works.
LANGUAGE_FILE = Path("/var/lib/chipbit/language")


def read_language(path: Path | None = None) -> str:
    """Return the configured language code, or "en" if unset or unknown."""
    target = path or LANGUAGE_FILE
    try:
        code = target.read_text(encoding="utf-8").strip()
    except OSError:
        return "en"
    return code if code in LANGUAGES else "en"


def write_language(code: str, path: Path | None = None) -> None:
    """Persist the parent's language choice."""
    if code not in LANGUAGES:
        raise ValueError(f"unknown language: {code!r}")
    target = path or LANGUAGE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(code + "\n", encoding="utf-8")


def generated_locales(runner: object = None) -> frozenset[str]:
    """Locales the system can actually set, from `locale -a`.

    Asking is the only reliable check: Debian keeps generated locales in a
    binary archive, so there is no directory to stat.
    """
    import subprocess

    run = runner or subprocess.run
    try:
        result = run(
            ["locale", "-a"], check=False, capture_output=True, text=True
        )
    except OSError:
        return frozenset()
    if getattr(result, "returncode", 1) != 0:
        return frozenset()
    # `locale -a` prints de_DE.utf8; callers hold de_DE.UTF-8.
    return frozenset(
        line.strip().replace("utf8", "UTF-8")
        for line in (result.stdout or "").splitlines()
        if line.strip()
    )


def child_locale_env(
    code: str, *, generated: frozenset[str] | None = None
) -> dict[str, str]:
    """Environment additions that make a launched title speak ``code``.

    LANGUAGE alone is enough for gettext apps (TuxPaint, SuperTux) and needs no
    locale generation.  Qt apps -- GCompris, KStars, Marble -- read
    QLocale::system(), which consults LC_ALL/LC_MESSAGES/LANG and ignores
    LANGUAGE entirely, so they need a real generated locale.  Setting LANG to
    an ungenerated locale is worse than leaving it alone, so we don't.
    """
    if code == "en" or code not in LANGUAGES:
        return {}
    env = {"LANGUAGE": code}
    posix_locale = LANGUAGES[code][1]
    available = generated if generated is not None else generated_locales()
    if posix_locale in available:
        env["LANG"] = posix_locale
    else:
        log.info(
            "locale %s is not generated; launched titles get LANGUAGE=%s only "
            "(gettext apps translate, Qt apps stay English)",
            posix_locale, code,
        )
    return env


@dataclass(frozen=True)
class LocaleChoice:
    """One option for the language picker."""

    code: str
    name: str


def available_languages(dirs: tuple[Path, ...] | None = None) -> list[LocaleChoice]:
    """English plus every language with a locale file present."""
    search = dirs if dirs is not None else (SYSTEM_LOCALE_DIR, USER_LOCALE_DIR)
    found = {"en"}
    for directory in search:
        try:
            entries = list(directory.glob("*.yaml"))
        except OSError:
            continue
        for entry in entries:
            if entry.stem in LANGUAGES:
                found.add(entry.stem)
    return [LocaleChoice(c, LANGUAGES[c][0]) for c in sorted(found)]


def language_tag() -> str:
    """BCP 47 tag for the active language, for <html lang="...">.

    A function, not a constant: it changes when a locale is loaded, and an
    imported constant would keep whatever value it had at import time.
    """
    return _language_tag


def t(key: str, **fields: object) -> str:
    """Return the UI string for ``key``, substituting any ``{placeholders}``.

    Falls back to the English reference text whenever the active language has
    no entry for the key, so a contributor can send in ten strings without
    leaving holes in a screen.

    Raises KeyError for a key that exists in neither: a typo should fail
    loudly in tests rather than render an empty element.
    """
    text = _active.get(key) or STRINGS[key]
    return text.format(**fields) if fields else text


@dataclass(frozen=True)
class LocaleReport:
    """What happened when a locale was loaded -- shown by --locale and /debug."""

    code: str
    path: Path | None
    translated: int
    total: int
    unknown_keys: tuple[str, ...]

    @property
    def missing(self) -> int:
        return self.total - self.translated

    def summary(self) -> str:
        if self.path is None:
            return f"{self.code}: no locale file found; using English"
        pct = (100 * self.translated) // self.total if self.total else 0
        line = (
            f"{self.code}: {self.translated}/{self.total} strings ({pct}%), "
            f"{self.missing} falling back to English"
        )
        if self.unknown_keys:
            line += f"; {len(self.unknown_keys)} unknown key(s) ignored"
        return line


def locale_search_paths(
    code: str, dirs: tuple[Path, ...] | None = None
) -> list[Path]:
    """Candidate files for ``code``, lowest priority first."""
    search = dirs if dirs is not None else (SYSTEM_LOCALE_DIR, USER_LOCALE_DIR)
    return [d / f"{code}.yaml" for d in search]


def load_locale(
    code: str, dirs: tuple[Path, ...] | None = None
) -> LocaleReport:
    """Activate ``code``, merging every locale file found for it.

    Later directories win, so a file dropped in /var/lib overrides the one
    baked into the image -- the same precedence the catalog uses.  Unknown
    keys are dropped with a warning rather than failing the load: a
    translation written against a newer ChipBit must not brick an older one.
    """
    global _language_tag, _active

    merged: dict[str, str] = {}
    unknown: list[str] = []
    found: Path | None = None

    for path in locale_search_paths(code, dirs):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, yaml.YAMLError) as exc:
            log.warning("could not read locale %s: %s", path, exc)
            continue
        if not isinstance(raw, dict):
            log.warning("locale %s must be a mapping of key: text", path)
            continue
        found = path
        for key, value in raw.items():
            if not isinstance(value, str) or not value.strip():
                continue
            if key not in STRINGS:
                unknown.append(str(key))
                continue
            merged[str(key)] = value

    _active = merged
    _language_tag = code if found is not None else "en"
    if unknown:
        log.warning(
            "locale %s has %d key(s) not in this ChipBit: %s",
            code, len(unknown), ", ".join(sorted(unknown)[:5]),
        )
    return LocaleReport(
        code=code,
        path=found,
        translated=len(merged),
        total=len(STRINGS),
        unknown_keys=tuple(sorted(unknown)),
    )


def use_english() -> None:
    """Reset to the built-in English reference text."""
    global _language_tag, _active
    _language_tag, _active = "en", {}
