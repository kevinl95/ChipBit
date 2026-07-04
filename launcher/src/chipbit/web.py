"""Plain-HTML web service and kiosk shell for the ChipBit runtime."""

from __future__ import annotations

import json
import logging
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
    var badge = document.getElementById('live-mode');
    var tapBanner = document.getElementById('tap-now-banner');

    var enrollInProgress = false;
    var overlayPinned = false;  // true while an error is displayed; SSE won't clear it

    function showOverlay(title, msg) {
      if (overlayTitle) overlayTitle.textContent = title;
      if (overlayMsg) overlayMsg.textContent = msg;
      if (overlay) overlay.hidden = false;
    }
    function hideOverlay() {
      if (overlay) overlay.hidden = true;
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
              showOverlay('Enrollment failed', data.error || 'Something went wrong.');
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
            window.location.replace('/');
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
              connectMsg.textContent = 'Connected! Taking you to the admin page…';
              setTimeout(function () { window.location.replace('/'); }, 1200);
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

    def configure_wifi(self, ssid: str, password: str | None) -> str:
        cards = load_cards(self.cards_path)
        self._require_unlocked(cards)

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
        self.control.lock()
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
                    <a href="/">Skip — I'll connect later</a>
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
                bundled=False, data="required", game_id=game_id,
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
        self.control.lock()
        return f"Added “{label}” — tap “Tap card to enroll” to assign a card"

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
              <h2>Add a custom card</h2>
              <p class="muted">
                Create cards for software you own or websites you'd
                like your child to visit.
                After saving, use "Tap card to enroll" in the grid
                above to assign an RFID card.
              </p>
              <details>
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
              <details>
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
              <details>
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
              <details>
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
            </section>
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
        state = escape(self._title_state(title, catalog))
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
) -> ThreadingHTTPServer:
    """Create the plain-HTML parent console and kiosk shell server."""
    art_root = catalog_path.parent / "art"
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
                    self._send_html(200, app.render_setup())
                    return
                if path == "/kiosk":
                    cards = load_cards(app.cards_path)
                    if "unlock" not in cards.system_cards:
                        self._redirect("/admin")
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
            except (ConfigLoadError, ControlApiError) as exc:
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

            self._send_html(404, app.render_index(error="not found"))

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
            if path == "/setup/wifi":
                try:
                    app.configure_wifi(form.get("ssid", ""), form.get("password"))
                    try:
                        app.control.unlock()
                    except Exception:
                        pass
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
