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
    load_catalog,
    normalize_uid,
    save_cards,
)

log = logging.getLogger(__name__)

DEFAULT_EVENT_POLL_SECS = 1.0

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
    """).strip()

PARENT_EVENTS_SCRIPT = dedent("""
    const events = new EventSource('/events');
    events.onmessage = (event) => {
      const state = JSON.parse(event.data);
      const badge = document.getElementById('live-mode');
      if (badge) {
        badge.textContent = state.mode;
      }
    };
    """).strip()

KIOSK_EVENTS_SCRIPT = dedent("""
    const title = document.getElementById('kiosk-title');
    const body = document.getElementById('kiosk-body');
    const events = new EventSource('/events');
    events.onmessage = (event) => {
      const state = JSON.parse(event.data);
            const badge = document.getElementById('live-mode');
            if (badge && state.kiosk) {
                badge.textContent = state.kiosk.kind;
      }
            if (!state.kiosk) {
                return;
            }
            title.textContent = state.kiosk.title;
            body.textContent = state.kiosk.body;
    };
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

    def capture(self) -> str:
        payload = self._request_json("POST", "/capture")
        uid = payload.get("uid")
        if not isinstance(uid, str) or not uid:
            raise ControlApiError("daemon capture returned an invalid uid")
        return uid

    def _request_json(self, method: str, path: str) -> dict[str, object]:
        request = Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=b"" if method == "POST" else None,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_secs) as response:
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

    def render_index(
        self,
        *,
        message: str | None = None,
        error: str | None = None,
    ) -> str:
        catalog = self._load_catalog()
        cards = load_cards(self.cards_path)
        status = self.control.status()
        mode = self._mode(cards, status)

        if mode == "first-run":
            body = self._render_first_run(message=message, error=error)
        elif mode == "locked":
            body = self._render_locked(status=status, message=message, error=error)
        else:
            body = self._render_unlocked(
                catalog=catalog,
                cards=cards,
                status=status,
                message=message,
                error=error,
            )

        return self._layout("ChipBit Parent Console", body, include_events=True)

    def render_kiosk(self) -> str:
        body = dedent("""
            <section class="panel panel-wide">
              <p class="eyebrow">ChipBit</p>
              <h1 id="kiosk-title">Tap a card</h1>
              <p id="kiosk-body">
                Waiting for a game card, Home card, or admin card.
              </p>
            </section>
            """).strip()
        return self._layout(
            "ChipBit Kiosk",
            body,
            include_events=False,
            script=KIOSK_EVENTS_SCRIPT,
        )

    def event_payload(self) -> dict[str, object]:
        cards = load_cards(self.cards_path)
        status = self.control.status()
        return {
            "mode": self._mode(cards, status),
            "status": status,
            "kiosk": self._kiosk_state(cards, status),
            "has_admin_card": "unlock" in cards.system_cards,
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
        return self.enroll_title_for_uid(normalized_uid, title_id)

    def enroll_title_for_uid(self, uid: str, title_id: str) -> str:
        with self._mutation_lock:
            catalog = self._load_catalog()
            cards = load_cards(self.cards_path)
            self._require_unlocked(cards)

            title = catalog.titles.get(title_id)
            if title is None:
                raise ValueError(f"unknown title: {title_id}")

            progress = list(
                enroll_card(
                    uid,
                    title,
                    cards_path=self.cards_path,
                    games_root=catalog.settings.games_root,
                    runner=self.runner,
                    network_checker=self.network_checker,
                    scummvm_executable=self.scummvm_executable,
                )
            )

        self._clear_readiness_cache()
        self.control.reload()
        self.control.lock()
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

    def configure_wifi(self, ssid: str, password: str | None) -> str:
        cards = load_cards(self.cards_path)
        self._require_unlocked(cards)

        normalized_ssid = ssid.strip()
        if not normalized_ssid:
            raise ValueError("ssid is required")

        argv = ["nmcli", "device", "wifi", "connect", normalized_ssid]
        if password:
            argv.extend(["password", password])
        result = self.runner(argv, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise RuntimeError(f"Wi-Fi setup failed: {message}")
        self.control.lock()
        return f"Connected Wi-Fi to {normalized_ssid}"

    def _require_unlocked(self, cards: CardsConfig) -> None:
        if "unlock" not in cards.system_cards:
            raise PermissionError("no admin card is enrolled yet")
        status = self.control.status()
        if status.get("unlocked") is not True:
            raise PermissionError("tap the admin card to unlock configuration")

    def _load_catalog(self) -> Catalog:
        return load_catalog(self.catalog_path)

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
                No network is required. Tap one card and ChipBit will use it to
                unlock parent controls.
              </p>
              <form method="post" action="/admin/enroll">
                <button type="submit">Tap a card to enroll admin</button>
              </form>
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
        running = "yes" if status.get("running") else "no"
        current = escape(str(status.get("current") or "Idle"))
        section = dedent(f"""
            <section class="panel panel-wide">
              <p class="eyebrow">Locked</p>
              <h1>Tap the admin card</h1>
              <p>
                Parent controls stay locked until the daemon reports
                <code>status.unlocked = true</code>.
              </p>
              <dl class="stats">
                <div><dt>Running</dt><dd>{running}</dd></div>
                <div><dt>Current</dt><dd>{current}</dd></div>
              </dl>
            </section>
            """).strip()
        return f"{self._flash(message, error)}{section}"

    def _render_unlocked(
        self,
        *,
        catalog: Catalog,
        cards: CardsConfig,
        status: dict[str, object],
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

        running = "yes" if status.get("running") else "no"
        current = escape(str(status.get("current") or "Idle"))
        admin_uid = escape(cards.system_cards["unlock"].uid)
        section = dedent(f"""
            <section class="panel">
              <p class="eyebrow">Parent Console</p>
              <h1>Catalog</h1>
              <p>Admin card: <strong>{admin_uid}</strong></p>
              <dl class="stats">
                <div><dt>Running</dt><dd>{running}</dd></div>
                <div><dt>Current</dt><dd>{current}</dd></div>
                <div><dt>Titles</dt><dd>{len(catalog.titles)}</dd></div>
                <div><dt>Cards</dt><dd>{len(cards.title_cards)}</dd></div>
              </dl>
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
              <h2>Settings</h2>
              <form
                method="post"
                action="/settings/reload"
                class="inline-form"
              >
                <button type="submit">Reload daemon config</button>
              </form>
              <form method="post" action="/wifi/connect" class="wifi-form">
                <label>SSID <input type="text" name="ssid" /></label>
                <label>
                  Password
                  <input type="password" name="password" />
                </label>
                <button type="submit">Connect Wi-Fi</button>
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
              <form method="post" action="/titles/{action}/enroll">
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
    ) -> dict[str, str]:
        if status.get("capture_mode") is True:
            return {
                "kind": "enroll",
                "title": "Tap a card now",
                "body": "Enrollment is armed.",
            }

        current = status.get("current")
        if status.get("running") is True and isinstance(current, str) and current:
            return {
                "kind": "loading",
                "title": current,
                "body": "Launching now.",
            }

        last_event = status.get("last_event")
        if isinstance(last_event, dict) and last_event.get("kind") == "unknown-card":
            return {
                "kind": "unknown-card",
                "title": "Ask a grown-up",
                "body": "That card is not set up yet.",
            }

        if "unlock" not in cards.system_cards:
            return {
                "kind": "first-run",
                "title": "Tap a card to make it the admin card",
                "body": "No network needed for first-run setup.",
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
                  <p>Live mode: <strong id="live-mode">loading</strong></p>
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
) -> ThreadingHTTPServer:
    """Create the plain-HTML parent console and kiosk shell server."""
    app = WebApp(
        catalog_path=catalog_path,
        cards_path=cards_path,
        control=ControlClient(control_base_url),
        runner=runner,
        network_checker=network_checker,
        scummvm_executable=scummvm_executable,
        event_poll_secs=event_poll_secs,
    )

    class Handler(BaseHTTPRequestHandler):
        def _send_html(self, code: int, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

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
                if path == "/":
                    self._send_html(200, app.render_index())
                    return
                if path == "/kiosk":
                    self._send_html(200, app.render_kiosk())
                    return
                if path == "/events":
                    self._serve_events()
                    return
            except (ConfigLoadError, ControlApiError) as exc:
                self._send_html(502, app.render_index(error=str(exc)))
                return

            self._send_html(404, app.render_index(error="not found"))

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            form = self._read_form()
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
                elif path == "/wifi/connect":
                    message = app.configure_wifi(
                        form.get("ssid", ""),
                        form.get("password"),
                    )
                else:
                    self._send_html(404, app.render_index(error="not found"))
                    return
            except PermissionError as exc:
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
                self._send_html(400, app.render_index(error=str(exc)))
                return

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
                    except (ConfigLoadError, ControlApiError) as exc:
                        payload_obj = {
                            "mode": "error",
                            "error": str(exc),
                            "status": {
                                "running": False,
                                "current": None,
                                "unlocked": False,
                                "capture_mode": False,
                                "last_event": None,
                            },
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
