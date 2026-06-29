"""HTTP control API for the ChipBit launcher daemon."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .launcher import LauncherService


def make_handler(service: LauncherService):
    """Create a request handler bound to the current launcher service."""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/status":
                self._send(200, service.status())
                return
            if self.path == "/cards":
                self._send(200, service.cards_snapshot())
                return
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/capture":
                uid = service.capture()
                if uid is None:
                    self._send(408, {"error": "no card within timeout"})
                else:
                    self._send(200, {"uid": uid})
                return

            if self.path == "/reload":
                changed = service.reload(force=True)
                cards = service.cards_snapshot()
                self._send(
                    200,
                    {
                        "reloaded": changed,
                        "cards": len(cards["cards"]),
                        "system_cards": len(cards["system"]),
                    },
                )
                return

            if self.path == "/lock":
                service.lock()
                self._send(200, {"locked": True})
                return

            if self.path == "/unlock":
                service.unlock()
                self._send(200, {"unlocked": True})
                return

            self._send(404, {"error": "not found"})

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return None

    return Handler


def create_control_server(
    host: str,
    port: int,
    service: LauncherService,
) -> ThreadingHTTPServer:
    """Create the localhost control API server."""
    return ThreadingHTTPServer((host, port), make_handler(service))
