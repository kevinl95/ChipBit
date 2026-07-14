"""Plain-HTML web service and kiosk shell for the ChipBit runtime."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import defaultdict
from collections.abc import Callable
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

log = logging.getLogger(__name__)

DEFAULT_EVENT_POLL_SECS = 1.0
_MEDIA_ROOT = Path("/media")
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
_VALID_COUNTRY_CODES: frozenset[str] = frozenset(c for c, _ in _WIFI_COUNTRIES)


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
            if f.suffix.lower() in (".exe", ".com", ".bat") and f.name.lower() not in _DOS_SKIP_EXES:
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


# Served for any /art/<name> that doesn't exist on disk — generic app placeholder.
_DEFAULT_ART_SVG: bytes = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">'
    b'<rect width="200" height="200" rx="24" fill="#6d28d9"/>'
    b'<circle cx="100" cy="100" r="58" fill="rgba(255,255,255,0.15)"/>'
    b'<polygon points="82,68 82,132 142,100" fill="white"/>'
    b"</svg>"
)

PAGE_CSS = dedent("""
    :root {
      color-scheme: light;
      --paper: #f5f0e8;
      --ink: #16212c;
      --accent: #d95f32;
      --panel: #fffaf4;
      --border: #d9c8b3;
      --muted: #5d6b79;
      --ok: #2b7a45;
      --bad: #9a2d21;
    }
    body {
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      background: radial-gradient(circle at top, #fff7eb, var(--paper) 60%);
      color: var(--ink);
    }
    main {
      max-width: 1100px;
      margin: 0 auto;
      padding: 2rem 1rem 4rem;
    }
    header {
      display: flex;
      justify-content: space-between;
      gap: 1rem;
      align-items: baseline;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 1rem;
      box-shadow: 0 10px 24px rgba(22, 33, 44, 0.08);
    }
    .panel-wide {
      margin-bottom: 1rem;
    }
    .catalog-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 1rem;
      margin: 1rem 0;
    }
    .title-card {
      background: rgba(255, 255, 255, 0.7);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 1rem;
      overflow-wrap: break-word;
      word-break: break-all;
    }
    .eyebrow {
      margin: 0 0 0.5rem;
      color: var(--accent);
      font-size: 0.78rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .flash {
      padding: 0.8rem 1rem;
      margin-bottom: 1rem;
      border-radius: 12px;
    }
    .flash.ok {
      background: rgba(43, 122, 69, 0.12);
      color: var(--ok);
    }
    .flash.error {
      background: rgba(154, 45, 33, 0.12);
      color: var(--bad);
    }
    .muted {
      color: var(--muted);
    }
    .stats {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
      gap: 0.75rem;
    }
    .stats div {
      background: rgba(217, 200, 179, 0.2);
      padding: 0.75rem;
      border-radius: 12px;
    }
    button {
      border: 0;
      border-radius: 999px;
      padding: 0.7rem 1rem;
      background: var(--accent);
      color: white;
      cursor: pointer;
    }
    button:hover {
      filter: brightness(0.95);
    }
    .btn-link {
      display: inline-block;
      margin-top: 0.75rem;
      padding: 0.6rem 1rem;
      background: var(--accent);
      color: white;
      border-radius: 999px;
      text-decoration: none;
      font-size: 0.9rem;
    }
    .btn-link:hover {
      filter: brightness(0.95);
    }
    table {
      width: 100%;
      border-collapse: collapse;
    }
    th,
    td {
      padding: 0.6rem;
      border-bottom: 1px solid var(--border);
      text-align: left;
      vertical-align: top;
    }
    .inline-form,
    .wifi-form {
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
      align-items: center;
    }
    input,
    select {
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 0.55rem 0.7rem;
      background: white;
    }
    code {
      background: rgba(217, 200, 179, 0.24);
      padding: 0.15rem 0.3rem;
      border-radius: 6px;
    }
    details {
      margin-top: 0.75rem;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 0.6rem 1rem;
    }
    details[open] {
      padding-bottom: 1rem;
    }
    summary {
      cursor: pointer;
      font-weight: bold;
      padding: 0.2rem 0;
      list-style: none;
    }
    summary::before {
      content: "▶ ";
      font-size: 0.7em;
    }
    details[open] summary::before {
      content: "▼ ";
    }
    details .wifi-form {
      margin-top: 0.75rem;
    }
    @media (max-width: 640px) {
      header {
        flex-direction: column;
        align-items: flex-start;
      }
      .inline-form,
      .wifi-form {
        flex-direction: column;
        align-items: stretch;
      }
      button,
      input,
      select {
        width: 100%;
        box-sizing: border-box;
      }
    }
    .spinner {
      width: 2.2rem;
      height: 2.2rem;
      border: 3px solid var(--border);
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
      margin: 0 auto 0.75rem;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .connecting-box {
      text-align: center;
      padding: 2rem 1rem;
      color: var(--muted);
    }
    .op-overlay {
      position: fixed;
      bottom: 0;
      left: 0;
      right: 0;
      z-index: 99;
      background: var(--panel);
      border-top: 3px solid var(--accent);
      padding: 1rem 1.5rem;
      display: flex;
      align-items: center;
      gap: 1rem;
      box-shadow: 0 -4px 24px rgba(22,33,44,.18);
    }
    .op-overlay[hidden] { display: none; }
    .op-overlay .spinner {
      width: 1.5rem;
      height: 1.5rem;
      border-width: 3px;
      margin: 0;
      flex-shrink: 0;
    }
    .op-overlay.error .spinner { display: none; }
    .op-overlay-dismiss {
      margin-left: auto;
      background: none;
      border: none;
      font-size: 1.25rem;
      cursor: pointer;
      color: var(--muted);
      padding: 0 .25rem;
    }
    .link-button {
      background: none;
      border: none;
      padding: 0;
      font-size: inherit;
      color: var(--muted);
      cursor: pointer;
      text-decoration: underline;
    }
    .op-overlay-text strong {
      display: block;
    }
    .op-overlay-text span {
      color: var(--muted);
      font-size: .9rem;
    }
    """).strip()

PARENT_EVENTS_SCRIPT = dedent("""
    var overlay = document.getElementById('op-overlay');
    var overlayTitle = document.getElementById('op-overlay-title');
    var overlayMsg = document.getElementById('op-overlay-msg');
    var overlayDismiss = document.getElementById('op-overlay-dismiss');
    var badge = document.getElementById('live-mode');
    var tapBanner = document.getElementById('tap-now-banner');

    var enrollInProgress = false;
    var overlayPinned = false;  // true while an error is displayed; SSE won't clear it

    function showOverlay(title, msg, isError) {
      if (overlayTitle) overlayTitle.textContent = title;
      if (overlayMsg) overlayMsg.textContent = msg;
      if (overlay) {
        overlay.hidden = false;
        overlay.classList.toggle('error', !!isError);
      }
      if (overlayDismiss) overlayDismiss.hidden = !isError;
    }
    function hideOverlay() {
      if (overlay) { overlay.hidden = true; overlay.classList.remove('error'); }
      if (overlayDismiss) overlayDismiss.hidden = true;
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
      if (badge) badge.textContent = state.mode;
      if (tapBanner) {
        tapBanner.style.display =
          (state.status && state.status.capture_mode) ? '' : 'none';
      }
      if (state.operation) {
        showOverlay(
          state.operation.title || 'Working…',
          state.operation.message || ''
        );
        document.querySelectorAll('.enroll-form button[disabled]')
          .forEach(function(btn) {
          btn.textContent = state.operation.message || 'Working…';
        });
      } else if (!enrollInProgress && !overlayPinned) {
        hideOverlay();
      }
    };

    document.querySelectorAll('.enroll-form').forEach(function(form) {
      form.addEventListener('submit', function(e) {
        e.preventDefault();
        var btn = form.querySelector('button[type="submit"]');
        if (btn) {
          btn.disabled = true;
          btn.textContent = 'Waiting for card…';
        }
        enrollInProgress = true;
        overlayPinned = false;
        showOverlay('Enrolling…', 'Tap your card to the reader now');
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
              showOverlay('Enrollment failed', data.error || 'Something went wrong.', true);
              if (btn) {
                btn.disabled = false;
                btn.textContent = 'Try again';
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
        :root {
            color-scheme: light;
            --paper: #efe7d7;
            --ink: #102030;
            --accent: #d95f32;
            --sun: #f3b94d;
            --panel: rgba(255, 251, 244, 0.9);
            --border: rgba(16, 32, 48, 0.12);
            --shadow: rgba(16, 32, 48, 0.18);
        }
        body {
            margin: 0;
            min-height: 100vh;
            font-family: "Avenir Next", "Trebuchet MS", "Gill Sans", sans-serif;
            color: var(--ink);
            background:
                radial-gradient(
                    circle at top,
                    rgba(243, 185, 77, 0.45),
                    transparent 34%
                ),
                linear-gradient(180deg, #fff8ec 0%, #f4ead7 100%);
        }
        .kiosk-shell {
            min-height: 100vh;
            display: grid;
            place-items: center;
            padding: 4vw;
            box-sizing: border-box;
        }
        .kiosk-panel {
            width: min(100%, 1180px);
            min-height: min(78vh, 820px);
            display: grid;
            grid-template-columns: minmax(0, 1fr) minmax(260px, 36%);
            gap: clamp(1.5rem, 4vw, 3rem);
            align-items: center;
            padding: clamp(2rem, 5vw, 4rem);
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 36px;
            box-shadow: 0 30px 80px var(--shadow);
        }
        .kiosk-copy {
            display: grid;
            gap: 1.25rem;
            max-width: 44rem;
        }
        .kiosk-brand {
            margin: 0;
            color: var(--accent);
            font-size: clamp(1rem, 2vw, 1.4rem);
            font-weight: 700;
            letter-spacing: 0.14em;
            text-transform: uppercase;
        }
        .kiosk-title {
            margin: 0;
            font-size: clamp(2.8rem, 7vw, 5.8rem);
            line-height: 0.95;
            letter-spacing: -0.04em;
        }
        .kiosk-body {
            margin: 0;
            font-size: clamp(1.3rem, 2.7vw, 2rem);
            line-height: 1.35;
            max-width: 30rem;
        }
        .kiosk-art-frame {
            display: grid;
            place-items: center;
            aspect-ratio: 4 / 5;
            border-radius: 28px;
            background: linear-gradient(
                180deg,
                rgba(243, 185, 77, 0.28),
                rgba(217, 95, 50, 0.14)
            );
            border: 1px solid rgba(217, 95, 50, 0.18);
            overflow: hidden;
        }
        .kiosk-art-frame[hidden] {
            display: none;
        }
        .kiosk-art {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }
        .kiosk-spinner {
            display: none;
            width: 2.5rem;
            height: 2.5rem;
            border: 4px solid rgba(217, 95, 50, 0.2);
            border-top-color: var(--accent);
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }
        .kiosk-spinner.active {
            display: block;
        }
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        @media (max-width: 900px) {
            .kiosk-panel {
                grid-template-columns: minmax(0, 1fr);
                min-height: auto;
            }
            .kiosk-copy,
            .kiosk-body {
                max-width: none;
            }
        }
        """).strip()

KIOSK_EVENTS_SCRIPT = dedent("""
    const title = document.getElementById('kiosk-title');
    const body = document.getElementById('kiosk-body');
        const artFrame = document.getElementById('kiosk-art-frame');
        const art = document.getElementById('kiosk-art');
        const spinner = document.getElementById('kiosk-spinner');
    const events = new EventSource('/events');

        const BUSY_KINDS = new Set(['loading', 'enroll']);

        const applyKioskState = (kiosk) => {
            if (!kiosk) {
                return;
            }
            title.textContent = kiosk.title;
            body.textContent = kiosk.body;
            if (spinner) {
                spinner.classList.toggle('active', BUSY_KINDS.has(kiosk.kind));
            }
            if (artFrame && art) {
                if (kiosk.art) {
                    art.src = kiosk.art;
                    art.alt = kiosk.title;
                    artFrame.hidden = false;
                } else {
                    art.removeAttribute('src');
                    art.alt = '';
                    artFrame.hidden = true;
                }
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
                title: 'Reconnecting to ChipBit',
                body: 'Trying to reconnect. Hold tight.',
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
    _mutation_lock: threading.Lock = field(
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
        body = dedent("""
            <main class="kiosk-shell">
              <section class="kiosk-panel">
                <section class="kiosk-copy">
                  <p class="kiosk-brand">ChipBit</p>
                  <h1 class="kiosk-title" id="kiosk-title">Tap a card</h1>
                  <p class="kiosk-body" id="kiosk-body">
                    Waiting for a game card, Home card, or admin card.
                  </p>
                  <div class="kiosk-spinner" id="kiosk-spinner"></div>
                </section>
                <section class="kiosk-art-frame" id="kiosk-art-frame" hidden>
                  <img class="kiosk-art" id="kiosk-art" alt=""
                       onerror="this.src='/art/default';this.onerror=null;" />
                </section>
              </section>
            </main>
            """).strip()
        return self._kiosk_layout(body)

    def _kiosk_layout(self, body: str) -> str:
        return dedent(f"""<!doctype html>
            <html lang="en">
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
            {KIOSK_EVENTS_SCRIPT}
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

    def enroll_admin(self) -> str:
        with self._mutation_lock:
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
                    )
            finally:
                self._clear_operation_state()

        self._clear_readiness_cache()
        self.control.reload()
        if progress:
            return progress[-1].message
        normalized_uid = normalize_uid(uid)
        return f"Bound {normalized_uid} to {title.id}"

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

    def render_country_picker(self, *, error: str = "") -> str:
        """First-run country selection page — shown before WiFi setup."""
        flash = self._flash("", error)
        options = "\n".join(
            f'<option value="{c}">{escape(name)}</option>'
            for c, name in _WIFI_COUNTRIES
        )
        return dedent(f"""
            <!doctype html>
            <html lang="en">
            <head>
              <meta charset="utf-8">
              <meta name="viewport" content="width=device-width,initial-scale=1">
              <title>ChipBit Setup</title>
              <style>{PAGE_CSS}</style>
            </head>
            <body>
              <main>
                <header class="site-header">
                  <h1 class="site-title">ChipBit Setup</h1>
                </header>
                {flash}
                <section class="panel panel-wide">
                  <h2>Wi-Fi Country</h2>
                  <p>
                    Choose the country where this ChipBit is being used.
                    This sets the Wi-Fi radio channels available on your network.
                    The device will reboot once to apply the setting.
                  </p>
                  <form method="post" action="/setup/country">
                    <label>Country
                      <select name="country" required>
                        <option value="" disabled selected>Select a country…</option>
                        {options}
                      </select>
                    </label>
                    <button type="submit">Set country and reboot</button>
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
            <html lang="en">
            <head>
              <meta charset="utf-8">
              <meta name="viewport" content="width=device-width,initial-scale=1">
              <meta http-equiv="refresh" content="20;url=/setup">
              <title>ChipBit — Rebooting</title>
              <style>__PAGE_CSS__</style>
            </head>
            <body>
              <main>
                <header class="site-header">
                  <h1 class="site-title">ChipBit Setup</h1>
                </header>
                <section class="panel panel-wide">
                  <h2>Rebooting…</h2>
                  <p>Applying Wi-Fi country settings and rebooting.
                     This page will reload automatically in about 20 seconds.</p>
                  <div class="connecting-box">
                    <div class="spinner"></div>
                  </div>
                </section>
              </main>
            </body>
            </html>
        """.replace("__PAGE_CSS__", PAGE_CSS)).strip()

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
            <html lang="en">
            <head>
              <meta charset="utf-8">
              <meta name="viewport" content="width=device-width,initial-scale=1">
              <title>ChipBit Setup</title>
              <style>{PAGE_CSS}</style>
            </head>
            <body>
              <main>
                <header class="site-header">
                  <h1 class="site-title">ChipBit Setup</h1>
                </header>
                {flash}
                <section class="panel panel-wide">
                  <h2>Connect to Wi-Fi</h2>
                  <p>
                    Some activities (like Marble, KStars, and SuperTux) download and
                    install when a card is first enrolled. They need an internet
                    connection the first time. You can skip this and connect later
                    in Settings.
                  </p>
                  <p id="wifi-connect-error" class="flash error" hidden></p>
                  <form id="wifi-form" method="post"
                    action="/setup/wifi" class="wifi-form">
                    <label>Network
                      <select id="ssid-select" name="ssid" required>
                        <option value="" disabled selected>Scanning…</option>
                      </select>
                    </label>
                    <div id="ssid-manual-row" hidden>
                      <label>Network name
                        <input type="text" id="ssid-manual-input" autocomplete="off" />
                      </label>
                    </div>
                    <label>Password
                      <input type="password" name="password" />
                    </label>
                    <button type="submit">Connect and continue</button>
                  </form>
                  <div id="wifi-connecting" class="connecting-box" hidden>
                    <div class="spinner"></div>
                    <p id="wifi-connect-msg">Connecting…</p>
                  </div>
                  <script>{WIFI_SCAN_SCRIPT}</script>
                  <script>{WIFI_CONNECT_SCRIPT}</script>
                  <p class="muted">
                    <a href="/setup/skip">Skip — I'll connect later</a>
                    &nbsp;·&nbsp;
                    <a href="/debug">Diagnostics</a>
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
        return f"""<!doctype html><html lang="en"><head>
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
        return f'Added “{label}” — use “Tap card to enroll” in the grid above to assign a card'

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
                f'<button type="submit">Mount: {escape(label)}</button>'
                f"</form></li>"
            )
        if not items:
            items.append(
                "<li>No drives detected. Plug in a drive and click Rescan.</li>"
            )
        drive_list = "\n".join(items)

        section = dedent(f"""
            <section class="panel">
              <p class="eyebrow"><a href="/">&#8592; Parent Console</a></p>
              <h1>Game files</h1>
              <p class="muted">Browse a drive and copy game data to <code>/games/</code>.</p>
            </section>
            <section class="panel panel-wide">
              <h2>Drives</h2>
              <p><a href="/files">Rescan</a></p>
              <ul class="file-list">
                {drive_list}
              </ul>
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
        crumb_parts: list[str] = ['<a href="/files">Drives</a>']
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
            up_link = f'<p><a href="/files/browse?p={quote(str(parent))}">[up]</a></p>'
        else:
            up_link = f'<p><a href="/files">[up — drives]</a></p>'

        # Copy-this-folder form (copies the current directory)
        suggested = p.name.lower().replace(" ", "-")
        copy_form = dedent(f"""
            <section class="panel panel-wide">
              <h2>Copy this folder to /games/</h2>
              <form method="post" action="/files/copy" class="wifi-form">
                <input type="hidden" name="source" value="{escape(str(p))}" />
                <input type="hidden" name="back" value="{escape(str(p))}" />
                <label>Game type
                  <select id="copy-type">
                    <option value="scummvm">ScummVM</option>
                    <option value="dosbox">DOSBox</option>
                    <option value="flash">Flash / Ruffle</option>
                    <option value="">Other</option>
                  </select>
                </label>
                <label>Destination in /games/
                  <input type="text" name="dest" id="copy-dest"
                         value="scummvm/{escape(suggested)}"
                         placeholder="scummvm/monkey" required />
                </label>
                <button type="submit">Copy folder</button>
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

        listing = "\n".join(rows) if rows else "<li><em>Empty folder</em></li>"

        section = dedent(f"""
            <section class="panel">
              <p class="eyebrow">{breadcrumb}</p>
              <h1>{escape(p.name)}</h1>
            </section>
            {copy_form}
            <section class="panel panel-wide">
              <h2>Contents</h2>
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
                self._flash(None, "Unknown copy job — it may have already completed.")
                + '<section class="panel"><p><a href="/files">Back to drives</a></p></section>',
                include_events=False,
            )

        if not job["done"]:
            status_url = f"/files/copy/status?job={quote(job_id)}"
            body = dedent(f"""
                <section class="panel panel-wide">
                  <h2>Copying&hellip;</h2>
                  <div class="spinner"></div>
                  <p class="muted">This may take a minute for large game data.</p>
                </section>
                """).strip()
            return self._layout(
                "Copying… — ChipBit",
                body,
                include_events=False,
                head_extra=f'<meta http-equiv="refresh" content="2; url={escape(status_url)}" />',
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
                + f'<section class="panel"><p><a href="{escape(back_url)}">Back</a></p></section>',
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
            '<section class="panel"><p>Copy complete. Taking you to the card form&hellip;</p></section>',
            include_events=False,
            head_extra=f'<meta http-equiv="refresh" content="0; url=/?{escape(qs)}" />',
        )

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
                ["lsblk", "-J", "-p", "-o", "NAME,LABEL,FSTYPE,MOUNTPOINT,TYPE,HOTPLUG"],
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

    def _guess_prefill(self, dest_rel: str, games_root: Path | None = None) -> dict[str, str]:
        """Return form prefill hints based on the destination path."""
        p = Path(dest_rel)
        label = p.name.replace("-", " ").replace("_", " ").title()
        parts = p.parts
        if parts and parts[0] == "scummvm":
            pf: dict[str, str] = {"type": "scummvm", "data_dir": dest_rel, "label": label}
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
                    inner = sorted(f for f in target.rglob("*") if f.suffix.lower() == ".swf")
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
        """Write a minimal dosbox.conf next to game_dir; return conf path relative to games_root."""
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
        section = dedent("""
            <section class="panel panel-wide">
              <p class="eyebrow">First Run</p>
              <h1>Make this card the admin card</h1>
              <p>
                Hold any card to the reader now. No button needed — this page
                will update automatically when the card is registered.
              </p>
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
        playing = f"<p>Now playing: <strong>{current}</strong></p>" if current else ""
        section = dedent(f"""
            <section class="panel panel-wide">
              <p class="eyebrow">Parent Controls</p>
              <h1>Tap your admin card to unlock</h1>
              <p>
                Hold the card you registered as admin to the reader.
                This page will update automatically.
              </p>
              {playing}
              <p class="muted" style="margin-top:1.5rem">
                <a href="/debug">Diagnostics</a>
                &nbsp;·&nbsp;
                <a href="/setup">WiFi setup</a>
                &nbsp;·&nbsp;
                <form method="post" action="/settings/shutdown" style="display:inline">
                  <button type="submit" class="link-button">Shut down</button>
                </form>
              </p>
            </section>
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
            card_rows = '<tr><td colspan="4">No game cards enrolled yet.</td></tr>'

        admin_uid = escape(cards.system_cards["unlock"].uid)
        section = dedent(f"""
            <div id="op-overlay" class="op-overlay" hidden>
              <div class="spinner"></div>
              <div class="op-overlay-text">
                <strong id="op-overlay-title"></strong>
                <span id="op-overlay-msg"></span>
              </div>
              <button id="op-overlay-dismiss" class="op-overlay-dismiss" type="button" hidden title="Dismiss">&times;</button>
            </div>
            <div id="tap-now-banner" class="flash ok" style="display:none">
              Hold your card to the reader now — waiting up to 30 seconds.
            </div>
            <script>
              function exitAdmin() {{
                fetch('/settings/lock', {{method: 'POST'}})
                  .then(() => location.replace('/kiosk'));
              }}
            </script>
            <section class="panel">
              <p class="eyebrow">Parent Console</p>
              <h1>Game cards</h1>
              <p class="muted">Admin card UID: <code>{admin_uid}</code></p>
              <button type="button" onclick="exitAdmin()">← Back to play mode</button>
            </section>
            <section class="catalog-grid">{title_rows}</section>
            <section class="panel panel-wide">
              <h2>Enrolled cards</h2>
              <table>
                <thead>
                  <tr>
                    <th>UID</th>
                    <th>Title</th>
                    <th>Reassign</th>
                    <th>Disable</th>
                  </tr>
                </thead>
                <tbody>{card_rows}</tbody>
              </table>
            </section>
            <section class="panel panel-wide">
              <h2>Game files</h2>
              <p class="muted">Copy game data from a USB drive to <code>/games/</code>
                so ScummVM, DOSBox, and Ruffle titles can find it.</p>
              <a href="/files">Open file browser →</a>
            </section>
            <section class="panel panel-wide">
              <h2>Add a custom card</h2>
              <p class="muted">
                Create cards for software you own or websites you'd
                like your child to visit.
                After saving, use "Tap card to enroll" in the grid
                above to assign an RFID card.
              </p>
              <details id="custom-web">
                <summary>Add a website</summary>
                <form method="post" action="/titles/custom" class="wifi-form">
                  <input type="hidden" name="type" value="web" />
                  <label>Name
                    <input type="text" name="label"
                      placeholder="My Website" required /></label>
                  <label>URL
                    <input type="url" name="url"
                      placeholder="https://example.com" required /></label>
                  <button type="submit">Save website card</button>
                </form>
              </details>
              <details id="custom-exec">
                <summary>Add an installed app</summary>
                <form method="post" action="/titles/custom" class="wifi-form">
                  <input type="hidden" name="type" value="exec" />
                  <label>Name
                    <input type="text" name="label"
                      placeholder="My App" required /></label>
                  <label>Launch command
                    <input type="text" name="cmd"
                      placeholder="myapp --fullscreen" required /></label>
                  <label>Apt package to install (optional)
                    <input type="text" name="apt"
                      placeholder="my-package" /></label>
                  <button type="submit">Save app card</button>
                </form>
              </details>
              <details id="custom-scummvm">
                <summary>Add a ScummVM game (you supply game data)</summary>
                <form method="post" action="/titles/custom" class="wifi-form">
                  <input type="hidden" name="type" value="scummvm" />
                  <label>Name
                    <input type="text" name="label"
                      placeholder="Monkey Island" required /></label>
                  <label>
                    ScummVM game ID
                    <input type="text" name="game_id" placeholder="monkey" required />
                  </label>
                  <label>
                    Data folder under /games/ (leave blank for scummvm/&lt;name&gt;)
                    <input type="text" name="data_dir" placeholder="scummvm/monkey" />
                  </label>
                  <button type="submit">Save ScummVM card</button>
                </form>
              </details>
              <details id="custom-dosbox">
                <summary>Add a DOSBox game (you supply game data)</summary>
                <form method="post" action="/titles/custom" class="wifi-form">
                  <input type="hidden" name="type" value="dosbox" />
                  <label>Name
                    <input type="text" name="label"
                      placeholder="My DOS Game" required /></label>
                  <label>
                    DOSBox config file path under /games/
                    <input type="text" name="conf"
                      placeholder="mygame/dosbox.conf" required />
                  </label>
                  <button type="submit">Save DOSBox card</button>
                </form>
              </details>
              <details id="custom-ruffle">
                <summary>Add a Flash/Ruffle game (you supply the .swf)</summary>
                <form method="post" action="/titles/custom" class="wifi-form">
                  <input type="hidden" name="type" value="ruffle" />
                  <label>Name
                    <input type="text" name="label"
                      placeholder="Math Blaster" required /></label>
                  <label>
                    SWF path under /games/
                    <input type="text" name="swf"
                      placeholder="flash/mathblaster.swf" required />
                  </label>
                  <button type="submit">Save Ruffle card</button>
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
            <section class="panel panel-wide">
              <h2>Settings</h2>
              <h3>Wi-Fi</h3>
              <form method="post" action="/wifi/connect" class="wifi-form">
                <label>Network
                  <select id="ssid-select" name="ssid" required>
                    <option value="" disabled selected>Scanning…</option>
                  </select>
                </label>
                <div id="ssid-manual-row" hidden>
                  <label>Network name
                    <input type="text" id="ssid-manual-input"
                           autocomplete="off" />
                  </label>
                </div>
                <label>Password
                  <input type="password" name="password" />
                </label>
                <button type="submit">Connect</button>
              </form>
              <script>{WIFI_SCAN_SCRIPT}</script>
              <p class="muted"><a href="/debug">Diagnostics</a></p>
              <h3>Keyboard layout</h3>
              <form method="post" action="/settings/keyboard" class="inline-form">
                <select name="layout">
                  <option value="us">US (QWERTY)</option>
                  <option value="gb">UK (QWERTY)</option>
                  <option value="de">German (QWERTZ)</option>
                  <option value="fr">French (AZERTY)</option>
                  <option value="es">Spanish</option>
                  <option value="it">Italian</option>
                  <option value="pt">Portuguese</option>
                  <option value="nl">Dutch</option>
                </select>
                <button type="submit">Apply keyboard layout</button>
              </form>
              <h3>System</h3>
              <form method="post" action="/settings/lock" class="inline-form">
                <button type="submit">Lock parent controls</button>
              </form>
              <form method="post" action="/settings/shutdown" class="inline-form"
                    onsubmit="return confirm('Shut down the Pi now?')">
                <button type="submit">Shut down</button>
              </form>
            </section>
            """).strip()
        return f"{self._flash(message, error)}{section}"

    def _render_title_card(
        self,
        title: CatalogTitle,
        catalog: Catalog,
        bindings_by_title: dict[str, list[str]],
    ) -> str:
        summary = escape(self._title_summary(title, catalog))
        raw_state = self._title_state(title, catalog)
        state = escape(raw_state)
        bound_cards = ", ".join(sorted(bindings_by_title.get(title.id, [])))
        bound_cards = escape(bound_cards or "none")
        action = quote(title.id)
        return dedent(f"""
            <article class="title-card">
              <h2>{escape(title.label)}</h2>
              <p>{summary}</p>
              <p class="muted">{state}</p>
              <p class="muted">Bound cards: {bound_cards}</p>
              <form method="post" action="/titles/{action}/enroll" class="enroll-form">
                <button type="submit">Tap card to enroll</button>
              </form>
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
        return dedent(f"""
            <tr>
              <td>{escape(uid)}</td>
              <td>{escape(title_id)}</td>
              <td>
                <form
                  method="post"
                  action="/cards/{quoted_uid}/reassign"
                  class="inline-form"
                >
                  <select name="title_id">{options}</select>
                  <button type="submit">Reassign</button>
                </form>
              </td>
              <td>
                <form
                  method="post"
                  action="/cards/{quoted_uid}/remove"
                  class="inline-form"
                >
                  <button type="submit">Disable</button>
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
        if title.data == "required":
            ready = self._required_data_ready(title, catalog)
            return "Data ready" if ready else "Needs parent-supplied data"
        if title.install:
            return "Installs on enroll"
        if title.bundled:
            return "Bundled in the image"
        return "Ready"

    def _kiosk_state(
        self,
        cards: CardsConfig,
        status: dict[str, object],
        operation: dict[str, str] | None,
    ) -> dict[str, str]:
        if status.get("capture_mode") is True:
            return {
                "kind": "enroll",
                "title": "Tap a card now",
                "body": "Enrollment is armed.",
            }

        if "unlock" not in cards.system_cards:
            return {
                "kind": "first-run",
                "title": "Tap a card to make it the admin card",
                "body": "No network needed for first-run setup.",
            }

        if operation is not None:
            return {
                "kind": operation["kind"],
                "title": operation["title"],
                "body": operation["message"],
                **({"art": operation["art"]} if "art" in operation else {}),
            }

        current = status.get("current")
        if status.get("running") is True and isinstance(current, str) and current:
            kiosk = {
                "kind": "loading",
                "title": current,
                "body": "Launching now.",
            }
            current_art = status.get("current_art")
            kiosk["art"] = (
                current_art if isinstance(current_art, str) and current_art
                else "/art/default"
            )
            return kiosk

        last_event = status.get("last_event")
        if isinstance(last_event, dict) and last_event.get("kind") == "unknown-card":
            uid = last_event.get("uid", "")
            body = (
                f"Card {uid} is not set up yet. Enroll it in the parent console."
                if uid else "That card is not set up yet."
            )
            return {
                "kind": "unknown-card",
                "title": "Ask a grown-up",
                "body": body,
            }

        return {
            "kind": "idle",
            "title": "Tap a card",
            "body": "Waiting for a game card, Home card, or admin card.",
        }

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
    ) -> None:
        operation = {
            "kind": "loading",
            "title": title,
            "message": message,
        }
        if art:
            operation["art"] = art
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
        script_tag = ""
        if script_body:
            script_tag = f"<script>\n{script_body}\n</script>"

        return dedent(f"""<!doctype html>
            <html lang="en">
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
              <main>
                <header>
                  <div>
                    <p class="eyebrow">ChipBit</p>
                    <h1>{escape(title)}</h1>
                  </div>
                                    <div>
                                        <p>
                                            Live mode:
                                            <strong id="live-mode">loading</strong>
                                        </p>
                                        <p class="muted" id="live-detail"></p>
                                    </div>
                </header>
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
                    self._send_html(200, app.render_index())
                    return
                if path == "/setup":
                    if not _WIFI_COUNTRY_FILE.exists():
                        self._send_html(200, app.render_country_picker())
                    else:
                        qs = parse_qs(urlparse(self.path).query)
                        msg = "Connected to Wi-Fi." if qs.get("connected") else ""
                        self._send_html(200, app.render_setup(message=msg))
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
                if not rel or ".." in rel.split("/") or not rel.lower().endswith(".swf"):
                    self.send_response(403)
                    self.end_headers()
                    return
                resolved = (games_root / rel).resolve()
                if not str(resolved).startswith(str(games_root.resolve())):
                    self.send_response(403)
                    self.end_headers()
                    return
                if not resolved.exists():
                    log.warning("SWF not found: %s (games_root=%s)", resolved, games_root)
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
                elif path == "/settings/lock":
                    message = app.lock_controls()
                elif path == "/settings/shutdown":
                    message = app.shutdown_system()
                elif path == "/wifi/connect":
                    message = app.configure_wifi(
                        form.get("ssid", ""),
                        form.get("password"),
                    )
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
