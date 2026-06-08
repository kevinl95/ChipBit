from __future__ import annotations

import json
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from chipbit.models import load_cards
from chipbit.web import create_web_server


@dataclass
class FakeControlState:
    unlocked: bool = False
    capture_uid: str = "AABBCC"
    capture_mode: bool = False
    reload_calls: int = 0
    capture_calls: int = 0
    lock_calls: int = 0

    def status_payload(self) -> dict[str, object]:
        return {
            "running": False,
            "current": None,
            "unlocked": self.unlocked,
            "cards": 0,
            "capture_mode": self.capture_mode,
        }


def test_first_run_requires_admin_enrollment_before_anything_else(
    tmp_path: Path,
) -> None:
    catalog_path = write_catalog(tmp_path)
    cards_path = tmp_path / "cards.yaml"
    control_state = FakeControlState(unlocked=False)

    with run_control_server(control_state) as control_url:
        with run_web_server(catalog_path, cards_path, control_url) as web_url:
            body = http_get(f"{web_url}/")

    assert "Make this card the admin card" in body
    assert "Demo App" not in body
    assert "Wi-Fi" not in body


def test_admin_enrollment_and_title_enrollment_work_against_mock_daemon(
    tmp_path: Path,
) -> None:
    catalog_path = write_catalog(tmp_path)
    cards_path = tmp_path / "cards.yaml"
    control_state = FakeControlState(unlocked=False, capture_uid="11-22-33")

    with run_control_server(control_state) as control_url:
        with run_web_server(catalog_path, cards_path, control_url) as web_url:
            http_post(f"{web_url}/admin/enroll", {})

            cards = load_cards(cards_path)
            assert cards.system_cards["unlock"].uid == "112233"
            assert control_state.reload_calls == 1

            control_state.unlocked = True
            control_state.capture_uid = "aa-bb-cc"

            body = http_post(f"{web_url}/titles/demo/enroll", {})

    cards = load_cards(cards_path)
    assert cards.title_cards["AABBCC"].title_id == "demo"
    assert control_state.reload_calls == 2
    assert control_state.lock_calls == 1
    assert control_state.unlocked is False
    assert "Bound AABBCC to demo" in body


def test_existing_admin_card_stays_locked_until_daemon_unlocks(tmp_path: Path) -> None:
    catalog_path = write_catalog(tmp_path)
    cards_path = tmp_path / "cards.yaml"
    cards_path.write_text(
        """
system:
  unlock: "ff-ee-dd"
""",
        encoding="utf-8",
    )
    control_state = FakeControlState(unlocked=False)

    with run_control_server(control_state) as control_url:
        with run_web_server(catalog_path, cards_path, control_url) as web_url:
            body = http_get(f"{web_url}/")

    assert "Tap the admin card" in body
    assert "Demo App" not in body


def test_locked_title_enroll_does_not_arm_capture(tmp_path: Path) -> None:
    catalog_path = write_catalog(tmp_path)
    cards_path = tmp_path / "cards.yaml"
    cards_path.write_text(
        """
system:
  unlock: "ff-ee-dd"
""",
        encoding="utf-8",
    )
    control_state = FakeControlState(unlocked=False)

    with run_control_server(control_state) as control_url:
        with run_web_server(catalog_path, cards_path, control_url) as web_url:
            with pytest.raises(HTTPError) as excinfo:
                http_post(f"{web_url}/titles/demo/enroll", {})

    assert excinfo.value.code == 403
    assert control_state.capture_calls == 0


def test_parent_console_caches_scummvm_readiness_between_renders(
    tmp_path: Path,
) -> None:
    games_root = tmp_path / "games"
    data_dir = games_root / "scummvm" / "puttmoon"
    data_dir.mkdir(parents=True)
    catalog_path = tmp_path / "catalog.yaml"
    cards_path = tmp_path / "cards.yaml"
    cards_path.write_text(
        """
system:
  unlock: "ff-ee-dd"
""",
        encoding="utf-8",
    )
    catalog_path.write_text(
        f"""
meta:
  catalog_version: 1
settings:
  games_root: {games_root}
titles:
  - id: puttmoon
    label: Putt-Putt
    type: scummvm
    bundled: false
    data: required
    game_id: puttmoon
""",
        encoding="utf-8",
    )
    control_state = FakeControlState(unlocked=True)
    runner = DetectRunner()

    with run_control_server(control_state) as control_url:
        with run_web_server(
            catalog_path,
            cards_path,
            control_url,
            runner=runner,
        ) as web_url:
            http_get(f"{web_url}/")
            http_get(f"{web_url}/")

    assert runner.calls == [
        ["scummvm", "--detect", f"--path={data_dir}"],
    ]


@contextmanager
def run_control_server(state: FakeControlState):
    def make_handler():
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
                    self._send(200, state.status_payload())
                    return
                if self.path == "/cards":
                    self._send(200, {"cards": {}, "system": {}})
                    return
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path == "/capture":
                    state.capture_calls += 1
                    self._send(200, {"uid": state.capture_uid})
                    return
                if self.path == "/reload":
                    state.reload_calls += 1
                    self._send(200, {"reloaded": True, "cards": 0, "system_cards": 0})
                    return
                if self.path == "/lock":
                    state.lock_calls += 1
                    state.unlocked = False
                    self._send(200, {"locked": True})
                    return
                self._send(404, {"error": "not found"})

            def log_message(self, format: str, *args: object) -> None:  # noqa: A003
                return None

        return Handler

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


@contextmanager
def run_web_server(
    catalog_path: Path,
    cards_path: Path,
    control_url: str,
    *,
    runner=None,
):
    server = create_web_server(
        "127.0.0.1",
        0,
        catalog_path=catalog_path,
        cards_path=cards_path,
        control_base_url=control_url,
        runner=subprocess.run if runner is None else runner,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


def write_catalog(tmp_path: Path) -> Path:
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(
        """
meta:
  catalog_version: 1
settings:
  games_root: /games
titles:
  - id: demo
    label: Demo App
    type: exec
    bundled: true
    cmd: [demo-app, --fullscreen]
""",
        encoding="utf-8",
    )
    return catalog_path


def http_get(url: str) -> str:
    with urlopen(url) as response:
        return response.read().decode("utf-8")


def http_post(url: str, form: dict[str, str]) -> str:
    data = urlencode(form).encode("utf-8")
    request = Request(url, data=data, method="POST")
    with urlopen(request) as response:
        return response.read().decode("utf-8")


class DetectRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs):
        self.calls.append(list(argv))
        assert kwargs == {"check": False, "capture_output": True, "text": True}
        return subprocess.CompletedProcess(
            argv,
            0,
            "puttmoon: Putt-Putt Goes to the Moon\n",
            "",
        )
