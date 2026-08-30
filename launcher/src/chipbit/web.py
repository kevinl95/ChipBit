"""Plain-HTML web service and kiosk shell for the ChipBit runtime."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import pwd
import re
import shutil
import subprocess
import threading
import time
import zlib
from collections import defaultdict
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from textwrap import dedent
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

from .installer import (
    DataMissingError,
    InstallationError,
    InstallProgress,
    NetworkUnavailableError,
    enroll_card,
    has_required_data,
)
from .models import (
    CardsConfig,
    Catalog,
    CatalogTitle,
    ConfigLoadError,
    SystemCard,
    load_cards,
    load_catalog_merged,
    normalize_uid,
    save_cards,
    save_user_title,
)
from .strings import (
    LANGUAGES,
    available_languages,
    language_is_set,
    language_tag,
    load_locale,
    peek,
    read_language,
    t,
    use_english,
    write_language,
)

log = logging.getLogger(__name__)

DEFAULT_EVENT_POLL_SECS = 1.0
_MEDIA_ROOT = Path("/media")
# Files a parent would recognise as "my child's work" and want to look at
# before copying.  Anything else in a work directory is still backed up, just
# not previewed.
_WORK_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp"})
# A wall of full-size PNGs is served straight from disk (no image library on
# the device to make thumbnails), so cap what one page will render.
_WORK_PREVIEW_LIMIT = 120

# Persistent state root.  Titles are often pointed here rather than at $HOME
# (TuxPaint's --savedir, Chromium's --user-data-dir), so work lives under it
# as legitimately as it does under home.
_STATE_ROOT = Path("/var/lib/chipbit")

_WIFI_COUNTRY_FILE = Path("/var/lib/chipbit/wifi_country")
# Written when the user completes (or skips) WiFi setup for the first time.
# /kiosk redirects to /setup while this file is absent and country is set.
_WIFI_SETUP_FILE = Path("/var/lib/chipbit/wifi_setup_done")

# Curated list of (ISO-3166 alpha-2, display name) sorted by display name.
_WIFI_COUNTRIES: list[tuple[str, str]] = [
    ("AT", "Austria"),
    ("AU", "Australia"),
    ("BE", "Belgium"),
    ("BR", "Brazil"),
    ("CA", "Canada"),
    ("CZ", "Czech Republic"),
    ("DK", "Denmark"),
    ("FI", "Finland"),
    ("FR", "France"),
    ("DE", "Germany"),
    ("GR", "Greece"),
    ("HU", "Hungary"),
    ("IN", "India"),
    ("IE", "Ireland"),
    ("IT", "Italy"),
    ("JP", "Japan"),
    ("MX", "Mexico"),
    ("NL", "Netherlands"),
    ("NZ", "New Zealand"),
    ("NO", "Norway"),
    ("PL", "Poland"),
    ("PT", "Portugal"),
    ("RO", "Romania"),
    ("SG", "Singapore"),
    ("SK", "Slovakia"),
    ("ZA", "South Africa"),
    ("ES", "Spain"),
    ("SE", "Sweden"),
    ("CH", "Switzerland"),
    ("GB", "United Kingdom"),
    ("US", "United States"),
]
# Console keyboard-layout picker; names come from strings.py ("keyboard.<code>").
_KEYBOARD_LAYOUTS: tuple[str, ...] = (
    "us", "gb", "de", "fr", "es", "it", "pt", "nl",
)

_VALID_COUNTRY_CODES: frozenset[str] = frozenset(c for c, _ in _WIFI_COUNTRIES)


# JS files can't call t(), so the scripts carry sentinels that are swapped for
# the active language's text as the page is written.
_SCRIPT_STRINGS: dict[str, str] = {
    "__T_MODE_FIRST_RUN__": "layout.mode.first-run",
    "__T_MODE_LOCKED__": "layout.mode.locked",
    "__T_MODE_UNLOCKED__": "layout.mode.unlocked",
    "__T_WORKING__": "js.working",
    "__T_WAITING__": "js.waiting_button",
    "__T_ENROLLING__": "js.enrolling.title",
    "__T_ENROLLING_BODY__": "js.enrolling.body",
    "__T_FAILED__": "js.failed.title",
    "__T_FAILED_BODY__": "js.failed.body",
    "__T_TRY_AGAIN__": "js.try_again",
    "__T_SLOW_HINT__": "js.slow_hint",
}


def _fill_script_strings(script: str) -> str:
    """Swap __T_*__ sentinels in an inline script for translated text."""
    for sentinel, key in _SCRIPT_STRINGS.items():
        if sentinel in script:
            script = script.replace(sentinel, _js_in_attr(t(key)))
    return script


def _js_in_attr(text: str) -> str:
    """Escape UI text for a single-quoted JS string inside an HTML attribute.

    Order matters.  JS-escape first, then HTML-escape everything *except* the
    apostrophe: turning ' into &#x27; would decode back to a bare quote and
    close the JS string early.  No English string here contains one, but
    plenty of translations will ("Voulez-vous vraiment l'arrêter ?").
    """
    js = text.replace("\\", "\\\\").replace("'", "\\'")
    return escape(js, quote=False).replace('"', "&quot;")


def _reboot_after_delay(runner: CommandRunner, delay: float = 2.0) -> None:
    time.sleep(delay)
    try:
        runner(["sudo", "systemctl", "reboot"], check=False, capture_output=True)
    except Exception:
        pass


def _unescape_mount_path(s: str) -> str:
    """Decode octal escapes in a /proc/mounts field (e.g. \\040 → space)."""
    return re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1), 8)), s)


_DOS_SKIP_EXES: frozenset[str] = frozenset({
    "install.exe", "setup.exe", "setup.com", "uninst.exe", "unins000.exe",
    "dos4gw.exe", "dos32a.exe", "cwsdpmi.exe", "dpmi16bi.ovl",
    "install.bat", "setup.bat", "autorun.bat", "autoexec.bat",
})


def _find_dos_executable(game_dir: Path) -> str | None:
    """Return the most likely game-launch filename in a DOS game directory."""
    candidates: list[str] = []
    try:
        for f in game_dir.iterdir():
            if not f.is_file():
                continue
            if (
                f.suffix.lower() in (".exe", ".com", ".bat")
                and f.name.lower() not in _DOS_SKIP_EXES
            ):
                candidates.append(f.name.upper())
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda n: (n.endswith(".BAT"), n))
    return candidates[0]


def _copytree_permissive(src: Path, dst: Path) -> None:
    """Copy a directory tree without preserving source permissions.

    shutil.copytree copies directory permission bits verbatim via copystat.
    Optical discs (ISO 9660/UDF) have directories with mode 0o555, which
    copytree applies to the destination mid-copy, causing EACCES on the next
    write into that directory.  This variant always creates directories 0o755
    and files 0o644 regardless of source permissions.
    """
    dst.mkdir(mode=0o755, exist_ok=True)
    for item in src.iterdir():
        dst_item = dst / item.name
        if item.is_symlink():
            continue
        if item.is_dir():
            _copytree_permissive(item, dst_item)
        elif item.is_file():
            shutil.copy2(str(item), str(dst_item))
            dst_item.chmod(0o644)
    dst.chmod(0o755)


# The wordmark glyph: a card, because that is the whole product.
CHIPBIT_MARK = (
    '<svg width="22" height="18" viewBox="0 0 22 18" aria-hidden="true">'
    '<rect x="1.2" y="1.2" width="19.6" height="15.6" rx="3.4" '
    'fill="none" stroke="currentColor" stroke-width="2.4"/>'
    '<rect x="5" y="5" width="7" height="5.5" rx="1.4" fill="currentColor"/>'
    "</svg>"
)

# One drawing, used on every screen that is waiting for a card: the kiosk, the
# first-run page and the lock screen.  It shows the gesture -- hold the card to
# the reader -- which is the only instruction that matters and the only one a
# pre-reader can follow.
CARD_TAP_SVG = (
    '<svg class="card-tap" viewBox="0 0 160 120" fill="none" aria-hidden="true"'
    ' stroke="currentColor" stroke-width="4"'
    ' stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="8" y="42" width="28" height="48" rx="7" fill="#fffdf8"/>'
    '<path class="wave" d="M46 52a20 20 0 0 1 0 28"/>'
    '<path class="wave" d="M58 45a32 32 0 0 1 0 42"/>'
    '<path class="wave" d="M70 38a44 44 0 0 1 0 56"/>'
    '<g transform="rotate(-8 112 60)">'
    '<rect x="80" y="26" width="64" height="68" rx="9" fill="#fffdf8"/>'
    '<rect x="92" y="40" width="22" height="17" rx="4" fill="#f0b429"/>'
    '<path d="M92 70h40M92 82h24"/>'
    "</g>"
    "</svg>"
)

# Card inks -----------------------------------------------------------------
#
# Every title is dealt one of these flat printed inks, picked from its catalog
# id.  The same ink tints the title's tile in the parent console and floods the
# whole kiosk screen while that title loads, so "the blue card" means the same
# thing on the table, on the TV, and on the laminated card art.  crc32 rather
# than hash() because the choice has to survive a reboot.
CARD_INKS: tuple[tuple[str, str], ...] = (
    ("#c0392f", "#ffffff"),  # tomato
    ("#b5561a", "#ffffff"),  # clay
    ("#f0b429", "#1a1a19"),  # school-bus yellow, takes dark type
    ("#2b7a4b", "#ffffff"),  # pine
    ("#1f5fa8", "#ffffff"),  # ink blue
    ("#0c6f78", "#ffffff"),  # teal
    ("#a83a6e", "#ffffff"),  # mulberry
)


def ink_for(key: str) -> tuple[str, str]:
    """Return the (background, foreground) ink pair dealt to a catalog id."""
    return CARD_INKS[zlib.crc32(key.encode("utf-8")) % len(CARD_INKS)]


# Served for any /art/<name> that doesn't exist on disk.  This is the *label*
# of an unprinted card -- chip mark and blank ruled lines, full bleed -- not a
# picture of a card: the kiosk already frames it in one, and a card drawn
# inside a card frame reads as a mistake.
_DEFAULT_ART_SVG: bytes = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 150 200">'
    b'<rect width="150" height="200" fill="#f4ede0"/>'
    b'<rect x="26" y="34" width="44" height="34" rx="7" '
    b'fill="#f0b429" stroke="#22201c" stroke-width="5"/>'
    b'<rect x="26" y="96" width="98" height="9" rx="4.5" fill="#22201c"/>'
    b'<rect x="26" y="119" width="66" height="9" rx="4.5" fill="#c9c2b4"/>'
    b'<rect x="26" y="142" width="80" height="9" rx="4.5" fill="#c9c2b4"/>'
    b"</svg>"
)

PAGE_CSS = dedent("""
    /* Parent console.  This is a workbench: a parent stands at it for two
       minutes to bind a card, then leaves.  So it is built from rules,
       headings and whitespace rather than a stack of floating panels, and the
       only things that carry a shadow are the things you can press or pick up.
       The kiosk (KIOSK_CSS) is the opposite surface and shares nothing but the
       inks -- one is read across a room by a six-year-old, this one is read at
       arm's length by an adult in a hurry. */
    :root {
      color-scheme: light;
      --paper:   #fbf7ef;
      --card:    #fffdf8;
      --ink:     #1a1a19;
      --ink-70:  #4b4842;
      --ink-45:  #77726a;
      --rule:    #ded5c6;
      --accent:  #1f5fa8;
      --ok:      #2b7a4b;
      --warn:    #8a5e06;
      --bad:     #b3271b;
      --serif:   Georgia, "Iowan Old Style", "Palatino Linotype",
                 "Noto Serif", "DejaVu Serif", serif;
      --sans:    Piboto, system-ui, -apple-system, "Segoe UI",
                 "Noto Sans", "DejaVu Sans", sans-serif;
      --mono:    "DejaVu Sans Mono", ui-monospace, "Cascadia Mono",
                 Menlo, monospace;
      /* A 1.6-ish step scale, so spacing carries meaning instead of
         everything sitting one gap-4 apart. */
      --s1: 0.25rem;
      --s2: 0.5rem;
      --s3: 0.8rem;
      --s4: 1.25rem;
      --s5: 2rem;
      --s6: 3.25rem;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: var(--sans);
      font-size: 1rem;
      line-height: 1.55;
      background: var(--paper);
      color: var(--ink);
      -webkit-text-size-adjust: 100%;
    }

    /* --- masthead ------------------------------------------------------- */
    .site-header {
      background: var(--ink);
      color: var(--paper);
      padding: var(--s3) var(--s4);
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: var(--s3);
    }
    .site-title,
    .wordmark {
      margin: 0;
      font-family: var(--serif);
      font-size: 1.35rem;
      font-weight: 700;
      letter-spacing: 0.01em;
      color: var(--paper);
      display: flex;
      align-items: center;
      gap: var(--s2);
    }
    .wordmark a {
      color: inherit;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
    }
    .wordmark svg { display: block; flex: none; }
    .live {
      font-size: 0.85rem;
      color: #cfc7b8;
      text-align: right;
      line-height: 1.35;
    }
    .live strong { color: var(--paper); font-weight: 600; }

    main {
      max-width: 60rem;
      margin: 0 auto;
      padding: var(--s5) var(--s4) var(--s6);
    }

    /* --- type ----------------------------------------------------------- */
    h1 {
      font-family: var(--serif);
      font-size: clamp(1.7rem, 4vw, 2.3rem);
      line-height: 1.15;
      margin: 0 0 var(--s3);
    }
    h2 {
      font-family: var(--serif);
      font-size: 1.3rem;
      margin: 0 0 var(--s3);
      padding-bottom: var(--s1);
      border-bottom: 2px solid var(--ink);
      display: inline-block;
    }
    h3 {
      font-size: 1rem;
      margin: var(--s4) 0 var(--s2);
    }
    p { margin: 0 0 var(--s3); }
    .lede { font-size: 1.08rem; max-width: 46rem; }
    .muted { color: var(--ink-70); }
    .small { font-size: 0.88rem; }
    a { color: var(--accent); text-underline-offset: 2px; }
    a:hover { text-decoration-thickness: 2px; }
    code, .uid {
      font-family: var(--mono);
      font-size: 0.92em;
      background: #efe8db;
      padding: 0.1em 0.35em;
      border-radius: 4px;
    }

    /* --- structure ------------------------------------------------------ */
    .block { margin: 0 0 var(--s6); }
    .block > :last-child { margin-bottom: 0; }
    .rule {
      border: 0;
      border-top: 1px solid var(--rule);
      margin: var(--s5) 0;
    }
    .row {
      display: flex;
      flex-wrap: wrap;
      gap: var(--s3);
      align-items: center;
    }

    /* --- the card tiles ------------------------------------------------- */
    /* These are the only true "cards" in the console, because they stand for
       an actual laminated card.  Hence the ink keyline, the printed color
       band and the hard offset shadow -- a thing lying on a table, not a
       floating pane of glass. */
    .catalog-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(15rem, 1fr));
      gap: var(--s4);
    }
    .title-card {
      display: flex;
      flex-direction: column;
      background: var(--card);
      border: 2px solid var(--ink);
      border-radius: 10px;
      box-shadow: 4px 4px 0 rgba(26, 26, 25, 0.14);
      overflow: hidden;
      overflow-wrap: anywhere;
    }
    .title-card .band {
      height: 0.7rem;
      background: var(--tile-ink, var(--ink));
    }
    .title-card .body {
      padding: var(--s3) var(--s3) var(--s4);
      display: flex;
      flex-direction: column;
      gap: var(--s2);
      flex: 1;
    }
    .title-card h2 {
      font-family: var(--serif);
      font-size: 1.15rem;
      line-height: 1.2;
      margin: 0;
      padding: 0;
      border: 0;
      display: block;
    }
    .title-card .meta { font-size: 0.85rem; color: var(--ink-70); margin: 0; }
    .title-card form { margin-top: auto; padding-top: var(--s2); }
    .title-card button { width: 100%; }

    .chip {
      display: inline-flex;
      align-items: center;
      gap: 0.4em;
      font-size: 0.78rem;
      font-weight: 600;
      line-height: 1;
      padding: 0.32em 0.55em;
      border: 1px solid currentColor;
      border-radius: 5px;
      white-space: nowrap;
    }
    .chip::before {
      content: "";
      width: 0.5em;
      height: 0.5em;
      border-radius: 50%;
      background: currentColor;
    }
    .chip.ready { color: var(--ok); }
    .chip.wait  { color: var(--warn); }
    .chip.plain { color: var(--ink-45); }
    .chip.plain::before { display: none; }
    .chip-row { display: flex; flex-wrap: wrap; gap: var(--s1); }

    /* --- controls ------------------------------------------------------- */
    /* One press behaviour everywhere: the shadow collapses and the control
       moves into it.  Nothing else on the page animates. */
    button, .btn {
      font: inherit;
      font-weight: 600;
      color: var(--paper);
      background: var(--ink);
      border: 2px solid var(--ink);
      border-radius: 7px;
      padding: 0.5rem 0.9rem;
      box-shadow: 3px 3px 0 rgba(26, 26, 25, 0.18);
      cursor: pointer;
      text-decoration: none;
      display: inline-block;
      transition: transform 0.06s, box-shadow 0.06s;
    }
    button:hover, .btn:hover { background: #2f2e2b; }
    button:active, .btn:active {
      transform: translate(3px, 3px);
      box-shadow: 0 0 0 rgba(26, 26, 25, 0.18);
    }
    .btn-primary {
      background: var(--accent);
      border-color: var(--accent);
    }
    .btn-primary:hover { background: #1a5290; }
    .btn-quiet {
      background: var(--card);
      color: var(--ink);
    }
    .btn-quiet:hover { background: #f2ebdd; }
    .btn-danger { background: var(--bad); border-color: var(--bad); }
    .btn-danger:hover { background: #9d2117; }
    /* Unbinding a card is reversible, so it gets a caution, not an alarm --
       but stacked on a phone it sits right under Reassign and must not read
       as the same routine action. */
    .btn-caution {
      background: var(--card);
      color: var(--bad);
      border-color: var(--bad);
    }
    .btn-caution:hover { background: #fbeceb; }
    :focus-visible {
      outline: 3px solid var(--accent);
      outline-offset: 2px;
    }

    label {
      display: inline-flex;
      flex-direction: column;
      gap: var(--s1);
      font-size: 0.88rem;
      font-weight: 600;
      color: var(--ink-70);
    }
    input, select {
      font: inherit;
      font-weight: 400;
      color: var(--ink);
      background: #fff;
      border: 2px solid var(--rule);
      border-radius: 7px;
      padding: 0.45rem 0.6rem;
      min-width: 12rem;
    }
    input:focus, select:focus { border-color: var(--ink); }
    .inline-form, .wifi-form {
      display: flex;
      flex-wrap: wrap;
      gap: var(--s3);
      align-items: flex-end;
    }
    .link-button {
      background: none;
      border: none;
      box-shadow: none;
      padding: 0;
      font: inherit;
      color: var(--accent);
      text-decoration: underline;
      cursor: pointer;
    }
    .link-button:hover { background: none; }
    .link-button:active { transform: none; }

    /* --- tables --------------------------------------------------------- */
    table { width: 100%; border-collapse: collapse; font-size: 0.94rem; }
    th {
      text-align: left;
      font-size: 0.78rem;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--ink-45);
      border-bottom: 2px solid var(--ink);
      padding: 0 var(--s2) var(--s1);
    }
    td {
      padding: var(--s3) var(--s2);
      border-bottom: 1px solid var(--rule);
      vertical-align: middle;
    }
    td:first-child, th:first-child { padding-left: 0; }
    .table-wrap { overflow-x: auto; }

    /* --- messages ------------------------------------------------------- */
    .flash {
      border: 2px solid;
      border-radius: 7px;
      padding: var(--s3);
      margin-bottom: var(--s4);
      font-weight: 500;
    }
    .flash.ok    { border-color: var(--ok);  background: #eaf4ed; color: #1d5c37; }
    .flash.error { border-color: var(--bad); background: #fbeceb; color: #8f1f15; }
    /* An instruction that is still waiting on the reader -- not a success.
       Green here would tell a parent the tap already landed. */
    .flash.wait  { border-color: var(--warn); background: #fbf1dc; color: #77510a; }

    /* --- disclosure groups ---------------------------------------------- */
    details {
      border-top: 1px solid var(--rule);
      padding: var(--s3) 0;
    }
    details:last-of-type { border-bottom: 1px solid var(--rule); }
    summary {
      cursor: pointer;
      font-weight: 600;
      list-style: none;
      display: flex;
      align-items: center;
      gap: var(--s2);
    }
    summary::-webkit-details-marker { display: none; }
    summary::before {
      content: "+";
      font-family: var(--mono);
      font-size: 1.1em;
      line-height: 1;
      width: 1.3em;
      height: 1.3em;
      display: grid;
      place-items: center;
      border: 2px solid var(--ink);
      border-radius: 5px;
      flex: none;
    }
    details[open] summary::before { content: "\\2212"; }
    details[open] > *:not(summary) { margin-top: var(--s3); }

    /* --- the child's work ----------------------------------------------- */
    /* Pictures, not filenames: a parent recognises the drawing long before
       they parse "2026-08-14-153022.png". Served full-size because the device
       has no image library to make thumbnails with, so they load lazily. */
    .work-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(11rem, 1fr));
      gap: var(--s4);
      margin-top: var(--s3);
    }
    .work-item { margin: 0; }
    .work-item img {
      display: block;
      width: 100%;
      aspect-ratio: 4 / 3;
      object-fit: contain;
      background: var(--card);
      border: 2px solid var(--ink);
      border-radius: 8px;
      box-shadow: 3px 3px 0 rgba(26, 26, 25, 0.14);
    }
    .work-item figcaption {
      font-family: var(--mono);
      font-size: 0.72rem;
      color: var(--ink-45);
      margin-top: var(--s1);
      overflow-wrap: anywhere;
    }

    /* --- file browser --------------------------------------------------- */
    .crumb { font-size: 0.88rem; color: var(--ink-70); margin-bottom: var(--s2); }
    .file-list {
      list-style: none;
      margin: 0;
      padding: 0;
      border-top: 1px solid var(--rule);
    }
    .file-list li {
      border-bottom: 1px solid var(--rule);
      padding: var(--s2) 0;
      display: flex;
      justify-content: space-between;
      gap: var(--s3);
    }
    .file-list a { font-weight: 600; text-decoration: none; }
    .file-list a:hover { text-decoration: underline; }
    .file-entry { color: var(--ink-70); }
    .file-size { flex: none; font-family: var(--mono); font-size: 0.85rem; }

    /* First-run language choice: one tap, no dropdown, no reading required. */
    .lang-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr));
      gap: var(--s3);
      margin: var(--s4) 0;
      max-width: 34rem;
    }
    .lang-choice {
      font-size: 1.15rem;
      padding: var(--s4) var(--s3);
    }

    /* --- waiting screens ------------------------------------------------ */
    /* First run and the lock screen do the same thing: stand still until a
       card shows up.  Same drawing as the kiosk, so the gesture is taught the
       same way on every surface. */
    .wait-screen {
      min-height: 60vh;
      display: grid;
      align-content: center;
      justify-items: center;
      text-align: center;
      gap: var(--s3);
    }
    .wait-screen .card-tap {
      width: min(15rem, 60vw);
      color: var(--ink);
      margin-bottom: var(--s2);
    }
    .wait-screen h1 { margin: 0; }
    .wait-screen p { margin: 0; max-width: 34rem; }
    .wait-screen .spinner { margin-top: var(--s3); }
    .wave {
      transform-box: view-box;
      transform-origin: 42px 66px;
      animation: wave 1.9s ease-out infinite;
    }
    .wave:nth-of-type(2) { animation-delay: 0.28s; }
    .wave:nth-of-type(3) { animation-delay: 0.56s; }
    @keyframes wave {
      0%   { opacity: 0; transform: scale(0.55); }
      35%  { opacity: 1; }
      100% { opacity: 0; transform: scale(1.15); }
    }

    /* --- progress ------------------------------------------------------- */
    /* A cartridge loading bar, not a spinning ring: it reads as "the machine
       is reading something" rather than "a web page is thinking". */
    .spinner {
      width: 100%;
      max-width: 12rem;
      height: 0.7rem;
      background: #e7dfd0;
      border: 2px solid var(--ink);
      border-radius: 5px;
      overflow: hidden;
      position: relative;
      margin: var(--s3) 0;
    }
    .spinner::after {
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 38%;
      background: var(--ink);
      animation: load-sweep 1.1s ease-in-out infinite alternate;
    }
    @keyframes load-sweep {
      from { transform: translateX(-8%); }
      to   { transform: translateX(180%); }
    }
    .connecting-box { padding: var(--s4) 0; }
    .connecting-box .spinner { margin-inline: 0; }

    /* --- the working-on-it bar ------------------------------------------ */
    .op-overlay {
      position: fixed;
      inset: auto 0 0 0;
      z-index: 99;
      background: var(--ink);
      color: var(--paper);
      padding: var(--s3) var(--s4);
      display: flex;
      align-items: center;
      gap: var(--s4);
    }
    .op-overlay[hidden] { display: none; }
    .op-overlay .spinner {
      width: 4.5rem;
      margin: 0;
      flex: none;
      background: #3b3934;
      border-color: var(--paper);
    }
    .op-overlay .spinner::after { background: var(--paper); }
    .op-overlay.error .spinner { display: none; }
    .op-overlay-text strong { display: block; }
    .op-overlay-text span { color: #cfc7b8; font-size: 0.9rem; }
    /* A clock, so "is this stuck?" has an answer without opening a terminal.
       Tabular figures stop the width jittering as the seconds tick. */
    .op-elapsed {
      display: block;
      font-family: var(--mono);
      font-variant-numeric: tabular-nums;
    }
    .op-overlay-dismiss {
      margin-left: auto;
      background: none;
      border: none;
      box-shadow: none;
      color: var(--paper);
      font-size: 1.4rem;
      line-height: 1;
      padding: 0 0.25rem;
      cursor: pointer;
    }
    .op-overlay-dismiss:hover { background: none; }

    @media (max-width: 640px) {
      main { padding: var(--s4) var(--s3) var(--s6); }
      /* A four-column table does not fit a phone, and a parent standing in a
         kitchen will not think to scroll a table sideways to reach Disable.
         Each card becomes its own stacked block instead. */
      .card-table,
      .card-table tbody,
      .card-table tr,
      .card-table td { display: block; width: 100%; }
      .card-table thead { display: none; }
      .card-table tr {
        border-bottom: 1px solid var(--rule);
        padding: var(--s3) 0;
      }
      .card-table td { border: 0; padding: var(--s1) 0; }
      .card-table td:first-child { font-weight: 600; }
      .live { text-align: left; }
      .inline-form, .wifi-form { flex-direction: column; align-items: stretch; }
      label, input, select, .inline-form button, .wifi-form button { width: 100%; }
    }
    @media (prefers-reduced-motion: reduce) {
      .spinner::after { animation: none; width: 100%; opacity: 0.55; }
      .wave { animation: none; opacity: 1; }
      button, .btn { transition: none; }
    }
    """).strip()

PARENT_EVENTS_SCRIPT = dedent("""
    var overlay = document.getElementById('op-overlay');
    var overlayTitle = document.getElementById('op-overlay-title');
    var overlayMsg = document.getElementById('op-overlay-msg');
    var overlayDismiss = document.getElementById('op-overlay-dismiss');
    var badge = document.getElementById('live-mode');
    var tapBanner = document.getElementById('tap-now-banner');

    var MODE_WORDS = {
      'first-run': '__T_MODE_FIRST_RUN__',
      'locked': '__T_MODE_LOCKED__',
      'unlocked': '__T_MODE_UNLOCKED__',
    };
    var enrollInProgress = false;
    var overlayPinned = false;  // true while an error is displayed; SSE won't clear it

    // Installing a big title can take many minutes with nothing to show for
    // it, which is indistinguishable from a hang. The clock keeps counting
    // across progress steps so it measures the whole wait, not the last step.
    var overlayElapsed = document.getElementById('op-overlay-elapsed');
    var startedAt = null;
    var tick = null;
    var SLOW_AFTER_SECS = 30;

    function renderElapsed() {
      if (!overlayElapsed || startedAt === null) return;
      var secs = Math.floor((Date.now() - startedAt) / 1000);
      var mins = Math.floor(secs / 60);
      var text = mins + ':' + String(secs % 60).padStart(2, '0');
      if (secs >= SLOW_AFTER_SECS) text += '  —  __T_SLOW_HINT__';
      overlayElapsed.textContent = text;
    }
    function startClock() {
      if (tick !== null) return;
      startedAt = Date.now();
      renderElapsed();
      tick = setInterval(renderElapsed, 1000);
    }
    function stopClock() {
      if (tick !== null) { clearInterval(tick); tick = null; }
      startedAt = null;
      if (overlayElapsed) overlayElapsed.textContent = '';
    }

    // The server refuses a second enrollment outright, because arming capture
    // twice corrupts the first one. Greying the buttons stops a parent
    // discovering that the hard way.
    function setEnrollButtonsBusy(busy) {
      document.querySelectorAll('.enroll-form button').forEach(function (btn) {
        if (busy) {
          if (!btn.dataset.label) btn.dataset.label = btn.textContent.trim();
          btn.disabled = true;
        } else if (btn.dataset.label) {
          btn.disabled = false;
          btn.textContent = btn.dataset.label;
          delete btn.dataset.label;
        }
      });
    }

    function showOverlay(title, msg, isError) {
      if (overlayTitle) overlayTitle.textContent = title;
      if (overlayMsg) overlayMsg.textContent = msg;
      if (overlay) {
        overlay.hidden = false;
        overlay.classList.toggle('error', !!isError);
      }
      if (overlayDismiss) overlayDismiss.hidden = !isError;
      // An error is the end of the wait, so freeze the clock rather than
      // leaving it counting under a failure message.
      if (isError) { stopClock(); } else { startClock(); }
    }
    function hideOverlay() {
      if (overlay) { overlay.hidden = true; overlay.classList.remove('error'); }
      if (overlayDismiss) overlayDismiss.hidden = true;
      stopClock();
    }

    if (overlayDismiss) {
      overlayDismiss.addEventListener('click', function() {
        overlayPinned = false;
        hideOverlay();
      });
    }

    const events = new EventSource('/events');
    events.onmessage = (event) => {
      const state = JSON.parse(event.data);
      if (badge) badge.textContent = MODE_WORDS[state.mode] || state.mode;
      if (tapBanner) {
        tapBanner.style.display =
          (state.status && state.status.capture_mode) ? '' : 'none';
      }
      if (state.operation) {
        showOverlay(
          state.operation.title || '__T_WORKING__',
          state.operation.message || ''
        );
        setEnrollButtonsBusy(true);
      } else if (!enrollInProgress && !overlayPinned) {
        hideOverlay();
        setEnrollButtonsBusy(false);
      }
    };

    document.querySelectorAll('.enroll-form').forEach(function(form) {
      form.addEventListener('submit', function(e) {
        e.preventDefault();
        var btn = form.querySelector('button[type="submit"]');
        if (btn) {
          if (!btn.dataset.label) btn.dataset.label = btn.textContent.trim();
          btn.disabled = true;
          btn.textContent = '__T_WAITING__';
        }
        enrollInProgress = true;
        overlayPinned = false;
        showOverlay('__T_ENROLLING__', '__T_ENROLLING_BODY__');
        fetch(form.action, {
          method: 'POST',
          body: '',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        })
          .then(function(r) { return r.json(); })
          .then(function(data) {
            enrollInProgress = false;
            if (data.ok) {
              window.location.replace('/');
            } else {
              overlayPinned = true;
              showOverlay(
                '__T_FAILED__',
                data.error || '__T_FAILED_BODY__',
                true
              );
              setEnrollButtonsBusy(false);
              if (btn) {
                btn.disabled = false;
                btn.textContent = '__T_TRY_AGAIN__';
              }
            }
          })
          .catch(function() {
            enrollInProgress = false;
            window.location.replace('/');
          });
      });
    });
    """).strip()

FIRST_RUN_EVENTS_SCRIPT = dedent("""
    const events = new EventSource('/events');
    events.onmessage = (event) => {
        const state = JSON.parse(event.data);
        const badge = document.getElementById('live-mode');
        const detail = document.getElementById('live-detail');
        if (badge) badge.textContent = state.mode;
        if (detail) detail.textContent = state.operation ? state.operation.message : '';
        if (state.has_admin_card) {
            events.close();
            window.location.replace('/setup');
        }
    };
    """).strip()

LOCKED_EVENTS_SCRIPT = dedent("""
    const events = new EventSource('/events');
    events.onmessage = (event) => {
        const state = JSON.parse(event.data);
        const badge = document.getElementById('live-mode');
        const detail = document.getElementById('live-detail');
        if (badge) badge.textContent = state.mode;
        if (detail) detail.textContent = state.operation ? state.operation.message : '';
        if (state.mode === 'unlocked') {
            events.close();
            window.location.reload();
        }
    };
    """).strip()

KIOSK_CSS = dedent("""
    /* Kiosk.  Read from across a room by someone who may not read yet, on a
       screen with no keyboard and no mouse.  Everything here is big, flat and
       high contrast, and the whole screen floods with the tapped card's ink --
       tap the blue card, the room goes blue.  That is the only feedback a
       pre-reader gets that the machine understood them, so it does the work
       that a paragraph of text would do for an adult. */
    :root {
      color-scheme: light;
      --paper:    #fbf7ef;
      --ink:      #1a1a19;
      --flood:    #fbf7ef;
      --on-flood: #1a1a19;
      --sans:     Piboto, system-ui, -apple-system, "Segoe UI",
                  "Noto Sans", "DejaVu Sans", sans-serif;
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; }
    body {
      margin: 0;
      font-family: var(--sans);
      background: var(--flood);
      color: var(--on-flood);
      transition: background-color 420ms ease, color 420ms ease;
      overflow: hidden;
    }
    .kiosk {
      height: 100%;
      display: grid;
      grid-template-rows: 1fr auto;
      padding: clamp(1.5rem, 4vh, 3rem) clamp(1.5rem, 5vw, 4rem);
    }
    .kiosk-stage {
      width: min(100%, 68rem);
      margin-inline: auto;
      display: grid;
      grid-template-columns: minmax(0, 20rem) minmax(0, 1fr);
      align-items: center;
      justify-content: center;
      gap: clamp(2rem, 6vw, 5rem);
      min-height: 0;
    }

    /* The card itself: ink keyline, hard offset shadow, set down slightly
       crooked the way a real one lands on a table. */
    .kiosk-card {
      position: relative;
      aspect-ratio: 3 / 4;
      width: 100%;
      max-height: 62vh;
      margin-inline: auto;
      background: #fffdf8;
      border: 4px solid var(--ink);
      border-radius: 18px;
      box-shadow: 12px 12px 0 rgba(26, 26, 25, 0.22);
      transform: rotate(-2deg);
      overflow: hidden;
      display: grid;
      place-items: center;
    }
    .kiosk-art {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }
    .kiosk-art[hidden] { display: none; }
    .kiosk-placeholder { display: block; width: 82%; }
    .kiosk-card.blank .kiosk-placeholder { width: 100%; }
    .kiosk-placeholder[hidden] { display: none; }
    .card-tap { width: 100%; height: auto; }
    /* Nothing to frame until there is art: the outline steps aside so the
       drawing is the picture rather than a picture of a picture. */
    .kiosk-card.blank {
      background: none;
      border-color: transparent;
      box-shadow: none;
      transform: none;
      aspect-ratio: auto;
    }

    /* The reader's radio waves pulse outward -- the one animation on the
       screen, and it is there to show the gesture, not to decorate. */
    .wave {
      transform-box: view-box;
      transform-origin: 42px 66px;
      animation: wave 1.9s ease-out infinite;
    }
    .wave:nth-of-type(2) { animation-delay: 0.28s; }
    .wave:nth-of-type(3) { animation-delay: 0.56s; }
    @keyframes wave {
      0%   { opacity: 0; transform: scale(0.55); }
      35%  { opacity: 1; }
      100% { opacity: 0; transform: scale(1.15); }
    }

    .kiosk-copy { min-width: 0; }
    .kiosk-title {
      /* ch resolves against this element's own size, so the measure holds at
         every step of the clamp instead of guillotining long game titles. */
      max-width: 13ch;
      margin: 0 0 clamp(0.5rem, 1.5vh, 1rem);
      font-size: clamp(2.2rem, 5.2vw, 4.6rem);
      font-weight: 800;
      line-height: 1.04;
      overflow-wrap: break-word;
      text-wrap: balance;
    }
    .kiosk-body {
      max-width: 24ch;
      margin: 0;
      font-size: clamp(1.2rem, 2.2vw, 1.85rem);
      font-weight: 500;
      line-height: 1.3;
      opacity: 0.85;
    }

    /* Loading bar, styled to match the card: same keyline, same corners. */
    .kiosk-progress {
      margin-top: clamp(1rem, 3vh, 2rem);
      height: 1.4rem;
      max-width: 20rem;
      border: 4px solid currentColor;
      border-radius: 10px;
      overflow: hidden;
      position: relative;
      visibility: hidden;
    }
    .kiosk-progress.active { visibility: visible; }
    .kiosk-progress::after {
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 34%;
      background: currentColor;
      animation: kiosk-load 1.1s ease-in-out infinite alternate;
    }
    @keyframes kiosk-load {
      from { transform: translateX(-6%); }
      to   { transform: translateX(200%); }
    }

    .kiosk-mark {
      margin: 0;
      justify-self: center;
      font-size: clamp(0.8rem, 1.4vw, 1rem);
      font-weight: 700;
      letter-spacing: 0.22em;
      text-transform: uppercase;
      opacity: 0.45;
    }

    /* Nothing to look at while a game is loading over the top of us. */
    @media (max-width: 900px), (max-height: 560px) {
      .kiosk-stage { grid-template-columns: minmax(0, 1fr); justify-items: center; }
      .kiosk-card { max-width: 15rem; max-height: 34vh; }
      .kiosk-copy { text-align: center; }
      .kiosk-title, .kiosk-body { max-width: 26ch; margin-inline: auto; }
      .kiosk-progress { margin-inline: auto; }
    }
    @media (prefers-reduced-motion: reduce) {
      body { transition: none; }
      .wave { animation: none; opacity: 1; }
      .kiosk-progress::after { animation: none; width: 100%; }
    }
    """).strip()

KIOSK_EVENTS_SCRIPT = dedent("""
    const shell = document.getElementById('kiosk');
    const title = document.getElementById('kiosk-title');
    const body = document.getElementById('kiosk-body');
    const card = document.getElementById('kiosk-card');
    const art = document.getElementById('kiosk-art');
    const placeholder = document.getElementById('kiosk-placeholder');
    const progress = document.getElementById('kiosk-spinner');
    const root = document.documentElement;
    const events = new EventSource('/events');

    const BUSY_KINDS = new Set(['loading', 'enroll']);
    const PAPER = '#fbf7ef';
    const INK = '#1a1a19';

    const applyKioskState = (kiosk) => {
        if (!kiosk) {
            return;
        }
        title.textContent = kiosk.title;
        body.textContent = kiosk.body;
        shell.dataset.kind = kiosk.kind || 'idle';

        // Flood the screen with the tapped card's ink.  Falling back to paper
        // means an unknown or idle state simply calms down instead of
        // freezing on the last game's color.
        root.style.setProperty('--flood', kiosk.ink || PAPER);
        root.style.setProperty('--on-flood', kiosk.on_ink || INK);

        if (progress) {
            progress.classList.toggle('active', BUSY_KINDS.has(kiosk.kind));
        }
        // With no art there is nothing to frame, so the card outline gets out
        // of the way and the drawing stands on its own.
        if (kiosk.art) {
            art.src = kiosk.art;
            art.alt = kiosk.title;
            art.hidden = false;
            placeholder.hidden = true;
            card.classList.remove('blank');
        } else {
            art.removeAttribute('src');
            art.alt = '';
            art.hidden = true;
            placeholder.hidden = false;
            card.classList.add('blank');
        }
    };

    events.onmessage = (event) => {
        const state = JSON.parse(event.data);
        applyKioskState(state.kiosk);
        if (state.mode === 'unlocked') {
            events.close();
            window.location.replace('/');
        }
    };

    events.onerror = () => {
        applyKioskState({
            kind: 'loading',
            title: '__T_OFFLINE_TITLE__',
            body: '__T_OFFLINE_BODY__',
        });
    };
    """).strip()


WIFI_SCAN_SCRIPT = dedent("""
    (function () {
      var sel = document.getElementById('ssid-select');
      var manRow = document.getElementById('ssid-manual-row');
      var manInput = document.getElementById('ssid-manual-input');
      if (!sel) { return; }

      sel.addEventListener('change', function () {
        if (sel.value === '__other__') {
          sel.removeAttribute('name');
          sel.removeAttribute('required');
          manRow.hidden = false;
          manInput.name = 'ssid';
          manInput.required = true;
        } else {
          sel.name = 'ssid';
          sel.required = true;
          manRow.hidden = true;
          manInput.removeAttribute('name');
          manInput.required = false;
        }
      });

      function populate(ssids) {
        sel.innerHTML = '';
        ssids.forEach(function (ssid) {
          var o = document.createElement('option');
          o.value = ssid;
          o.textContent = ssid;
          sel.appendChild(o);
        });
        var other = document.createElement('option');
        other.value = '__other__';
        other.textContent = ssids.length
          ? 'Other…' : 'No networks found — enter manually';
        sel.appendChild(other);
        if (ssids.length === 0) {
          sel.value = '__other__';
          sel.dispatchEvent(new Event('change'));
        }
      }

      fetch('/wifi/scan')
        .then(function (r) { return r.json(); })
        .then(populate)
        .catch(function () { populate([]); });
    })();
    """).strip()

WIFI_CONNECT_SCRIPT = dedent("""
    (function () {
      var form = document.getElementById('wifi-form');
      var connectingBox = document.getElementById('wifi-connecting');
      var connectMsg = document.getElementById('wifi-connect-msg');
      var errorBox = document.getElementById('wifi-connect-error');
      if (!form) { return; }

      form.addEventListener('submit', function (e) {
        e.preventDefault();
        form.hidden = true;
        connectingBox.hidden = false;
        errorBox.hidden = true;

        var body = new URLSearchParams(new FormData(form)).toString();
        fetch('/setup/wifi', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: body,
        })
          .then(function (r) { return r.json(); })
          .then(function (data) {
            if (data.ok) {
              connectMsg.textContent = 'Connected!';
              setTimeout(function () { window.location.replace('/'); }, 2500);
            } else {
              connectingBox.hidden = true;
              form.hidden = false;
              errorBox.textContent = data.error || 'Connection failed.';
              errorBox.hidden = false;
            }
          })
          .catch(function () {
            connectingBox.hidden = true;
            form.hidden = false;
            errorBox.textContent = 'Could not reach the server. Please try again.';
            errorBox.hidden = false;
          });
      });
    })();
    """).strip()

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
NetworkChecker = Callable[[], bool]


class ControlApiError(RuntimeError):
    """Raised when the launcher control API cannot satisfy a request."""


@dataclass(frozen=True)
class ControlClient:
    """Small JSON client for the launcher control API."""

    base_url: str
    timeout_secs: float = 5.0

    def status(self) -> dict[str, object]:
        return self._request_json("GET", "/status")

    def reload(self) -> dict[str, object]:
        return self._request_json("POST", "/reload")

    def lock(self) -> dict[str, object]:
        return self._request_json("POST", "/lock")

    def unlock(self) -> dict[str, object]:
        return self._request_json("POST", "/unlock")

    def capture(self) -> str:
        # Use a generous timeout: the launcher holds the connection open until a
        # card is tapped (up to DEFAULT_CAPTURE_TIMEOUT_SECS = 30 s), so the
        # HTTP client must wait at least that long before giving up.
        try:
            payload = self._request_json("POST", "/capture", timeout=35.0)
        except ControlApiError as exc:
            if "no card within timeout" in str(exc):
                raise ControlApiError(
                    "No card detected — hold the card close to the reader and try again"
                ) from exc
            raise
        uid = payload.get("uid")
        if not isinstance(uid, str) or not uid:
            raise ControlApiError("daemon capture returned an invalid uid")
        return uid

    def _request_json(
        self,
        method: str,
        path: str,
        timeout: float | None = None,
    ) -> dict[str, object]:
        request = Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=b"" if method == "POST" else None,
            method=method,
        )
        try:
            with urlopen(request, timeout=timeout or self.timeout_secs) as response:
                body = response.read().decode("utf-8")
                payload = json.loads(body)
        except HTTPError as exc:
            try:
                error_body = exc.read().decode("utf-8")
                message = json.loads(error_body).get("error", exc.reason)
            except (OSError, json.JSONDecodeError, AttributeError):
                message = exc.reason
            raise ControlApiError(f"daemon request failed: {message}") from exc
        except (OSError, URLError, json.JSONDecodeError) as exc:
            raise ControlApiError(f"daemon request failed: {exc}") from exc

        if not isinstance(payload, dict):
            raise ControlApiError("daemon returned a non-object JSON payload")
        return payload


@dataclass
class WebApp:
    """Thin web layer over the catalog, cards file, and launcher control API."""

    catalog_path: Path
    cards_path: Path
    control: ControlClient
    runner: CommandRunner = subprocess.run
    network_checker: NetworkChecker | None = None
    scummvm_executable: str = "scummvm"
    event_poll_secs: float = DEFAULT_EVENT_POLL_SECS
    user_catalog_path: Path | None = None
    # Where the language choice is stored and where locale files are looked
    # for.  Both overridable so tests and --locales-dir do not touch /var.
    language_path: Path | None = None
    locale_dirs: tuple[Path, ...] | None = None
    # Where the titles' user_dirs live.  Overridable so tests never touch a
    # real home directory.
    home_path: Path | None = None
    state_path: Path | None = None
    _mutation_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    # Held for a whole enrollment, capture included.  See
    # _one_enrollment_at_a_time for why this cannot be a blocking wait.
    _enroll_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    _readiness_cache: dict[tuple[str, ...], bool] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _operation_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    _operation: dict[str, str] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _export_jobs: dict = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _export_jobs_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    _copy_jobs: dict = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _copy_jobs_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )

    def render_index(
        self,
        *,
        message: str | None = None,
        error: str | None = None,
    ) -> str:
        catalog = self._load_catalog()
        cards = load_cards(self.cards_path)
        try:
            status = self.control.status()
        except ControlApiError:
            status = {}
        mode = self._mode(cards, status)

        if mode == "first-run":
            body = self._render_first_run(message=message, error=error)
            return self._layout(
                "ChipBit Parent Console",
                body,
                include_events=True,
                script=FIRST_RUN_EVENTS_SCRIPT,
            )

        if mode == "locked":
            body = self._render_locked(status=status, message=message, error=error)
            return self._layout(
                "ChipBit Parent Console",
                body,
                include_events=True,
                script=LOCKED_EVENTS_SCRIPT,
            )

        body = self._render_unlocked(
            catalog=catalog,
            cards=cards,
            message=message,
            error=error,
        )
        return self._layout("ChipBit Parent Console", body, include_events=True)

    def render_kiosk(self) -> str:
        body = dedent(f"""
            <main class="kiosk" id="kiosk" data-kind="idle">
              <div class="kiosk-stage">
                <div class="kiosk-card blank" id="kiosk-card">
                  <img class="kiosk-art" id="kiosk-art" alt="" hidden
                       onerror="this.src='/art/default';this.onerror=null;" />
                  <span class="kiosk-placeholder" id="kiosk-placeholder">
                    {CARD_TAP_SVG}
                  </span>
                </div>
                <div class="kiosk-copy">
                  <h1 class="kiosk-title" id="kiosk-title">
                    {t('kiosk.idle.title')}
                  </h1>
                  <p class="kiosk-body" id="kiosk-body">
                    {t('kiosk.idle.body')}
                  </p>
                  <div class="kiosk-progress" id="kiosk-spinner"></div>
                </div>
              </div>
              <p class="kiosk-mark">ChipBit</p>
            </main>
            """).strip()
        return self._kiosk_layout(body)

    def _kiosk_layout(self, body: str) -> str:
        kiosk_script = (
            KIOSK_EVENTS_SCRIPT
            .replace("__T_OFFLINE_TITLE__", t("kiosk.offline.title"))
            .replace("__T_OFFLINE_BODY__", t("kiosk.offline.body"))
        )
        return dedent(f"""<!doctype html>
            <html lang="{language_tag()}">
            <head>
              <meta charset="utf-8" />
              <meta name="viewport" content="width=device-width, initial-scale=1" />
              <title>ChipBit</title>
              <style>
            {KIOSK_CSS}
              </style>
            </head>
            <body>
              {body}
              <script>
            {kiosk_script}
              </script>
            </body>
            </html>
            """).strip()

    def event_payload(self) -> dict[str, object]:
        cards = load_cards(self.cards_path)
        has_admin_card = "unlock" in cards.system_cards
        try:
            status = self.control.status()
        except ControlApiError:
            status = {}
        operation = self._operation_snapshot()
        return {
            "mode": self._mode(cards, status),
            "status": status,
            "operation": operation,
            "kiosk": self._kiosk_state(cards, status, operation),
            "has_admin_card": has_admin_card,
            "title_cards": len(cards.title_cards),
            "system_cards": len(cards.system_cards),
        }

    @contextmanager
    def _one_enrollment_at_a_time(self):
        """Refuse a second enrollment while one is already in flight.

        control.capture() is a single slot on the daemon: arming it again sets
        _capture_uid back to None and clears the event the first caller is
        waiting on.  One card tap then wakes *both* callers with the *same*
        uid, they queue on _mutation_lock, and the card ends up bound to
        whichever install finishes last -- after running two installs.  The
        parent thinks they enrolled two cards and actually enrolled none of
        what they meant.

        Refusing rather than queueing on purpose: an install can run for
        minutes, and silently parking a parent behind it looks identical to
        the hang we just spent days removing.
        """
        if not self._enroll_lock.acquire(blocking=False):
            raise RuntimeError(t("msg.enroll_busy"))
        try:
            yield
        finally:
            self._enroll_lock.release()

    def enroll_admin(self) -> str:
        with self._one_enrollment_at_a_time(), self._mutation_lock:
            cards = load_cards(self.cards_path)
            if "unlock" in cards.system_cards:
                raise ValueError("admin card is already enrolled")

            uid = normalize_uid(self.control.capture())
            system_cards = dict(cards.system_cards)
            system_cards["unlock"] = SystemCard(action="unlock", uid=uid)
            save_cards(
                self.cards_path,
                CardsConfig(
                    title_cards=dict(cards.title_cards),
                    system_cards=system_cards,
                ),
            )
        self.control.reload()
        return f"Admin card enrolled as {uid}"

    def enroll_title(self, title_id: str) -> str:
        cards = load_cards(self.cards_path)
        self._require_unlocked(cards)
        # The guard has to wrap capture too, not just the install: arming a
        # second capture is what corrupts the first one's result.
        with self._one_enrollment_at_a_time():
            uid = self.control.capture()
            return self.enroll_title_for_uid(uid, title_id)

    def reassign_card(self, uid: str, title_id: str) -> str:
        normalized_uid = normalize_uid(uid)
        if not normalized_uid:
            raise ValueError("uid is required")
        cards = load_cards(self.cards_path)
        self._require_unlocked(cards)
        return self.enroll_title_for_uid(normalized_uid, title_id)

    def enroll_title_for_uid(self, uid: str, title_id: str) -> str:
        with self._mutation_lock:
            catalog = self._load_catalog()

            title = catalog.titles.get(title_id)
            if title is None:
                raise ValueError(f"unknown title: {title_id}")

            progress: list[InstallProgress] = []
            self._set_operation_state(
                title=title.label,
                message=f"Preparing {title.label}",
                art=title.art,
                title_id=title.id,
            )
            try:
                for event in enroll_card(
                    uid,
                    title,
                    cards_path=self.cards_path,
                    games_root=catalog.settings.games_root,
                    runner=self.runner,
                    network_checker=self.network_checker,
                    scummvm_executable=self.scummvm_executable,
                ):
                    progress.append(event)
                    self._set_operation_state(
                        title=title.label,
                        message=event.message,
                        art=title.art,
                        title_id=title.id,
                    )
            finally:
                self._clear_operation_state()

        self._clear_readiness_cache()
        self.control.reload()
        if progress:
            return progress[-1].message
        normalized_uid = normalize_uid(uid)
        return t("msg.bound", uid=normalized_uid, title=title.id)

    def remove_card(self, uid: str) -> str:
        with self._mutation_lock:
            cards = load_cards(self.cards_path)
            self._require_unlocked(cards)

            normalized_uid = normalize_uid(uid)
            title_cards = dict(cards.title_cards)
            removed = title_cards.pop(normalized_uid, None)
            if removed is None:
                raise ValueError(f"no enrolled card for {normalized_uid}")

            save_cards(
                self.cards_path,
                CardsConfig(
                    title_cards=title_cards,
                    system_cards=dict(cards.system_cards),
                ),
            )

        self._clear_readiness_cache()
        self.control.reload()
        self.control.lock()
        return f"Removed card {normalized_uid} from {removed.title_id}"

    def reload_daemon(self) -> str:
        cards = load_cards(self.cards_path)
        self._require_unlocked(cards)
        self._clear_readiness_cache()
        result = self.control.reload()
        self.control.lock()
        if result.get("reloaded"):
            return "Reloaded daemon config"
        return "No config changes detected"

    def is_first_run(self) -> bool:
        """No admin card yet -- a genuinely fresh device."""
        try:
            return "unlock" not in load_cards(self.cards_path).system_cards
        except ConfigLoadError:
            return True

    def needs_language_choice(self) -> bool:
        """Ask on first run only, and only when there is a real choice."""
        if language_is_set(self.language_path):
            return False
        return len(available_languages(self.locale_dirs)) > 1

    def set_language(self, code: str) -> str:
        """Persist the parent's language choice and apply it immediately.

        The web UI switches on the next page render; launched titles pick it
        up on their next launch, because the launcher reads the same file.
        """
        codes = {choice.code for choice in available_languages(self.locale_dirs)}
        if code not in codes:
            raise ValueError(f"unknown language: {code}")
        write_language(code, self.language_path)
        if code == "en":
            use_english()
        else:
            load_locale(code, self.locale_dirs)
            self._ensure_locale_generated(code)
        return t("msg.language_set")

    def _ensure_locale_generated(self, code: str) -> None:
        """Generate the POSIX locale so Qt titles can use it.

        Best effort on purpose.  If this fails -- no script, no sudo rule, a
        dev box -- the launcher still exports LANGUAGE, so gettext titles like
        TuxPaint translate and only Qt ones (GCompris, KStars) stay English.
        A partly-translated device beats a failed settings save.
        """
        posix_locale = LANGUAGES.get(code, (None, None))[1]
        if not posix_locale:
            return
        try:
            result = self.runner(
                ["sudo", "/usr/share/chipbit/apply_locale.sh", posix_locale],
                check=False, capture_output=True, text=True,
            )
        except OSError as exc:
            log.warning("could not generate locale %s: %s", posix_locale, exc)
            return
        if getattr(result, "returncode", 1) != 0:
            log.warning(
                "locale %s not generated: %s",
                posix_locale,
                (getattr(result, "stderr", "") or "").strip(),
            )

    def set_keyboard_layout(self, layout: str) -> str:
        _VALID_LAYOUTS = {"us", "gb", "de", "fr", "es", "it", "pt", "nl"}
        if layout not in _VALID_LAYOUTS:
            raise ValueError(f"unsupported keyboard layout: {layout!r}")
        cards = load_cards(self.cards_path)
        self._require_unlocked(cards)
        result = self.runner(
            ["sudo", "localectl", "set-x11-keymap", layout],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            msg = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise RuntimeError(f"keyboard layout change failed: {msg}")
        self.control.lock()
        return (
            f"Keyboard layout set to {layout}"
            " — takes effect after the screen restarts"
        )

    def lock_controls(self) -> str:
        self.control.lock()
        return "Parent controls locked"

    def shutdown_system(self) -> str:
        threading.Thread(
            target=lambda: self.runner(
                ["sudo", "shutdown", "-h", "now"],
                check=False, capture_output=True, text=True,
            ),
            daemon=True,
        ).start()
        return "Shutting down — you can safely unplug the Pi in a moment"

    def configure_wifi(self, ssid: str, password: str | None) -> str:
        normalized_ssid = ssid.strip()
        if not normalized_ssid:
            raise ValueError("ssid is required")

        argv = ["sudo", "nmcli", "device", "wifi", "connect", normalized_ssid]
        if password:
            argv.extend(["password", password])
        result = self.runner(argv, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise RuntimeError(f"Wi-Fi setup failed: {message}")
        return f"Connected Wi-Fi to {normalized_ssid}"

    def scan_wifi(self) -> list[str]:
        """Return nearby SSIDs in signal-strength order (strongest first)."""
        try:
            result = self.runner(
                [
                    "nmcli", "-f", "SSID", "-t", "-e", "no",
                    "device", "wifi", "list", "--rescan", "auto",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            return []
        seen: set[str] = set()
        ssids: list[str] = []
        for line in result.stdout.splitlines():
            ssid = line.strip()
            if ssid and ssid not in seen:
                seen.add(ssid)
                ssids.append(ssid)
        return ssids

    def render_language_picker(self, *, next_path: str = "/") -> str:
        """First-run language choice, shown before anything else.

        This runs ahead of the Wi-Fi country picker on purpose: that screen is
        English prose asking for a regulatory decision, which is a poor first
        thing to hand a parent who does not read English.  Choose here and
        every screen after it is already translated.

        Deliberately wordless.  The heading is the word "Language" in each
        installed language and the choices are endonyms, so nobody has to read
        a sentence in a language they do not speak to get out of this screen.
        """
        choices = available_languages(self.locale_dirs)
        words: list[str] = []
        for choice in choices:
            word = (
                t("console.settings.language") if choice.code == "en"
                else peek(
                    choice.code, "console.settings.language", self.locale_dirs
                )
            )
            if word and word not in words:
                words.append(word)
        heading = escape(" · ".join(words))
        buttons = "".join(
            f'<button type="submit" name="language" value="{escape(choice.code)}"'
            f' class="btn-quiet lang-choice">{escape(choice.name)}</button>'
            for choice in choices
        )
        return dedent(f"""
            <!doctype html>
            <html lang="{language_tag()}">
            <head>
              <meta charset="utf-8">
              <meta name="viewport" content="width=device-width,initial-scale=1">
              <title>ChipBit</title>
              <style>{PAGE_CSS}</style>
            </head>
            <body>
              <header class="site-header">
                <p class="site-title">{CHIPBIT_MARK}ChipBit</p>
              </header>
              <main>
                <section class="block">
                  <h1>{heading}</h1>
                  <form method="post" action="/setup/language" class="lang-grid">
                    <input type="hidden" name="next" value="{escape(next_path)}" />
                    {buttons}
                  </form>
                  <p class="muted small">{t('setup.language.note')}</p>
                </section>
              </main>
            </body>
            </html>
        """).strip()

    def render_country_picker(self, *, error: str = "") -> str:
        """First-run country selection page — shown before WiFi setup."""
        flash = self._flash("", error)
        options = "\n".join(
            f'<option value="{c}">{escape(name)}</option>'
            for c, name in _WIFI_COUNTRIES
        )
        return dedent(f"""
            <!doctype html>
            <html lang="{language_tag()}">
            <head>
              <meta charset="utf-8">
              <meta name="viewport" content="width=device-width,initial-scale=1">
              <title>ChipBit Setup</title>
              <style>{PAGE_CSS}</style>
            </head>
            <body>
              <header class="site-header">
                <p class="site-title">{CHIPBIT_MARK}{t('setup.title')}</p>
              </header>
              <main>
                {flash}
                <section class="block">
                  <h1>{t('setup.country.heading')}</h1>
                  <p>{t('setup.country.body')}</p>
                  <form method="post" action="/setup/country">
                    <label>{t('setup.country.label')}
                      <select name="country" required>
                        <option value="" disabled selected>
                          {t('setup.country.placeholder')}
                        </option>
                        {options}
                      </select>
                    </label>
                    <button type="submit" class="btn-primary">
                      {t('setup.country.submit')}
                    </button>
                  </form>
                </section>
              </main>
            </body>
            </html>
        """).strip()

    def render_rebooting(self) -> str:
        """Shown immediately after country selection while the device reboots."""
        return dedent("""
            <!doctype html>
            <html lang="{language_tag()}">
            <head>
              <meta charset="utf-8">
              <meta name="viewport" content="width=device-width,initial-scale=1">
              <meta http-equiv="refresh" content="20;url=/setup">
              <title>ChipBit — Rebooting</title>
              <style>__PAGE_CSS__</style>
            </head>
            <body>
              <header class="site-header">
                <p class="site-title">__CHIPBIT_MARK____T_SETUP_TITLE__</p>
              </header>
              <main>
                <section class="block">
                  <h1>__T_REBOOT_HEADING__</h1>
                  <p>__T_REBOOT_BODY__</p>
                  <div class="connecting-box">
                    <div class="spinner"></div>
                  </div>
                </section>
              </main>
            </body>
            </html>
        """.replace("__PAGE_CSS__", PAGE_CSS)
           .replace("__CHIPBIT_MARK__", CHIPBIT_MARK)
           .replace("__T_SETUP_TITLE__", t("setup.title"))
           .replace("__T_REBOOT_HEADING__", t("setup.reboot.heading"))
           .replace("__T_REBOOT_BODY__", t("setup.reboot.body"))).strip()

    def apply_wifi_country(self, country: str) -> None:
        """Save the country code and immediately apply regulatory settings."""
        country = country.strip().upper()
        if country not in _VALID_COUNTRY_CODES:
            raise ValueError(f"Unknown country code: {country!r}")
        _WIFI_COUNTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _WIFI_COUNTRY_FILE.write_text(country + "\n")
        result = self.runner(
            ["sudo", "/usr/share/chipbit/apply_wifi_country.sh"],
            check=False, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"apply_wifi_country.sh failed: {result.stderr.strip()}"
            )

    def render_setup(self, *, message: str = "", error: str = "") -> str:
        """First-run WiFi setup page — shown once after admin card enrollment."""
        flash = self._flash(message, error)
        return dedent(f"""
            <!doctype html>
            <html lang="{language_tag()}">
            <head>
              <meta charset="utf-8">
              <meta name="viewport" content="width=device-width,initial-scale=1">
              <title>ChipBit Setup</title>
              <style>{PAGE_CSS}</style>
            </head>
            <body>
              <header class="site-header">
                <p class="site-title">{CHIPBIT_MARK}{t('setup.title')}</p>
              </header>
              <main>
                {flash}
                <section class="block">
                  <h1>{t('setup.wifi.heading')}</h1>
                  <p>{t('setup.wifi.body')}</p>
                  <p id="wifi-connect-error" class="flash error" hidden></p>
                  <form id="wifi-form" method="post"
                    action="/setup/wifi" class="wifi-form">
                    <label>{t('common.network')}
                      <select id="ssid-select" name="ssid" required>
                        <option value="" disabled selected>
                          {t('common.scanning')}
                        </option>
                      </select>
                    </label>
                    <div id="ssid-manual-row" hidden>
                      <label>{t('common.network_name')}
                        <input type="text" id="ssid-manual-input" autocomplete="off" />
                      </label>
                    </div>
                    <label>{t('common.password')}
                      <input type="password" name="password" />
                    </label>
                    <button type="submit" class="btn-primary">
                      {t('setup.wifi.submit')}
                    </button>
                  </form>
                  <div id="wifi-connecting" class="connecting-box" hidden>
                    <div class="spinner"></div>
                    <p id="wifi-connect-msg">{t('setup.wifi.connecting')}</p>
                  </div>
                  <script>{WIFI_SCAN_SCRIPT}</script>
                  <script>{WIFI_CONNECT_SCRIPT}</script>
                  <p class="muted">
                    <a href="/setup/skip">{t('setup.wifi.skip')}</a>
                    &nbsp;·&nbsp;
                    <a href="/debug">{t('common.diagnostics')}</a>
                  </p>
                </section>
              </main>
            </body>
            </html>
        """).strip()

    def wifi_diagnostics(self, *, message: str = "") -> str:
        """Run a set of read-only diagnostic commands and return their output."""
        cmds = [
            ("Launcher log", [
                "sudo", "journalctl", "-u", "chipbit-launcher",
                "--no-pager", "-n", "60", "--output=short-monotonic",
            ]),
            # Input diagnostics.  The launcher takes an *exclusive* grab
            # (EVIOCGRAB) on whatever find_rfid_reader() picks, so a
            # misidentified device disappears from the kiosk entirely --
            # which looks to a parent like dead keyboard or mouse, with
            # nothing on screen to explain it.  "reader open:" in the
            # launcher log above names the device that was grabbed; these
            # two say what else exists and what the compositor can see.
            ("Input devices", ["cat", "/proc/bus/input/devices"]),
            ("Seat devices (what the kiosk can open)", [
                "loginctl", "seat-status", "seat0", "--no-pager",
            ]),
            ("Kiosk log", [
                "sudo", "journalctl", "-u", "chipbit-kiosk",
                "--no-pager", "-n", "40", "--output=short-monotonic",
            ]),
            ("Disk space", ["df", "-h", "/"]),
            ("Root filesystem expand log", [
                "sudo", "journalctl", "-u", "chipbit-expand-rootfs",
                "--no-pager", "--output=short-monotonic",
            ]),
            ("WiFi radio", ["nmcli", "radio", "wifi"]),
            ("Network devices", ["nmcli", "device", "status"]),
            ("Nearby networks", [
                "nmcli", "-f", "SSID,SIGNAL,SECURITY", "-e", "no",
                "device", "wifi", "list", "--rescan", "no",
            ]),
        ]
        sections: list[str] = []
        for label, argv in cmds:
            try:
                r = self.runner(argv, check=False, capture_output=True, text=True)
                out = (r.stdout + r.stderr).strip() or "(no output)"
            except OSError as exc:
                out = f"(command not found: {exc})"
            sections.append(f"<h3>{escape(label)}</h3><pre>{escape(out)}</pre>")
        body = "\n".join(sections)
        flash = f'<p style="color:green">{escape(message)}</p>' if message else ""
        return f"""<!doctype html><html lang="{language_tag()}"><head>
<meta charset="utf-8"><title>ChipBit diagnostics</title>
<style>body{{font-family:monospace;padding:1rem}}pre{{background:#f4f4f4;padding:.5rem;white-space:pre-wrap}}</style>
</head><body><h1>ChipBit diagnostics</h1>{flash}
<form method="post" action="/debug/wifi-enable" style="margin-bottom:1rem">
  <button type="submit">Unblock radio + enable WiFi</button>
  <span style="margin-left:.5rem;font-size:.9em">
    (runs rfkill unblock wifi &amp;&amp; nmcli radio wifi on)
  </span>
</form>
{body}
<p><a href="/">&#8592; Back to parent console</a></p>
</body></html>"""

    def wifi_enable(self) -> str:
        """Unblock the radio and tell NetworkManager to turn WiFi on."""
        self.runner(
            ["sudo", "rfkill", "unblock", "wifi"],
            check=False, capture_output=True, text=True,
        )
        result = self.runner(
            ["sudo", "nmcli", "radio", "wifi", "on"],
            check=False, capture_output=True, text=True,
        )
        if result.returncode != 0:
            err = (result.stderr + result.stdout).strip()
            raise RuntimeError(f"nmcli radio wifi on failed: {err}")
        return "WiFi radio enabled — try scanning now"

    def create_custom_title(self, form: dict[str, str]) -> str:
        import re
        from urllib.parse import urlparse as _urlparse

        cards = load_cards(self.cards_path)
        self._require_unlocked(cards)

        if self.user_catalog_path is None:
            raise RuntimeError("no user catalog configured; cannot save custom titles")

        title_type = form.get("type", "").strip()
        label = form.get("label", "").strip()
        if not label:
            raise ValueError("title name is required")
        if title_type not in {"web", "exec", "scummvm", "dosbox", "ruffle"}:
            raise ValueError(f"invalid type: {title_type!r}")

        title_id = self._unique_title_id(
            re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:28] or "custom"
        )

        if title_type == "web":
            url = form.get("url", "").strip()
            if not url:
                raise ValueError("URL is required")
            host = _urlparse(url).hostname or ""
            if not host:
                raise ValueError("URL must include a hostname")
            title = CatalogTitle(
                id=title_id, label=label, type="web",
                bundled=False, url=url, allowlist=(host,),
            )
        elif title_type == "exec":
            cmd_str = form.get("cmd", "").strip()
            if not cmd_str:
                raise ValueError("launch command is required")
            apt_pkg = form.get("apt", "").strip()
            title = CatalogTitle(
                id=title_id, label=label, type="exec",
                bundled=False, cmd=tuple(cmd_str.split()),
                install={"apt": (apt_pkg,)} if apt_pkg else {},
            )
        elif title_type == "scummvm":
            game_id = form.get("game_id", "").strip()
            if not game_id:
                raise ValueError("ScummVM game ID is required")
            data_dir = form.get("data_dir", "").strip() or None
            title = CatalogTitle(
                id=title_id, label=label, type="scummvm",
                bundled=False, game_id=game_id,
                data_dir=data_dir,
                install={"apt": ("scummvm",)},
            )
        elif title_type == "dosbox":
            conf = form.get("conf", "").strip()
            if not conf:
                raise ValueError("config file path is required")
            title = CatalogTitle(
                id=title_id, label=label, type="dosbox",
                bundled=False, data="required", conf=conf,
            )
        else:  # ruffle
            swf = form.get("swf", "").strip()
            if not swf:
                raise ValueError("SWF file path is required")
            title = CatalogTitle(
                id=title_id, label=label, type="ruffle",
                bundled=False, data="required", swf=swf,
            )

        save_user_title(self.user_catalog_path, title)
        self._clear_readiness_cache()
        self.control.reload()
        return t("msg.added", label=label)

    def _unique_title_id(self, slug: str) -> str:
        catalog = self._load_catalog()
        candidate = f"user-{slug}"
        if candidate not in catalog.titles:
            return candidate
        for n in range(2, 100):
            candidate = f"user-{slug}-{n}"
            if candidate not in catalog.titles:
                return candidate
        import uuid
        return f"user-{uuid.uuid4().hex[:8]}"

    def render_files(
        self,
        *,
        message: str | None = None,
        error: str | None = None,
    ) -> str:
        for dev, _ in self._detect_unmounted_devices():
            try:
                self.mount_device(dev)
            except Exception:
                pass
        drives = self._detect_drives()
        unmounted = self._detect_unmounted_devices()

        items: list[str] = []
        for d in drives:
            items.append(
                f'<li><a href="/files/browse?p={quote(str(d))}">'
                f"{escape(d.name)}</a></li>"
            )
        for dev, label in unmounted:
            items.append(
                f'<li><form method="post" action="/files/mount" style="display:inline">'
                f'<input type="hidden" name="device" value="{escape(dev)}" />'
                '<button type="submit">'
                f'{escape(t("files.mount", label=label))}</button>'
                f"</form></li>"
            )
        if not items:
            items.append(
                f"<li>{escape(t('files.none'))}</li>"
            )
        drive_list = "\n".join(items)

        section = dedent(f"""
            <section class="block">
              <p class="crumb"><a href="/">{t('files.crumb_back')}</a></p>
              <h1>{t('files.heading')}</h1>
              <p class="lede">{t('files.lede')}</p>
            </section>
            <section class="block">
              <h2>{t('files.drives')}</h2>
              <ul class="file-list">
                {drive_list}
              </ul>
              <p class="small"><a href="/files">{t('files.rescan')}</a></p>
            </section>
            """).strip()

        return self._layout(
            "Game files — ChipBit",
            self._flash(message, error) + section,
            include_events=False,
        )

    def render_file_browse(
        self,
        path_str: str,
        *,
        message: str | None = None,
        error: str | None = None,
    ) -> str:
        p = Path(path_str)
        if ".." in p.parts or not str(p).startswith(str(_MEDIA_ROOT) + "/"):
            raise ValueError("path must be on a mounted drive under /media/")
        if not p.is_dir():
            raise ValueError(f"not a directory: {p} (drive may have been ejected)")
        try:
            children = sorted(
                p.iterdir(), key=lambda c: (c.is_file(), c.name.lower())
            )
        except PermissionError as exc:
            raise ValueError(str(exc)) from exc

        # Breadcrumb: Drives / chipbit / BluesYellow / ...
        crumb_parts: list[str] = [f'<a href="/files">{t("files.drives")}</a>']
        built = Path("/")
        for part in p.parts[1:]:  # skip root '/'
            built = built / part
            if str(built) == str(_MEDIA_ROOT):
                crumb_parts.append(escape(part))
            elif str(built).startswith(str(_MEDIA_ROOT) + "/"):
                crumb_parts.append(
                    f'<a href="/files/browse?p={quote(str(built))}">{escape(part)}</a>'
                )
        breadcrumb = " / ".join(crumb_parts)

        # Up link
        parent = p.parent
        if str(parent).startswith(str(_MEDIA_ROOT) + "/"):
            up_link = (
                f'<p><a href="/files/browse?p={quote(str(parent))}">'
                f'{t("files.up")}</a></p>'
            )
        else:
            up_link = f'<p><a href="/files">{t("files.up_drives")}</a></p>'

        # Copy-this-folder form (copies the current directory)
        suggested = p.name.lower().replace(" ", "-")
        copy_form = dedent(f"""
            <section class="block">
              <h2>{t('files.copy_heading')}</h2>
              <form method="post" action="/files/copy" class="wifi-form">
                <input type="hidden" name="source" value="{escape(str(p))}" />
                <input type="hidden" name="back" value="{escape(str(p))}" />
                <label>{t('files.copy_type')}
                  <select id="copy-type">
                    <option value="scummvm">{t('files.copy_type.scummvm')}</option>
                    <option value="dosbox">{t('files.copy_type.dosbox')}</option>
                    <option value="flash">{t('files.copy_type.flash')}</option>
                    <option value="">{t('files.copy_type.other')}</option>
                  </select>
                </label>
                <label>{t('files.copy_dest')}
                  <input type="text" name="dest" id="copy-dest"
                         value="scummvm/{escape(suggested)}"
                         placeholder="scummvm/monkey" required />
                </label>
                <button type="submit">{t('files.copy_button')}</button>
              </form>
              <script>
              (function() {{
                var sel = document.getElementById('copy-type');
                var dest = document.getElementById('copy-dest');
                sel.addEventListener('change', function() {{
                  var slash = dest.value.indexOf('/');
                  var name = slash >= 0 ? dest.value.slice(slash + 1) : dest.value;
                  dest.value = sel.value ? sel.value + '/' + name : name;
                }});
              }})();
              </script>
            </section>
            """).strip()

        # File listing
        rows: list[str] = []
        for child in children:
            if child.name.startswith("."):
                continue
            try:
                if child.is_dir():
                    rows.append(
                        f'<li><a href="/files/browse?p={quote(str(child))}">'
                        f"[+] {escape(child.name)}</a></li>"
                    )
                else:
                    kb = child.stat().st_size // 1024
                    rows.append(
                        f'<li class="file-entry">'
                        f"{escape(child.name)}"
                        f'<span class="file-size muted"> {kb} KB</span></li>'
                    )
            except OSError:
                continue

        empty = f"<li><em>{escape(t('files.empty'))}</em></li>"
        listing = "\n".join(rows) if rows else empty

        section = dedent(f"""
            <section class="block">
              <p class="crumb">{breadcrumb}</p>
              <h1>{escape(p.name)}</h1>
            </section>
            {copy_form}
            <section class="block">
              <h2>{t('files.contents')}</h2>
              {up_link}
              <ul class="file-list">
                {listing}
              </ul>
            </section>
            """).strip()

        return self._layout(
            f"{escape(p.name)} — ChipBit",
            self._flash(message, error) + section,
            include_events=False,
        )

    def start_copy_job(
        self, *, source: str, dest: str, games_root: Path, back: str
    ) -> str:
        import uuid
        job_id = uuid.uuid4().hex[:12]
        with self._copy_jobs_lock:
            self._copy_jobs[job_id] = {
                "done": False, "error": None, "prefill": {}, "back": back,
            }
        t = threading.Thread(
            target=self._run_copy_job,
            args=(job_id, source, dest, games_root),
            daemon=True,
        )
        t.start()
        return job_id

    def _run_copy_job(
        self, job_id: str, source: str, dest: str, games_root: Path
    ) -> None:
        try:
            self.copy_game_files(source, dest, games_root)
            pf = self._guess_prefill(dest, games_root)
            with self._copy_jobs_lock:
                self._copy_jobs[job_id].update({"done": True, "prefill": pf})
        except Exception as exc:
            with self._copy_jobs_lock:
                self._copy_jobs[job_id].update({"done": True, "error": str(exc)})

    def render_copy_status(self, job_id: str) -> str:
        with self._copy_jobs_lock:
            job = dict(self._copy_jobs.get(job_id, {}))

        if not job:
            return self._layout(
                "Copy — ChipBit",
                self._flash(None, t("copy.unknown_job"))
                + '<section class="block"><p>'
                + '<a href="/files">{t("files.back_to_drives")}</a></p></section>',
                include_events=False,
            )

        if not job["done"]:
            status_url = f"/files/copy/status?job={quote(job_id)}"
            body = dedent(f"""
                <section class="block">
                  <h2>{t('copy.heading')}</h2>
                  <div class="spinner"></div>
                  <p class="muted">{t('copy.body')}</p>
                </section>
                """).strip()
            return self._layout(
                "Copying… — ChipBit",
                body,
                include_events=False,
                head_extra=(
                    '<meta http-equiv="refresh" '
                    f'content="2; url={escape(status_url)}" />'
                ),
            )

        with self._copy_jobs_lock:
            self._copy_jobs.pop(job_id, None)

        if job["error"]:
            back = job.get("back", "")
            back_url = (
                f"/files/browse?p={quote(back)}" if back else "/files"
            )
            return self._layout(
                "Copy failed — ChipBit",
                self._flash(None, job["error"])
                + '<section class="block"><p>'
                + f'<a href="{escape(back_url)}">{t("common.back")}</a></p></section>',
                include_events=False,
            )

        pf = job["prefill"]
        qs = (
            "type=" + quote(pf.get("type", ""))
            + "&label=" + quote(pf.get("label", ""))
        )
        if "data_dir" in pf:
            qs += "&data_dir=" + quote(pf["data_dir"])
        if "game_id" in pf:
            qs += "&game_id=" + quote(pf["game_id"])
        if "swf" in pf:
            qs += "&swf=" + quote(pf["swf"])
        if "conf" in pf:
            qs += "&conf=" + quote(pf["conf"])
        # Redirect immediately via meta-refresh — no JS needed.
        return self._layout(
            "Done — ChipBit",
            '<section class="block"><p>'
            + escape(t("copy.done"))
            + "</p></section>",
            include_events=False,
            head_extra=f'<meta http-equiv="refresh" content="0; url=/?{escape(qs)}" />',
        )

    def render_work(
        self, *, message: str | None = None, error: str | None = None
    ) -> str:
        sections: list[str] = []
        total_images = 0
        for label, directory in self.work_dirs():
            images, others = self._work_files(directory)
            total_images += len(images)
            if not images and not others:
                continue
            shown = images[:_WORK_PREVIEW_LIMIT]
            tiles = "".join(
                f'<figure class="work-item">'
                f'<a href="/work/file?p={quote(str(image))}" target="_blank">'
                f'<img loading="lazy" src="/work/file?p={quote(str(image))}" '
                f'alt="{escape(image.name)}" /></a>'
                f"<figcaption>{escape(image.name)}</figcaption></figure>"
                for image in shown
            )
            notes = []
            if len(images) > len(shown):
                notes.append(
                    escape(t("work.truncated", shown=len(shown), total=len(images)))
                )
            if others:
                notes.append(escape(t("work.other_files", count=others)))
            note = (
                f'<p class="muted small">{" · ".join(notes)}</p>' if notes else ""
            )
            sections.append(
                f'<section class="block">'
                f"<h2>{escape(t('work.from_title', title=label))}</h2>"
                f'<div class="work-grid">{tiles}</div>{note}</section>'
            )

        if not sections:
            body = (
                '<section class="block"><p class="lede">'
                + escape(t("work.empty"))
                + "</p></section>"
            )
        else:
            body = "".join(sections)

        drives = self._detect_drives()
        if drives:
            options = "".join(
                f'<option value="{escape(str(d))}">{escape(d.name)}</option>'
                for d in drives
            )
            copy_form = dedent(f"""
                <section class="block">
                  <h2>{t('work.copy_heading')}</h2>
                  <form method="post" action="/work/copy" class="inline-form">
                    <label>{t('work.copy_drive')}
                      <select name="drive">{options}</select>
                    </label>
                    <button type="submit" class="btn-primary">
                      {t('work.copy_button')}
                    </button>
                  </form>
                </section>
                """).strip()
        else:
            copy_form = dedent(f"""
                <section class="block">
                  <h2>{t('work.copy_heading')}</h2>
                  <p class="muted">{t('work.no_drives')}</p>
                  <p><a class="btn btn-quiet" href="/work">{t('work.rescan')}</a></p>
                </section>
                """).strip()

        header = dedent(f"""
            <section class="block">
              <p class="crumb"><a href="/">{t('files.crumb_back')}</a></p>
              <h1>{t('work.heading')}</h1>
              <p class="lede">{t('work.lede')}</p>
            </section>
            """).strip()

        return self._layout(
            "ChipBit",
            self._flash(message, error) + header + copy_form + body,
            include_events=False,
        )

    def render_work_export_status(self, job_id: str) -> str:
        with self._export_jobs_lock:
            job = dict(self._export_jobs.get(job_id, {}))
        if not job:
            return self._layout(
                "ChipBit",
                self._flash(None, t("copy.unknown_job"))
                + f'<section class="block"><p><a href="/work">'
                f'{t("common.back")}</a></p></section>',
                include_events=False,
            )

        if not job["done"]:
            status_url = f"/work/copy/status?job={quote(job_id)}"
            body = dedent(f"""
                <section class="block">
                  <h1>{t('work.copying')}</h1>
                  <p class="muted">{t('work.copying_body')}</p>
                  <div class="spinner"></div>
                </section>
                """).strip()
            return self._layout(
                "ChipBit", body, include_events=False,
                head_extra=(
                    '<meta http-equiv="refresh" '
                    f'content="2; url={escape(status_url)}" />'
                ),
            )

        with self._export_jobs_lock:
            self._export_jobs.pop(job_id, None)

        if job["error"]:
            return self.render_work(error=t("work.failed", detail=job["error"]))
        done = t("work.done") if job["unmounted"] else t("work.done_no_unmount")
        return self.render_work(message=done)

    def start_work_export(self, drive: str) -> str:
        """Copy every work directory onto ``drive``; returns a job id."""
        target = Path(drive)
        if target not in self._detect_drives():
            raise ValueError(f"not a mounted drive: {drive}")
        sources = self.work_dirs()
        if not sources:
            raise ValueError("there is nothing saved to copy yet")

        import uuid

        job_id = uuid.uuid4().hex[:12]
        with self._export_jobs_lock:
            self._export_jobs[job_id] = {
                "done": False, "error": None, "unmounted": False, "files": 0,
            }
        thread = threading.Thread(
            target=self._run_work_export,
            args=(job_id, target, sources),
            daemon=True,
        )
        thread.start()
        return job_id

    def _run_work_export(
        self, job_id: str, drive: Path, sources: list[tuple[str, Path]]
    ) -> None:
        error: str | None = None
        copied = 0
        # Date-stamped so backing up twice never overwrites the first copy.
        root = drive / "ChipBit" / time.strftime("%Y-%m-%d")
        try:
            for _label, directory in sources:
                dest_base = root / directory.name.lstrip(".")
                for entry in sorted(directory.rglob("*")):
                    if not entry.is_file():
                        continue
                    dest = dest_base / entry.relative_to(directory)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    # copy, not copy2: preserving mode/times raises on FAT,
                    # which is what a USB stick from a drawer is formatted as.
                    shutil.copy(entry, dest)
                    copied += 1
        except (OSError, shutil.Error) as exc:
            error = str(exc)

        unmounted = False
        if error is None:
            # Parents pull the stick the instant the bar stops, so flush and
            # unmount before saying a word about it being safe.
            try:
                self.runner(["sync"], check=False, capture_output=True, text=True)
            except OSError:
                pass
            device = self._device_for_mount(drive)
            if device:
                try:
                    result = self.runner(
                        ["udisksctl", "unmount", "-b", device],
                        check=False, capture_output=True, text=True,
                    )
                    unmounted = result.returncode == 0
                except OSError:
                    unmounted = False

        with self._export_jobs_lock:
            self._export_jobs[job_id] = {
                "done": True, "error": error,
                "unmounted": unmounted, "files": copied,
            }

    def _home(self) -> Path:
        """Home directory the titles save into.

        Mirrors how the launcher resolves it, because user_dirs are relative
        to whatever HOME the title was launched with.
        """
        if self.home_path is not None:
            return self.home_path
        return Path(os.environ.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir)

    def work_dirs(self) -> list[tuple[str, Path]]:
        """(title label, directory) for every declared, existing work dir.

        Driven off the catalog's user_dirs, which the launcher already creates
        at launch -- so supporting a new title's work is a catalog edit, not a
        code change.  Nothing outside these directories is ever exposed.
        """
        home = self._home()
        state = self.state_path or _STATE_ROOT
        # A user_dir may be relative to home, or an absolute path under the
        # state root -- TuxPaint's --savedir puts drawings in the latter, and
        # a gallery that only looked at home found nothing at all.
        roots = (home, state.resolve())
        found: list[tuple[str, Path]] = []
        try:
            catalog = self._load_catalog()
        except ConfigLoadError:
            return found
        for title in self._sorted_titles(catalog):
            for rel in title.user_dirs:
                directory = (home / rel).resolve()
                # Catalog-supplied, so still not trusted to stay in bounds.
                if not any(
                    directory == root or root in directory.parents
                    for root in roots
                ):
                    log.warning("ignoring user_dir outside the allowed roots: %s",
                                directory)
                    continue
                if directory.is_dir():
                    found.append((title.label, directory))
        return found

    def _work_files(self, directory: Path) -> tuple[list[Path], int]:
        """Previewable images, plus a count of everything else."""
        images: list[Path] = []
        others = 0
        try:
            entries = sorted(directory.rglob("*"))
        except OSError:
            return images, others
        for entry in entries:
            try:
                if not entry.is_file() or entry.name.startswith("."):
                    continue
            except OSError:
                continue
            if entry.suffix.lower() in _WORK_IMAGE_SUFFIXES:
                images.append(entry)
            else:
                others += 1
        # Newest first: a parent is usually after what was just made.
        images.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        return images, others

    def resolve_work_file(self, raw: str) -> Path:
        """Map a request path to a file, or refuse.

        Allow-list rather than sanitise: the file has to live under one of the
        declared work directories, checked after resolving symlinks.
        """
        candidate = Path(raw)
        if not candidate.is_absolute():
            raise ValueError("work path must be absolute")
        resolved = candidate.resolve()
        for _label, directory in self.work_dirs():
            if resolved == directory or directory in resolved.parents:
                if resolved.is_file():
                    return resolved
                raise ValueError("not a file")
        raise ValueError("path is not inside a work directory")

    def _device_for_mount(self, mount: Path) -> str | None:
        """Backing device for a mount point, so it can be unmounted again."""
        try:
            for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) >= 2 and _unescape_mount_path(parts[1]) == str(mount):
                    return parts[0]
        except OSError:
            pass
        return None

    def _detect_drives(self) -> list[Path]:
        drives = []
        try:
            for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1].startswith("/media/"):
                    drives.append(Path(_unescape_mount_path(parts[1])))
        except OSError:
            pass
        return drives

    def _detect_unmounted_devices(self) -> list[tuple[str, str]]:
        """Return (device_path, label) for removable/optical devices not yet mounted."""
        try:
            result = self.runner(
                [
                    "lsblk", "-J", "-p",
                    "-o", "NAME,LABEL,FSTYPE,MOUNTPOINT,TYPE,HOTPLUG",
                ],
                check=False, capture_output=True, text=True,
            )
            if result.returncode != 0:
                return []
            data = json.loads(result.stdout)
        except (OSError, ValueError):
            return []

        devices: list[tuple[str, str]] = []

        def walk(nodes: list) -> None:
            for node in nodes:
                is_optical = node.get("type") == "rom"
                is_hotplug = node.get("hotplug") in (True, "1", 1)
                has_fs = bool(node.get("fstype"))
                not_mounted = not node.get("mountpoint")
                if (is_optical or is_hotplug) and has_fs and not_mounted:
                    dev = node.get("name", "")
                    label = node.get("label") or dev.rsplit("/", 1)[-1]
                    devices.append((dev, label))
                walk(node.get("children") or [])

        walk(data.get("blockdevices", []))
        return devices

    def mount_device(self, device: str) -> str:
        if not device.startswith("/dev/") or ".." in device:
            raise ValueError(f"invalid device path: {device!r}")
        result = self.runner(
            ["udisksctl", "mount", "-b", device],
            check=False, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"Failed to mount {device}")
        return result.stdout.strip()

    def _list_dir_json(self, path_str: str) -> dict:
        if not path_str:
            return {
                "path": "",
                "parent": None,
                "entries": [
                    {"name": d.name, "path": str(d), "type": "dir", "size": 0}
                    for d in self._detect_drives()
                ],
            }
        p = Path(path_str)
        # Validate before resolving — resolve() follows symlinks and would turn
        # /media/chipbit (which may be a symlink to /run/media/chipbit) into a
        # path that no longer starts with /media/, breaking the security check.
        if ".." in p.parts or not str(p).startswith(str(_MEDIA_ROOT) + "/"):
            raise ValueError("path must be on a mounted drive under /media/")
        if not p.is_dir():
            raise ValueError(f"not a directory: {p} (drive may have been ejected)")
        try:
            children = sorted(
                p.iterdir(), key=lambda c: (c.is_file(), c.name.lower())
            )
        except PermissionError as exc:
            raise ValueError(str(exc)) from exc
        entries = []
        for child in children:
            if child.name.startswith("."):
                continue
            try:
                stat = child.stat()
                entries.append({
                    "name": child.name,
                    "path": str(child),
                    "type": "file" if child.is_file() else "dir",
                    "size": stat.st_size if child.is_file() else 0,
                })
            except OSError:
                continue
        parent = p.parent
        parent_str: str | None = (
            str(parent) if str(parent).startswith(str(_MEDIA_ROOT) + "/") else None
        )
        return {"path": str(p), "parent": parent_str, "entries": entries}

    def copy_game_files(
        self, source_str: str, dest_str: str, games_root: Path
    ) -> str:
        source = Path(source_str)
        if ".." in source.parts or not str(source).startswith(str(_MEDIA_ROOT) + "/"):
            raise ValueError("source must be on a drive mounted under /media/")
        if not source.exists():
            raise ValueError(f"source path not found: {source}")
        dest_rel = dest_str.strip().lstrip("/")
        if not dest_rel:
            raise ValueError("destination path is required")
        if ".." in Path(dest_rel).parts:
            raise ValueError("destination cannot contain '..'")
        dest = (games_root / dest_rel).resolve()
        if not str(dest).startswith(str(games_root.resolve())):
            raise ValueError("destination must be inside /games/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            _copytree_permissive(source, dest)
        else:
            shutil.copy2(str(source), str(dest))
            dest.chmod(0o644)
        os.sync()
        return f"Copied {source.name} → /games/{dest_rel}"

    def _guess_prefill(
        self, dest_rel: str, games_root: Path | None = None
    ) -> dict[str, str]:
        """Return form prefill hints based on the destination path."""
        p = Path(dest_rel)
        label = p.name.replace("-", " ").replace("_", " ").title()
        parts = p.parts
        if parts and parts[0] == "scummvm":
            pf: dict[str, str] = {
                "type": "scummvm", "data_dir": dest_rel, "label": label,
            }
            if games_root is not None:
                game_id = self._detect_scummvm_game_id(games_root / dest_rel)
                if game_id:
                    pf["game_id"] = game_id
            return pf
        if parts and parts[0] == "dosbox" and not dest_rel.endswith(".conf"):
            pf: dict[str, str] = {"type": "dosbox", "label": label}
            if games_root is not None:
                conf_rel = self._generate_dosbox_conf(games_root / dest_rel, dest_rel)
                if conf_rel:
                    pf["conf"] = conf_rel
            return pf
        if (parts and parts[0] == "flash") or dest_rel.lower().endswith(".swf"):
            swf_path = dest_rel
            if games_root is not None and not dest_rel.lower().endswith(".swf"):
                target = games_root / dest_rel
                if target.is_dir():
                    inner = sorted(
                        f for f in target.rglob("*") if f.suffix.lower() == ".swf"
                    )
                    if inner:
                        try:
                            swf_path = str(inner[0].relative_to(games_root))
                        except ValueError:
                            pass
            return {"type": "ruffle", "swf": swf_path, "label": label}
        if dest_rel.endswith(".conf"):
            return {"type": "dosbox", "conf": dest_rel, "label": label}
        return {"type": "exec", "label": label}

    def _detect_scummvm_game_id(self, content_path: Path) -> str | None:
        """Return the first game ID scummvm --detect finds in content_path, or None."""
        import re
        try:
            result = self.runner(
                [self.scummvm_executable, "--detect", f"--path={content_path}"],
                check=False, capture_output=True, text=True, timeout=30.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        for line in result.stdout.splitlines():
            m = re.match(r"^\s+([a-z][a-z0-9.:_-]+)\s+\S", line)
            if m:
                return m.group(1)
        return None

    def _generate_dosbox_conf(self, game_dir: Path, dest_rel: str) -> str | None:
        """Write a minimal dosbox.conf next to game_dir.

        Returns the conf path relative to games_root.
        """
        exe = _find_dos_executable(game_dir)
        conf_path = game_dir.parent / (game_dir.name + ".conf")
        conf_rel = str(Path(dest_rel).parent / (game_dir.name + ".conf"))
        autoexec = [f"mount c {game_dir}", "c:"]
        if exe:
            autoexec.append(exe)
        autoexec.append("exit")
        conf_text = (
            "[SDL]\nfullscreen=true\n\n"
            "[dosbox]\nmemsize=16\n\n"
            "[autoexec]\n" + "\n".join(autoexec) + "\n"
        )
        try:
            conf_path.write_text(conf_text)
            conf_path.chmod(0o644)
        except OSError:
            return None
        return conf_rel

    def _require_unlocked(self, cards: CardsConfig) -> None:
        if "unlock" not in cards.system_cards:
            raise PermissionError("no admin card is enrolled yet")
        status = self.control.status()
        if status.get("unlocked") is not True:
            raise PermissionError("tap the admin card to unlock configuration")

    def _load_catalog(self) -> Catalog:
        return load_catalog_merged(self.catalog_path, self.user_catalog_path)

    def _mode(self, cards: CardsConfig, status: dict[str, object]) -> str:
        if "unlock" not in cards.system_cards:
            return "first-run"
        if status.get("unlocked") is not True:
            return "locked"
        return "unlocked"

    def _render_first_run(
        self,
        *,
        message: str | None,
        error: str | None,
    ) -> str:
        section = dedent(f"""
            <section class="wait-screen">
              {CARD_TAP_SVG}
              <h1>{t('firstrun.heading')}</h1>
              <p class="lede">{t('firstrun.lede')}</p>
              <p class="muted small">{t('firstrun.note')}</p>
              <div class="spinner"></div>
            </section>
            """).strip()
        return f"{self._flash(message, error)}{section}"

    def _render_locked(
        self,
        *,
        status: dict[str, object],
        message: str | None,
        error: str | None,
    ) -> str:
        current = escape(str(status.get("current") or ""))
        playing = (
            '<p class="muted small">'
            f"{t('lock.playing')} <strong>{current}</strong></p>"
            if current else ""
        )
        section = dedent(f"""
            <section class="wait-screen">
              {CARD_TAP_SVG}
              <h1>{t('lock.heading')}</h1>
              <p class="lede">{t('lock.lede')}</p>
              {playing}
              <div class="spinner"></div>
            </section>
            <hr class="rule" />
            <div class="row small muted" style="justify-content:center">
              <a href="/debug">{t('common.diagnostics')}</a>
              <a href="/setup">{t('common.wifi_setup')}</a>
              <form method="post" action="/settings/shutdown">
                <button type="submit" class="link-button">
                  {t('common.shutdown')}
                </button>
              </form>
            </div>
            """).strip()
        return f"{self._flash(message, error)}{section}"

    def _render_unlocked(
        self,
        *,
        catalog: Catalog,
        cards: CardsConfig,
        message: str | None,
        error: str | None,
    ) -> str:
        bindings_by_title: dict[str, list[str]] = defaultdict(list)
        for card in cards.title_cards.values():
            bindings_by_title[card.title_id].append(card.uid)

        sorted_titles = self._sorted_titles(catalog)
        title_rows = "".join(
            self._render_title_card(title, catalog, bindings_by_title)
            for title in sorted_titles
        )
        card_rows = "".join(
            self._render_card_row(uid, card.title_id, sorted_titles)
            for uid, card in sorted(cards.title_cards.items())
        )
        if not card_rows:
            card_rows = (
                '<tr><td colspan="4" class="muted">'
                + escape(t("console.cards.empty"))
                + "</td></tr>"
            )

        admin_uid = escape(cards.system_cards["unlock"].uid)
        keyboard_options = "".join(
            f'<option value="{code}">{t("keyboard." + code)}</option>'
            for code in _KEYBOARD_LAYOUTS
        )
        shutdown_confirm = _js_in_attr(t("console.settings.shutdown_confirm"))
        current_language = read_language(self.language_path)
        language_options = "".join(
            f'<option value="{choice.code}"'
            f'{" selected" if choice.code == current_language else ""}>'
            f"{escape(choice.name)}</option>"
            for choice in available_languages(self.locale_dirs)
        )
        section = dedent(f"""
            <div id="op-overlay" class="op-overlay" hidden>
              <div class="spinner"></div>
              <div class="op-overlay-text">
                <strong id="op-overlay-title"></strong>
                <span id="op-overlay-msg"></span>
                <span id="op-overlay-elapsed" class="op-elapsed"></span>
              </div>
              <button id="op-overlay-dismiss" class="op-overlay-dismiss"
                      type="button" hidden title="Dismiss">&times;</button>
            </div>
            <div id="tap-now-banner" class="flash wait" style="display:none">
              {t('console.tap_banner')}
            </div>
            <script>
              function exitAdmin() {{
                fetch('/settings/lock', {{method: 'POST'}})
                  .then(() => location.replace('/kiosk'));
              }}
            </script>

            <section class="block">
              <h1>{t('console.heading')}</h1>
              <p class="lede">{t('console.lede')}</p>
              <p class="row">
                <button type="button" class="btn-primary" onclick="exitAdmin()">
                  {t('console.back_to_play')}
                </button>
                <span class="small muted">
                  {t('console.admin_card')}
                  <span class="uid">{admin_uid}</span>
                </span>
              </p>
            </section>

            <section class="block">
              <div class="catalog-grid">{title_rows}</div>
            </section>

            <section class="block">
              <h2>{t('console.cards.heading')}</h2>
              <div class="table-wrap">
                <table class="card-table">
                  <thead>
                    <tr>
                      <th>{t('console.cards.col_card')}</th>
                      <th>{t('console.cards.col_launches')}</th>
                      <th>{t('console.cards.col_change')}</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>{card_rows}</tbody>
                </table>
              </div>
            </section>

            <section class="block">
              <h2>{t('console.work.heading')}</h2>
              <p class="muted">{t('console.work.body')}</p>
              <p>
                <a class="btn btn-quiet" href="/work">
                  {t('console.work.open')}
                </a>
              </p>
            </section>

            <section class="block">
              <h2>{t('console.files.heading')}</h2>
              <p class="muted">{t('console.files.body')}</p>
              <p>
                <a class="btn btn-quiet" href="/files">
                  {t('console.files.open')}
                </a>
              </p>
            </section>

            <section class="block">
              <h2>{t('console.custom.heading')}</h2>
              <p class="muted">{t('console.custom.body')}</p>
              <details id="custom-web">
                <summary>{t('console.custom.web.summary')}</summary>
                <form method="post" action="/titles/custom" class="wifi-form">
                  <input type="hidden" name="type" value="web" />
                  <label>{t('common.name')}
                    <input type="text" name="label"
                      placeholder="My Website" required /></label>
                  <label>{t('console.custom.web.url')}
                    <input type="url" name="url"
                      placeholder="https://example.com" required /></label>
                  <button type="submit">
                    {t('console.custom.web.save')}
                  </button>
                </form>
              </details>
              <details id="custom-exec">
                <summary>{t('console.custom.exec.summary')}</summary>
                <form method="post" action="/titles/custom" class="wifi-form">
                  <input type="hidden" name="type" value="exec" />
                  <label>{t('common.name')}
                    <input type="text" name="label"
                      placeholder="My App" required /></label>
                  <label>{t('console.custom.exec.cmd')}
                    <input type="text" name="cmd"
                      placeholder="myapp --fullscreen" required /></label>
                  <label>{t('console.custom.exec.apt')}
                    <input type="text" name="apt"
                      placeholder="my-package" /></label>
                  <button type="submit">
                    {t('console.custom.exec.save')}
                  </button>
                </form>
              </details>
              <details id="custom-scummvm">
                <summary>{t('console.custom.scummvm.summary')}</summary>
                <form method="post" action="/titles/custom" class="wifi-form">
                  <input type="hidden" name="type" value="scummvm" />
                  <label>{t('common.name')}
                    <input type="text" name="label"
                      placeholder="Monkey Island" required /></label>
                  <label>
                    {t('console.custom.scummvm.game_id')}
                    <input type="text" name="game_id" placeholder="monkey" required />
                  </label>
                  <label>
                    {t('console.custom.scummvm.data_dir')}
                    <input type="text" name="data_dir" placeholder="scummvm/monkey" />
                  </label>
                  <button type="submit">
                    {t('console.custom.scummvm.save')}
                  </button>
                </form>
              </details>
              <details id="custom-dosbox">
                <summary>{t('console.custom.dosbox.summary')}</summary>
                <form method="post" action="/titles/custom" class="wifi-form">
                  <input type="hidden" name="type" value="dosbox" />
                  <label>{t('common.name')}
                    <input type="text" name="label"
                      placeholder="My DOS Game" required /></label>
                  <label>
                    {t('console.custom.dosbox.conf')}
                    <input type="text" name="conf"
                      placeholder="mygame/dosbox.conf" required />
                  </label>
                  <button type="submit">
                    {t('console.custom.dosbox.save')}
                  </button>
                </form>
              </details>
              <details id="custom-ruffle">
                <summary>{t('console.custom.ruffle.summary')}</summary>
                <form method="post" action="/titles/custom" class="wifi-form">
                  <input type="hidden" name="type" value="ruffle" />
                  <label>{t('common.name')}
                    <input type="text" name="label"
                      placeholder="Math Blaster" required /></label>
                  <label>
                    {t('console.custom.ruffle.swf')}
                    <input type="text" name="swf"
                      placeholder="flash/mathblaster.swf" required />
                  </label>
                  <button type="submit">
                    {t('console.custom.ruffle.save')}
                  </button>
                </form>
              </details>
            </section>
            <script>
              (function() {{
                const p = new URLSearchParams(window.location.search);
                const type = p.get('type');
                if (!type) return;
                const details = document.getElementById('custom-' + type);
                if (!details) return;
                details.open = true;
                for (const [key, val] of p.entries()) {{
                  if (key === 'type' || !val) continue;
                  const inp = details.querySelector('[name="' + key + '"]');
                  if (inp) inp.value = val;
                }}
                details.scrollIntoView({{behavior: 'smooth'}});
                history.replaceState({{}}, '', '/');
              }})();
            </script>

            <section class="block">
              <h2>{t('console.settings.heading')}</h2>
              <h3>{t('console.settings.wifi')}</h3>
              <form method="post" action="/wifi/connect" class="wifi-form">
                <label>{t('common.network')}
                  <select id="ssid-select" name="ssid" required>
                    <option value="" disabled selected>
                      {t('common.scanning')}
                    </option>
                  </select>
                </label>
                <div id="ssid-manual-row" hidden>
                  <label>{t('common.network_name')}
                    <input type="text" id="ssid-manual-input"
                           autocomplete="off" />
                  </label>
                </div>
                <label>{t('common.password')}
                  <input type="password" name="password" />
                </label>
                <button type="submit">
                  {t('console.settings.connect')}
                </button>
              </form>
              <script>{WIFI_SCAN_SCRIPT}</script>

              <h3>{t('console.settings.language')}</h3>
              <form method="post" action="/settings/language" class="inline-form">
                <label>{t('console.settings.language')}
                  <select name="language">{language_options}</select>
                </label>
                <button type="submit">
                  {t('console.settings.apply')}
                </button>
              </form>

              <h3>{t('console.settings.keyboard')}</h3>
              <form method="post" action="/settings/keyboard" class="inline-form">
                <label>{t('console.settings.layout')}
                  <select name="layout">{keyboard_options}</select>
                </label>
                <button type="submit">
                  {t('console.settings.apply')}
                </button>
              </form>

              <h3>{t('console.settings.this_pi')}</h3>
              <div class="row">
                <form method="post" action="/settings/lock">
                  <button type="submit" class="btn-quiet">
                    {t('console.settings.lock')}
                  </button>
                </form>
                <form method="post" action="/settings/shutdown"
                      onsubmit="return confirm('{shutdown_confirm}')">
                  <button type="submit" class="btn-danger">
                    {t('common.shutdown')}
                  </button>
                </form>
                <a class="small" href="/debug">{t('common.diagnostics')}</a>
              </div>
            </section>
            """).strip()
        return f"{self._flash(message, error)}{section}"

    def _render_title_card(
        self,
        title: CatalogTitle,
        catalog: Catalog,
        bindings_by_title: dict[str, list[str]],
    ) -> str:
        state = self._title_state(title, catalog)
        # Readiness is the one thing a parent scans this grid for, so it gets a
        # colored chip; the plumbing detail below it stays quiet text.
        chip_class = {
            "ready": "ready",
            "needs_files": "wait",
            "downloads": "wait",
        }.get(state, "plain")
        state_label = escape(t(f"console.state.{state}"))
        ink, _ = ink_for(title.id)
        uids = sorted(bindings_by_title.get(title.id, []))
        if uids:
            chips = "".join(
                f'<span class="chip plain">{escape(uid)}</span>' for uid in uids
            )
            bound = f'<div class="chip-row">{chips}</div>'
        else:
            bound = f'<p class="meta">{t("console.tile.no_card")}</p>'
        action = quote(title.id)
        blurb = escape(title.blurb) if title.blurb else escape(
            self._title_summary(title, catalog)
        )
        return dedent(f"""
            <article class="title-card" style="--tile-ink: {ink}">
              <div class="band"></div>
              <div class="body">
                <h2>{escape(title.label)}</h2>
                <p class="meta">{blurb}</p>
                <p><span class="chip {chip_class}">{state_label}</span></p>
                {bound}
                <form method="post" action="/titles/{action}/enroll"
                      class="enroll-form">
                  <button type="submit" class="btn-quiet">
                    {t('console.tile.bind')}
                  </button>
                </form>
              </div>
            </article>
            """).strip()

    def _render_card_row(
        self,
        uid: str,
        title_id: str,
        titles: list[CatalogTitle],
    ) -> str:
        options = "\n".join(
            self._render_title_option(title, selected=(title.id == title_id))
            for title in titles
        )
        quoted_uid = quote(uid)
        reassign_label = escape(t("console.cards.reassign_label", uid=uid))
        disable_confirm = _js_in_attr(t("console.cards.disable_confirm"))
        return dedent(f"""
            <tr>
              <td><span class="uid">{escape(uid)}</span></td>
              <td>{escape(title_id)}</td>
              <td>
                <form
                  method="post"
                  action="/cards/{quoted_uid}/reassign"
                  class="inline-form"
                >
                  <select name="title_id" aria-label="{reassign_label}">
                    {options}
                  </select>
                  <button type="submit" class="btn-quiet">
                    {t('console.cards.reassign')}
                  </button>
                </form>
              </td>
              <td>
                <form
                  method="post"
                  action="/cards/{quoted_uid}/remove"
                  class="inline-form"
                  onsubmit="return confirm('{disable_confirm}')"
                >
                  <button type="submit" class="btn-caution">
                    {t('console.cards.disable')}
                  </button>
                </form>
              </td>
            </tr>
            """).strip()

    def _render_title_option(self, title: CatalogTitle, *, selected: bool) -> str:
        selected_attr = " selected" if selected else ""
        label = escape(title.label)
        value = escape(title.id)
        return f'<option value="{value}"{selected_attr}>{label}</option>'

    def _sorted_titles(self, catalog: Catalog) -> list[CatalogTitle]:
        return sorted(catalog.titles.values(), key=lambda item: item.label.lower())

    def _title_summary(self, title: CatalogTitle, catalog: Catalog) -> str:
        games_root = str(catalog.settings.games_root)
        if title.type == "web":
            return f"Web card for {title.url or ''}"
        if title.type == "exec":
            return "Native app"
        if title.type == "scummvm":
            return f"ScummVM title in {games_root}"
        if title.type == "dosbox":
            return f"DOSBox config under {games_root}"
        return f"Ruffle content under {games_root}"

    def _title_state(self, title: CatalogTitle, catalog: Catalog) -> str:
        """Can a parent bind a card to this title right now?

        Returns a stable id, not display text: the chip colour is chosen from
        it and it must not change when the UI is translated.  The wording
        lives in strings.py under "console.state.<id>".

        Whether a title is bundled, installs on demand, or is waiting on files
        the parent has to supply is an implementation detail everywhere except
        here, where it decides whether tapping a card will work.
        """
        if title.data == "required":
            ready = self._required_data_ready(title, catalog)
            return "ready" if ready else "needs_files"
        if title.install:
            return "downloads"
        return "ready"

    def _kiosk_state(
        self,
        cards: CardsConfig,
        status: dict[str, object],
        operation: dict[str, str] | None,
    ) -> dict[str, str]:
        if status.get("capture_mode") is True:
            return self._kiosk_flood({
                "kind": "enroll",
                "title": t("kiosk.enroll.title"),
                "body": t("kiosk.enroll.body"),
            }, "enroll")

        if "unlock" not in cards.system_cards:
            return {
                "kind": "first-run",
                "title": t("kiosk.first_run.title"),
                "body": t("kiosk.first_run.body"),
            }

        if operation is not None:
            state = {
                "kind": operation["kind"],
                "title": operation["title"],
                "body": operation["message"],
                **({"art": operation["art"]} if "art" in operation else {}),
            }
            return self._kiosk_flood(state, operation.get("id") or operation["title"])

        current = status.get("current")
        if status.get("running") is True and isinstance(current, str) and current:
            current_art = status.get("current_art")
            current_id = status.get("current_id")
            kiosk = {
                "kind": "loading",
                "title": current,
                "body": t("kiosk.loading.body"),
                "art": (
                    current_art if isinstance(current_art, str) and current_art
                    else "/art/default"
                ),
            }
            return self._kiosk_flood(
                kiosk,
                current_id if isinstance(current_id, str) and current_id else current,
            )

        last_event = status.get("last_event")
        if isinstance(last_event, dict) and last_event.get("kind") == "unknown-card":
            # A kid is holding a card that does nothing.  Say who can fix it,
            # not what went wrong -- and keep the UID for the grown-up.
            uid = last_event.get("uid", "")
            body = (
                t("kiosk.unknown.body_uid", uid=uid) if uid
                else t("kiosk.unknown.body")
            )
            return {
                "kind": "unknown-card",
                "title": t("kiosk.unknown.title"),
                "body": body,
                "ink": "#f0b429",
                "on_ink": "#1a1a19",
            }

        return {
            "kind": "idle",
            "title": t("kiosk.idle.title"),
            "body": t("kiosk.idle.body"),
        }

    def _kiosk_flood(self, state: dict[str, str], key: str) -> dict[str, str]:
        """Attach the card ink that the kiosk screen floods with."""
        ink, on_ink = ink_for(key)
        state["ink"] = ink
        state["on_ink"] = on_ink
        return state

    def _required_data_ready(self, title: CatalogTitle, catalog: Catalog) -> bool:
        cache_key = (
            title.id,
            title.type,
            title.game_id or "",
            title.data_dir or "",
            title.conf or "",
            title.swf or "",
            str(catalog.settings.games_root),
        )
        cached = self._readiness_cache.get(cache_key)
        if cached is not None:
            return cached

        ready = has_required_data(
            title,
            catalog.settings.games_root,
            runner=self.runner,
            scummvm_executable=self.scummvm_executable,
        )
        self._readiness_cache[cache_key] = ready
        return ready

    def _clear_readiness_cache(self) -> None:
        self._readiness_cache.clear()

    def _operation_snapshot(self) -> dict[str, str] | None:
        with self._operation_lock:
            if self._operation is None:
                return None
            return dict(self._operation)

    def _set_operation_state(
        self,
        *,
        title: str,
        message: str,
        art: str | None,
        title_id: str | None = None,
    ) -> None:
        operation = {
            "kind": "loading",
            "title": title,
            "message": message,
        }
        if art:
            operation["art"] = art
        if title_id:
            # Carried so the kiosk floods with the same ink the console
            # tile uses for this title.
            operation["id"] = title_id
        with self._operation_lock:
            self._operation = operation

    def _clear_operation_state(self) -> None:
        with self._operation_lock:
            self._operation = None

    def _layout(
        self,
        title: str,
        body: str,
        *,
        include_events: bool,
        script: str | None = None,
        head_extra: str = "",
    ) -> str:
        script_body = script
        if include_events and script_body is None:
            script_body = PARENT_EVENTS_SCRIPT
        if script_body:
            script_body = _fill_script_strings(script_body)
        script_tag = ""
        if script_body:
            script_tag = f"<script>\n{script_body}\n</script>"

        live = ""
        if include_events:
            live = dedent(f"""
                <p class="live">
                  {t('layout.parent_controls')}
                  <strong id="live-mode">{t('layout.checking')}</strong>
                  <span id="live-detail"></span>
                </p>
                """).strip()

        return dedent(f"""<!doctype html>
            <html lang="{language_tag()}">
            <head>
              <meta charset="utf-8" />
              <meta name="viewport" content="width=device-width, initial-scale=1" />
              <title>{escape(title)}</title>
              {head_extra}
              <style>
            {PAGE_CSS}
              </style>
            </head>
            <body>
              <header class="site-header">
                <p class="wordmark"><a href="/">{CHIPBIT_MARK}ChipBit</a></p>
                {live}
              </header>
              <main>
                {body}
              </main>
              {script_tag}
            </body>
            </html>
            """).strip()

    def _flash(self, message: str | None, error: str | None) -> str:
        parts: list[str] = []
        if message:
            parts.append(f'<div class="flash ok">{escape(message)}</div>')
        if error:
            parts.append(f'<div class="flash error">{escape(error)}</div>')
        return "".join(parts)


_RUFFLE_MIME: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js":   "application/javascript",
    ".wasm": "application/wasm",
    ".map":  "application/json",
}


def create_web_server(
    host: str,
    port: int,
    *,
    catalog_path: Path,
    cards_path: Path,
    control_base_url: str,
    runner: CommandRunner = subprocess.run,
    network_checker: NetworkChecker | None = None,
    scummvm_executable: str = "scummvm",
    event_poll_secs: float = DEFAULT_EVENT_POLL_SECS,
    user_catalog_path: Path | None = None,
    language_path: Path | None = None,
    locale_dirs: tuple[Path, ...] | None = None,
    games_root: Path = Path("/games"),
) -> ThreadingHTTPServer:
    """Create the plain-HTML parent console and kiosk shell server."""
    art_root = catalog_path.parent / "art"
    ruffle_root = Path("/usr/share/chipbit/ruffle")
    app = WebApp(
        catalog_path=catalog_path,
        cards_path=cards_path,
        control=ControlClient(control_base_url),
        runner=runner,
        network_checker=network_checker,
        scummvm_executable=scummvm_executable,
        event_poll_secs=event_poll_secs,
        user_catalog_path=user_catalog_path,
        language_path=language_path,
        locale_dirs=locale_dirs,
    )

    class Handler(BaseHTTPRequestHandler):
        def _send_html(self, code: int, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _redirect(self, location: str) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _read_form(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                raw = self.rfile.read(length).decode("utf-8")
            else:
                raw = ""
            parsed = parse_qs(raw, keep_blank_values=True)
            return {key: values[-1] for key, values in parsed.items()}

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                if path in ("/", "/admin"):
                    # The first-run page is English prose telling a parent what
                    # to do with a card, so the language choice has to come
                    # before it, not after.
                    if app.is_first_run() and app.needs_language_choice():
                        self._send_html(
                            200, app.render_language_picker(next_path="/")
                        )
                        return
                    self._send_html(200, app.render_index())
                    return
                if path == "/setup":
                    if app.needs_language_choice() and not _WIFI_SETUP_FILE.exists():
                        self._send_html(
                            200, app.render_language_picker(next_path="/setup")
                        )
                    elif not _WIFI_COUNTRY_FILE.exists():
                        self._send_html(200, app.render_country_picker())
                    else:
                        qs = parse_qs(urlparse(self.path).query)
                        msg = "Connected to Wi-Fi." if qs.get("connected") else ""
                        self._send_html(200, app.render_setup(message=msg))
                    return
                if path == "/work":
                    self._send_html(200, app.render_work())
                    return
                if path == "/work/file":
                    qs = parse_qs(urlparse(self.path).query)
                    try:
                        target = app.resolve_work_file(qs.get("p", [""])[0])
                    except (ValueError, OSError):
                        self.send_response(404)
                        self.end_headers()
                        return
                    self._serve_work_file(target)
                    return
                if path == "/work/copy/status":
                    qs = parse_qs(urlparse(self.path).query)
                    job_id = qs.get("job", [""])[0]
                    self._send_html(200, app.render_work_export_status(job_id))
                    return
                if path == "/setup/skip":
                    _WIFI_SETUP_FILE.touch()
                    self._redirect("/")
                    return
                if path == "/kiosk":
                    cards = load_cards(app.cards_path)
                    if "unlock" not in cards.system_cards:
                        self._redirect("/admin")
                        return
                    # Country chosen but WiFi setup not yet completed → guide
                    # the user through setup before showing the kiosk.
                    if _WIFI_COUNTRY_FILE.exists() and not _WIFI_SETUP_FILE.exists():
                        self._redirect("/setup")
                        return
                    self._send_html(200, app.render_kiosk())
                    return
                if path == "/events":
                    self._serve_events()
                    return
                if path == "/wifi/scan":
                    ssids = app.scan_wifi()
                    body = json.dumps(ssids).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/debug":
                    self._send_html(200, app.wifi_diagnostics())
                    return
                if path == "/files":
                    qs = parse_qs(urlparse(self.path).query)
                    self._send_html(200, app.render_files(
                        message=qs.get("msg", [None])[0],
                        error=qs.get("err", [None])[0],
                    ))
                    return
                if path == "/files/browse":
                    qs = parse_qs(urlparse(self.path).query)
                    browse_path = qs.get("p", [""])[0]
                    msg = qs.get("msg", [None])[0]
                    err = qs.get("err", [None])[0]
                    try:
                        self._send_html(200, app.render_file_browse(
                            browse_path, message=msg, error=err,
                        ))
                    except (ValueError, OSError) as exc:
                        self._send_html(400, app.render_files(error=str(exc)))
                    return
                if path == "/files/copy/status":
                    qs = parse_qs(urlparse(self.path).query)
                    job_id = qs.get("job", [""])[0]
                    self._send_html(200, app.render_copy_status(job_id))
                    return
            except (ConfigLoadError, ControlApiError, InstallationError) as exc:
                self._send_html(502, app.render_index(error=str(exc)))
                return

            if path.startswith("/art/"):
                name = path[len("/art/"):]
                if name and "/" not in name and not name.startswith("."):
                    art_file = art_root / name
                    try:
                        data = art_file.read_bytes()
                        self.send_response(200)
                        self.send_header("Content-Type", "image/png")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    except OSError:
                        pass
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(_DEFAULT_ART_SVG)))
                self.end_headers()
                self.wfile.write(_DEFAULT_ART_SVG)
                return

            # Ruffle web bundle (JS + WASM) served from the install directory.
            if path.startswith("/ruffle/"):
                rel = path[len("/ruffle/"):]
                if not rel or ".." in rel.split("/"):
                    self.send_response(404)
                    self.end_headers()
                    return
                self._serve_file(ruffle_root / rel, _RUFFLE_MIME)
                return

            # SWF game files served so Ruffle can load them over HTTP
            # (file:// is blocked by Ruffle's own runtime guard).
            if path.startswith("/swf/"):
                rel = unquote(path[len("/swf/"):])
                if (
                    not rel
                    or ".." in rel.split("/")
                    or not rel.lower().endswith(".swf")
                ):
                    self.send_response(403)
                    self.end_headers()
                    return
                resolved = (games_root / rel).resolve()
                if not str(resolved).startswith(str(games_root.resolve())):
                    self.send_response(403)
                    self.end_headers()
                    return
                if not resolved.exists():
                    log.warning(
                        "SWF not found: %s (games_root=%s)", resolved, games_root
                    )
                    self.send_response(404)
                    self.end_headers()
                    return
                self._serve_file(resolved, {"application/x-shockwave-flash"})
                return

            self._send_html(404, app.render_index(error="not found"))

        def _serve_file(self, file_path: Path, mime_map: dict | set) -> None:
            suffix = file_path.suffix.lower()
            if isinstance(mime_map, set):
                mime = next(iter(mime_map))
            else:
                mime = mime_map.get(suffix, "application/octet-stream")
            try:
                data = file_path.read_bytes()
            except OSError:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _serve_work_file(self, target: Path) -> None:
            """Serve one of the child's files.

            The path was already allow-listed against the declared work
            directories by resolve_work_file; this only reads and sends it.
            """
            guessed, _ = mimetypes.guess_type(target.name)
            try:
                data = target.read_bytes()
            except OSError:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", guessed or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            # A drawing is immutable once saved, and the gallery may request a
            # hundred of them; let the browser keep them.
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            form = self._read_form()
            if path == "/setup/country":
                country = form.get("country", "").strip().upper()
                try:
                    app.apply_wifi_country(country)
                except (ValueError, RuntimeError) as exc:
                    self._send_html(400, app.render_country_picker(error=str(exc)))
                    return
                self._send_html(200, app.render_rebooting())
                threading.Thread(
                    target=_reboot_after_delay,
                    args=(app.runner,),
                    daemon=True,
                ).start()
                return

            if path == "/setup/wifi":
                try:
                    app.configure_wifi(form.get("ssid", ""), form.get("password"))
                    _WIFI_SETUP_FILE.touch()
                    # Kick NTP sync — Pi has no RTC so the clock is wrong at boot.
                    # Fire-and-forget; sync completes in the background within seconds.
                    try:
                        app.runner(
                            ["sudo", "systemctl", "restart", "systemd-timesyncd"],
                            check=False, capture_output=True, text=True,
                        )
                    except Exception:
                        pass
                    body = json.dumps({"ok": True}).encode()
                except Exception as exc:
                    body = json.dumps({"error": str(exc)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/debug/wifi-enable":
                try:
                    msg = app.wifi_enable()
                except Exception as exc:
                    msg = f"Error: {exc}"
                self._send_html(200, app.wifi_diagnostics(message=msg))
                return
            if path == "/files/mount":
                try:
                    msg = app.mount_device(form.get("device", ""))
                    self._redirect("/files?msg=" + quote(msg))
                except (ValueError, RuntimeError, OSError) as exc:
                    self._redirect("/files?err=" + quote(str(exc)))
                return
            if path == "/files/copy":
                back = form.get("back", "")
                try:
                    catalog = app._load_catalog()
                    job_id = app.start_copy_job(
                        source=form.get("source", ""),
                        dest=form.get("dest", ""),
                        games_root=catalog.settings.games_root,
                        back=back,
                    )
                    self._redirect("/files/copy/status?job=" + quote(job_id))
                except (ConfigLoadError, ValueError, OSError) as exc:
                    err_qs = "err=" + quote(str(exc))
                    if back:
                        self._redirect("/files/browse?p=" + quote(back) + "&" + err_qs)
                    else:
                        self._redirect("/files?" + err_qs)
                return
            # Slow enrollment paths return JSON so the browser can show errors
            # inline without losing context. Quick setting paths stay HTML.
            want_json = (
                path == "/admin/enroll"
                or (path.startswith("/titles/") and path.endswith("/enroll"))
                or (path.startswith("/cards/") and path.endswith("/reassign"))
            )
            try:
                if path == "/admin/enroll":
                    message = app.enroll_admin()
                elif path.startswith("/titles/") and path.endswith("/enroll"):
                    title_id = _path_value(path, "/titles/", "/enroll")
                    message = app.enroll_title(title_id)
                elif path.startswith("/cards/") and path.endswith("/reassign"):
                    uid = _path_value(path, "/cards/", "/reassign")
                    message = app.reassign_card(uid, form.get("title_id", ""))
                elif path.startswith("/cards/") and path.endswith("/remove"):
                    uid = _path_value(path, "/cards/", "/remove")
                    message = app.remove_card(uid)
                elif path == "/settings/reload":
                    message = app.reload_daemon()
                elif path == "/settings/keyboard":
                    message = app.set_keyboard_layout(form.get("layout", ""))
                elif path == "/settings/language":
                    message = app.set_language(form.get("language", ""))
                elif path == "/setup/language":
                    app.set_language(form.get("language", ""))
                    # Whitelisted: this value comes off a form.
                    nxt = form.get("next", "/")
                    self._redirect(nxt if nxt in ("/", "/setup") else "/")
                    return
                elif path == "/settings/lock":
                    message = app.lock_controls()
                elif path == "/settings/shutdown":
                    message = app.shutdown_system()
                elif path == "/wifi/connect":
                    message = app.configure_wifi(
                        form.get("ssid", ""),
                        form.get("password"),
                    )
                elif path == "/work/copy":
                    job_id = app.start_work_export(form.get("drive", ""))
                    self._redirect(f"/work/copy/status?job={quote(job_id)}")
                    return
                elif path == "/titles/custom":
                    message = app.create_custom_title(form)
                else:
                    self._send_html(404, app.render_index(error="not found"))
                    return
            except PermissionError as exc:
                if want_json:
                    self._send_json(403, {"error": str(exc)})
                else:
                    self._send_html(403, app.render_index(error=str(exc)))
                return
            except (
                ValueError,
                RuntimeError,
                InstallationError,
                NetworkUnavailableError,
                DataMissingError,
                ConfigLoadError,
                ControlApiError,
            ) as exc:
                if want_json:
                    self._send_json(400, {"error": str(exc)})
                else:
                    self._send_html(400, app.render_index(error=str(exc)))
                return

            if want_json:
                self._send_json(200, {"ok": True, "message": message})
            else:
                self._send_html(200, app.render_index(message=message))

        def _serve_events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            try:
                while True:
                    try:
                        payload_obj = app.event_payload()
                    except ConfigLoadError as exc:
                        payload_obj = {
                            "mode": "error",
                            "error": str(exc),
                            "has_admin_card": False,
                            "status": {
                                "running": False,
                                "current": None,
                                "current_art": None,
                                "unlocked": False,
                                "capture_mode": False,
                                "last_event": None,
                            },
                            "operation": None,
                            "kiosk": {
                                "kind": "error",
                                "title": "Ask a grown-up",
                                "body": "ChipBit needs attention right now.",
                            },
                        }

                    payload = json.dumps(payload_obj).encode("utf-8")
                    self.wfile.write(b"data: ")
                    self.wfile.write(payload)
                    self.wfile.write(b"\n\n")
                    self.wfile.flush()
                    time.sleep(app.event_poll_secs)
            except (BrokenPipeError, ConnectionResetError):
                return

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return None

    return ThreadingHTTPServer((host, port), Handler)


def _path_value(path: str, prefix: str, suffix: str) -> str:
    if not path.startswith(prefix) or not path.endswith(suffix):
        raise ValueError("invalid request path")
    return unquote(path[len(prefix) : -len(suffix)])
