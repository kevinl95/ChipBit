from __future__ import annotations

import json
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

import chipbit.web as web_module
from chipbit import strings
from chipbit.installer import InstallProgress
from chipbit.models import load_cards, load_catalog_merged
from chipbit.web import create_web_server


@pytest.fixture(autouse=True)
def _english_between_tests():
    """The active locale is module-global and the tests share a process, so a
    test that loads German would otherwise render every later test's pages in
    German."""
    strings.use_english()
    yield
    strings.use_english()


@dataclass
class FakeControlState:
    unlocked: bool = False
    capture_uid: str = "AABBCC"
    capture_mode: bool = False
    running: bool = False
    current: str | None = None
    current_art: str | None = None
    last_event: dict[str, object] | None = None
    reload_calls: int = 0
    capture_calls: int = 0
    lock_calls: int = 0

    def status_payload(self) -> dict[str, object]:
        return {
            "running": self.running,
            "current": self.current,
            "current_art": self.current_art,
            "unlocked": self.unlocked,
            "cards": 0,
            "capture_mode": self.capture_mode,
            "last_event": self.last_event,
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
    # No button — admin enrollment is driven by card tap, not form submit
    assert "enroll admin" not in body.lower()
    assert "Demo App" not in body
    assert "Wi-Fi" not in body


def test_first_run_sse_payload_includes_has_admin_card_flag(tmp_path: Path) -> None:
    catalog_path = write_catalog(tmp_path)
    cards_path = tmp_path / "cards.yaml"
    control_state = FakeControlState(unlocked=False)

    with run_control_server(control_state) as control_url:
        with run_web_server(catalog_path, cards_path, control_url) as web_url:
            payload = read_sse_payload(f"{web_url}/events")

    assert payload["has_admin_card"] is False
    assert payload["mode"] == "first-run"


def test_kiosk_first_run_prompt_wins_over_unknown_card_event(tmp_path: Path) -> None:
    catalog_path = write_catalog(tmp_path)
    cards_path = tmp_path / "cards.yaml"
    control_state = FakeControlState(
        unlocked=False,
        last_event={"kind": "unknown-card", "uid": "DEADBEEF"},
    )

    with run_control_server(control_state) as control_url:
        with run_web_server(catalog_path, cards_path, control_url) as web_url:
            payload = read_sse_payload(f"{web_url}/events")

    assert payload["kiosk"] == {
        "kind": "first-run",
        "title": "Tap a card to make it the admin card",
        "body": "No network needed for first-run setup.",
    }


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
    assert control_state.lock_calls == 0
    assert control_state.unlocked is True
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

    assert "Tap your admin card to unlock" in body
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


def test_kiosk_events_reflect_idle_enroll_loading_and_unknown_states(
    tmp_path: Path,
) -> None:
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
            idle = read_sse_payload(f"{web_url}/events")

            control_state.capture_mode = True
            enroll = read_sse_payload(f"{web_url}/events")

            control_state.capture_mode = False
            control_state.running = True
            control_state.current = "Demo App"
            control_state.current_art = "/art/demo-app.png"
            loading = read_sse_payload(f"{web_url}/events")

            control_state.running = False
            control_state.current = None
            control_state.current_art = None
            control_state.last_event = {
                "kind": "unknown-card",
                "uid": "DEADBEEF",
            }
            unknown = read_sse_payload(f"{web_url}/events")

    # Idle and first-run stay on paper; every other state carries the ink the
    # kiosk floods the screen with.
    assert idle["kiosk"] == {
        "kind": "idle",
        "title": "Tap a card",
        "body": "Hold a card against the reader to start playing.",
    }

    enroll_ink, enroll_on_ink = web_module.ink_for("enroll")
    assert enroll["kiosk"] == {
        "kind": "enroll",
        "title": "Tap a card now",
        "body": "This card is about to become a game card.",
        "ink": enroll_ink,
        "on_ink": enroll_on_ink,
    }

    # No current_id from this daemon, so the ink falls back to the label.
    loading_ink, loading_on_ink = web_module.ink_for("Demo App")
    assert loading["kiosk"] == {
        "kind": "loading",
        "title": "Demo App",
        "body": "Getting it ready\u2026",
        "art": "/art/demo-app.png",
        "ink": loading_ink,
        "on_ink": loading_on_ink,
    }
    assert unknown["kiosk"] == {
        "kind": "unknown-card",
        "title": "Ask a grown-up",
        "body": (
            "This card isn't set up yet. Card DEADBEEF can be added in the "
            "parent console."
        ),
        "ink": "#f0b429",
        "on_ink": "#1a1a19",
    }


def test_enroll_progress_is_visible_over_sse_while_post_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        """
meta:
  catalog_version: 1
settings:
  games_root: /games
titles:
  - id: marble
    label: Marble
    type: exec
    bundled: false
    install:
      apt: [marble]
    cmd: [marble]
    art: /art/marble.png
""",
        encoding="utf-8",
    )
    control_state = FakeControlState(unlocked=True, capture_uid="aa-bb-cc")
    installing = threading.Event()
    release = threading.Event()
    responses: list[str] = []

    def fake_enroll_card(uid: str, title, **kwargs):
        yield InstallProgress(
            step="installing",
            message="Installing Marble via apt",
            manager="apt",
            packages=("marble",),
        )
        installing.set()
        assert release.wait(timeout=1.0)
        yield InstallProgress(
            step="bound",
            message="Bound AABBCC to marble",
            packages=("marble",),
        )

    monkeypatch.setattr(web_module, "enroll_card", fake_enroll_card)

    with run_control_server(control_state) as control_url:
        with run_web_server(catalog_path, cards_path, control_url) as web_url:
            post_thread = threading.Thread(
                target=lambda: responses.append(
                    http_post(f"{web_url}/titles/marble/enroll", {})
                ),
                daemon=True,
            )
            post_thread.start()
            assert installing.wait(timeout=1.0)

            payload = read_sse_payload(f"{web_url}/events")

            release.set()
            post_thread.join(timeout=1.0)

    assert payload["operation"] == {
        "kind": "loading",
        "title": "Marble",
        "message": "Installing Marble via apt",
        "art": "/art/marble.png",
        # Carried so the kiosk can flood with this title's card ink.
        "id": "marble",
    }
    marble_ink, marble_on_ink = web_module.ink_for("marble")
    assert payload["kiosk"] == {
        "kind": "loading",
        "title": "Marble",
        "body": "Installing Marble via apt",
        "art": "/art/marble.png",
        "ink": marble_ink,
        "on_ink": marble_on_ink,
    }
    assert responses == [responses[0]]


def test_kiosk_redirects_to_admin_on_first_run(tmp_path: Path) -> None:
    catalog_path = write_catalog(tmp_path)
    cards_path = tmp_path / "cards.yaml"
    control_state = FakeControlState(unlocked=False)

    with run_control_server(control_state) as control_url:
        with run_web_server(catalog_path, cards_path, control_url) as web_url:
            body = http_get(f"{web_url}/kiosk")

    assert "Make this card the admin card" in body


def test_kiosk_page_uses_dedicated_layout_and_reconnect_handler(
    tmp_path: Path,
) -> None:
    catalog_path = write_catalog(tmp_path)
    cards_path = tmp_path / "cards.yaml"
    cards_path.write_text(
        'system:\n  unlock: "ff-ee-dd"\n',
        encoding="utf-8",
    )
    control_state = FakeControlState(unlocked=False)

    with run_control_server(control_state) as control_url:
        with run_web_server(catalog_path, cards_path, control_url) as web_url:
            body = http_get(f"{web_url}/kiosk")

    assert "Live mode:" not in body
    assert "ChipBit Kiosk" not in body
    assert "events.onerror" in body
    assert "Reconnecting to ChipBit" in body


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
    user_catalog_path: Path | None = None,
    language_path: Path | None = None,
    locale_dirs: tuple[Path, ...] | None = None,
):
    server = create_web_server(
        "127.0.0.1",
        0,
        catalog_path=catalog_path,
        cards_path=cards_path,
        control_base_url=control_url,
        runner=subprocess.run if runner is None else runner,
        user_catalog_path=user_catalog_path,
        language_path=language_path,
        locale_dirs=locale_dirs,
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


def read_sse_payload(url: str) -> dict[str, object]:
    with urlopen(url, timeout=2.0) as response:
        while True:
            line = response.readline().decode("utf-8")
            if line.startswith("data: "):
                return json.loads(line.removeprefix("data: ").strip())
            if line == "":
                raise AssertionError("SSE stream ended before a data event")


def test_create_custom_web_title_adds_to_user_catalog(tmp_path: Path) -> None:
    catalog_path = write_catalog(tmp_path)
    cards_path = tmp_path / "cards.yaml"
    cards_path.write_text('system:\n  unlock: "ff-ee-dd"\n', encoding="utf-8")
    user_catalog_path = tmp_path / "user-catalog.yaml"
    control_state = FakeControlState(unlocked=True)

    with run_control_server(control_state) as control_url:
        with run_web_server(
            catalog_path, cards_path, control_url,
            user_catalog_path=user_catalog_path,
        ) as web_url:
            body = http_post(
                f"{web_url}/titles/custom",
                {"type": "web", "label": "My Site", "url": "https://example.com"},
            )

    assert "My Site" in body
    import yaml
    raw = yaml.safe_load(user_catalog_path.read_text())
    assert len(raw["titles"]) == 1
    assert raw["titles"][0]["id"] == "user-my-site"
    assert raw["titles"][0]["url"] == "https://example.com"
    assert raw["titles"][0]["allowlist"] == ["example.com"]


def test_create_custom_exec_title_with_apt_package(tmp_path: Path) -> None:
    catalog_path = write_catalog(tmp_path)
    cards_path = tmp_path / "cards.yaml"
    cards_path.write_text('system:\n  unlock: "ff-ee-dd"\n', encoding="utf-8")
    user_catalog_path = tmp_path / "user-catalog.yaml"
    control_state = FakeControlState(unlocked=True)

    with run_control_server(control_state) as control_url:
        with run_web_server(
            catalog_path, cards_path, control_url,
            user_catalog_path=user_catalog_path,
        ) as web_url:
            http_post(
                f"{web_url}/titles/custom",
                {
                    "type": "exec", "label": "Cool App",
                    "cmd": "coolapp --fs", "apt": "coolapp",
                },
            )

    import yaml
    raw = yaml.safe_load(user_catalog_path.read_text())
    entry = raw["titles"][0]
    assert entry["type"] == "exec"
    assert entry["cmd"] == ["coolapp", "--fs"]
    assert entry["install"] == {"apt": ["coolapp"]}


def test_create_custom_scummvm_title(tmp_path: Path) -> None:
    catalog_path = write_catalog(tmp_path)
    cards_path = tmp_path / "cards.yaml"
    cards_path.write_text('system:\n  unlock: "ff-ee-dd"\n', encoding="utf-8")
    user_catalog_path = tmp_path / "user-catalog.yaml"
    control_state = FakeControlState(unlocked=True)

    with run_control_server(control_state) as control_url:
        with run_web_server(
            catalog_path, cards_path, control_url,
            user_catalog_path=user_catalog_path,
        ) as web_url:
            http_post(
                f"{web_url}/titles/custom",
                {"type": "scummvm", "label": "My Adventure", "game_id": "monkey"},
            )

    import yaml
    raw = yaml.safe_load(user_catalog_path.read_text())
    entry = raw["titles"][0]
    assert entry["type"] == "scummvm"
    assert entry["game_id"] == "monkey"
    assert "data" not in entry  # user-created ScummVM titles don't need data="required"


def test_create_custom_title_missing_label_returns_400(tmp_path: Path) -> None:
    catalog_path = write_catalog(tmp_path)
    cards_path = tmp_path / "cards.yaml"
    cards_path.write_text('system:\n  unlock: "ff-ee-dd"\n', encoding="utf-8")
    user_catalog_path = tmp_path / "user-catalog.yaml"
    control_state = FakeControlState(unlocked=True)

    with run_control_server(control_state) as control_url:
        with run_web_server(
            catalog_path, cards_path, control_url,
            user_catalog_path=user_catalog_path,
        ) as web_url:
            with pytest.raises(HTTPError) as excinfo:
                http_post(
                    f"{web_url}/titles/custom",
                    {"type": "web", "label": "", "url": "https://example.com"},
                )

    assert excinfo.value.code == 400


def test_custom_title_post_returns_confirmation_flash(tmp_path: Path) -> None:
    catalog_path = write_catalog(tmp_path)
    cards_path = tmp_path / "cards.yaml"
    cards_path.write_text('system:\n  unlock: "ff-ee-dd"\n', encoding="utf-8")
    user_catalog_path = tmp_path / "user-catalog.yaml"
    control_state = FakeControlState(unlocked=True)

    with run_control_server(control_state) as control_url:
        with run_web_server(
            catalog_path, cards_path, control_url,
            user_catalog_path=user_catalog_path,
        ) as web_url:
            body = http_post(
                f"{web_url}/titles/custom",
                {"type": "web", "label": "Cool Site", "url": "https://cool.example.com"},
            )

    # Response includes flash message with label
    assert "Cool Site" in body


class DetectRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs):
        self.calls.append(list(argv))
        assert kwargs["check"] is False
        assert kwargs["text"] is True
        # See test_installer: output is captured through files, never pipes.
        assert "capture_output" not in kwargs
        assert kwargs["stdin"] is subprocess.DEVNULL
        return subprocess.CompletedProcess(
            argv,
            0,
            "puttmoon: Putt-Putt Goes to the Moon\n",
            "",
        )


def _locale_fixture(tmp_path: Path) -> tuple[Path, ...]:
    d = tmp_path / "locales"
    d.mkdir()
    (d / "de.yaml").write_text(
        'kiosk.idle.title: "Karte auflegen"\n'
        'firstrun.heading: "Diese Karte zur Admin-Karte machen"\n'
        'console.settings.language: "Sprache"\n',
        encoding="utf-8",
    )
    return (d,)


def test_first_boot_asks_for_a_language_before_anything_else(tmp_path: Path) -> None:
    """The first-run page is English prose about what to do with a card, so a
    parent who does not read English has to be able to choose before it."""
    catalog_path = write_catalog(tmp_path)
    cards_path = tmp_path / "cards.yaml"
    cards_path.write_text("system: {}\n", encoding="utf-8")
    locales = _locale_fixture(tmp_path)
    language = tmp_path / "language"

    with run_control_server(FakeControlState(unlocked=False)) as control_url:
        with run_web_server(
            catalog_path, cards_path, control_url,
            language_path=language, locale_dirs=locales,
        ) as web_url:
            first = http_get(f"{web_url}/")
            assert "Deutsch" in first
            assert "Make this card the admin card" not in first

            http_post(f"{web_url}/setup/language", {"language": "de", "next": "/"})
            assert language.read_text().strip() == "de"

            after = http_get(f"{web_url}/")
            assert 'lang="de"' in after
            assert "Diese Karte zur Admin-Karte machen" in after


def test_no_language_prompt_once_it_has_been_chosen(tmp_path: Path) -> None:
    catalog_path = write_catalog(tmp_path)
    cards_path = tmp_path / "cards.yaml"
    cards_path.write_text("system: {}\n", encoding="utf-8")
    locales = _locale_fixture(tmp_path)
    language = tmp_path / "language"
    language.write_text("en\n", encoding="utf-8")

    with run_control_server(FakeControlState(unlocked=False)) as control_url:
        with run_web_server(
            catalog_path, cards_path, control_url,
            language_path=language, locale_dirs=locales,
        ) as web_url:
            body = http_get(f"{web_url}/")
    assert "Make this card the admin card" in body


def test_english_only_image_never_shows_the_picker(tmp_path: Path) -> None:
    """One language is not a choice; do not add a screen to a first boot."""
    catalog_path = write_catalog(tmp_path)
    cards_path = tmp_path / "cards.yaml"
    cards_path.write_text("system: {}\n", encoding="utf-8")
    empty = tmp_path / "no_locales"
    empty.mkdir()

    with run_control_server(FakeControlState(unlocked=False)) as control_url:
        with run_web_server(
            catalog_path, cards_path, control_url,
            language_path=tmp_path / "language", locale_dirs=(empty,),
        ) as web_url:
            body = http_get(f"{web_url}/")
    assert "Make this card the admin card" in body


def test_second_enrollment_is_refused_while_one_is_running(tmp_path: Path) -> None:
    """Regression: arming capture twice corrupts the first enrollment.

    capture() is one slot on the daemon. A second arm clears the first
    waiter's uid and event, so a single card tap wakes both callers with the
    same uid and the card is bound to whichever install finishes last.
    """
    catalog_path = write_catalog(tmp_path)
    cards_path = tmp_path / "cards.yaml"
    cards_path.write_text(
        'system:\n  unlock: "ff-ee-dd"\ncards: {}\n', encoding="utf-8"
    )

    captures = []

    class Control:
        def status(self):
            return {"unlocked": True}

        def capture(self):
            captures.append(1)
            return "AABBCC"

        def reload(self):
            return {}

    app = web_module.WebApp(
        catalog_path=catalog_path, cards_path=cards_path, control=Control()
    )

    # stand in for an enrollment already in flight
    assert app._enroll_lock.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            app.enroll_title("demo")
    finally:
        app._enroll_lock.release()

    assert "already being set up" in str(excinfo.value)
    # and crucially: capture was never armed, so the running enrollment's
    # pending card tap is untouched
    assert captures == []


def test_enroll_lock_is_released_after_a_failure(tmp_path: Path) -> None:
    """A failed enrollment must not wedge the device against all later ones."""
    catalog_path = write_catalog(tmp_path)
    cards_path = tmp_path / "cards.yaml"
    cards_path.write_text(
        'system:\n  unlock: "ff-ee-dd"\ncards: {}\n', encoding="utf-8"
    )

    class Control:
        def status(self):
            return {"unlocked": True}

        def capture(self):
            raise RuntimeError("reader exploded")

        def reload(self):
            return {}

    app = web_module.WebApp(
        catalog_path=catalog_path, cards_path=cards_path, control=Control()
    )
    with pytest.raises(RuntimeError):
        app.enroll_title("demo")

    assert app._enroll_lock.acquire(blocking=False), "lock leaked after failure"
    app._enroll_lock.release()


# --- backing up the child's work -------------------------------------------

FAKE_PNG = b"\x89PNG fake image bytes"


def write_work_catalog(tmp_path: Path) -> Path:
    """Like write_catalog, but the title declares where it saves work."""
    catalog_path = tmp_path / "work-catalog.yaml"
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
    user_dirs: [".tuxpaint"]
""",
        encoding="utf-8",
    )
    return catalog_path


def _work_app(tmp_path: Path, catalog_path: Path):
    home = tmp_path / "home"
    (home / ".tuxpaint" / "saved").mkdir(parents=True)
    for name in ("2026-08-20.png", "2026-08-21.png"):
        (home / ".tuxpaint" / "saved" / name).write_bytes(FAKE_PNG)
    (home / ".tuxpaint" / "settings.dat").write_text("brush=3\n")

    cards_path = tmp_path / "cards.yaml"
    cards_path.write_text('system:\n  unlock: "ff-ee-dd"\ncards: {}\n')

    class Control:
        def status(self):
            return {"unlocked": True}

        def reload(self):
            return {}

    app = web_module.WebApp(
        catalog_path=catalog_path, cards_path=cards_path,
        control=Control(), home_path=home, state_path=tmp_path / "state",
    )
    return app, home


def test_work_is_discovered_from_the_catalogs_user_dirs(tmp_path: Path) -> None:
    """Supporting a new title's work must stay a catalog edit, not a code change."""
    app, home = _work_app(tmp_path, write_work_catalog(tmp_path))
    found = app.work_dirs()
    assert [d for _label, d in found] == [(home / ".tuxpaint").resolve()]

    images, others = app._work_files(found[0][1])
    assert len(images) == 2
    assert others == 1, "non-images are counted, not previewed"


def test_only_declared_work_directories_are_readable(tmp_path: Path) -> None:
    """The console runs on a family device; it must not become a file browser."""
    app, home = _work_app(tmp_path, write_work_catalog(tmp_path))
    ok = home / ".tuxpaint" / "saved" / "2026-08-20.png"
    assert app.resolve_work_file(str(ok)) == ok.resolve()

    escaped = home / ".tuxpaint" / ".." / ".." / "etc" / "passwd"
    for bad in ("/etc/passwd", str(escaped)):
        with pytest.raises((ValueError, OSError)):
            app.resolve_work_file(bad)


def test_a_user_dir_escaping_home_is_ignored(tmp_path: Path) -> None:
    """user_dirs is catalog-supplied, so it is not automatically trusted."""
    catalog_path = tmp_path / "escaping.yaml"
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
    cmd: [demo-app]
    user_dirs: ["../../etc"]
""",
        encoding="utf-8",
    )
    app, _home = _work_app(tmp_path, catalog_path)
    assert app.work_dirs() == []


def test_export_copies_everything_not_just_the_pictures(tmp_path: Path) -> None:
    app, _home = _work_app(tmp_path, write_work_catalog(tmp_path))
    drive = tmp_path / "media" / "STICK"
    drive.mkdir(parents=True)
    app._detect_drives = lambda: [drive]
    app._device_for_mount = lambda mount: "/dev/sda1"

    job = app.start_work_export(str(drive))
    _await_export(app, job)

    copied = sorted(
        str(f.relative_to(drive)) for f in drive.rglob("*") if f.is_file()
    )
    assert any(c.endswith("saved/2026-08-20.png") for c in copied)
    assert any(c.endswith("settings.dat") for c in copied), (
        "a backup that silently drops files is worse than no backup"
    )
    # date-stamped, so backing up twice never overwrites the first copy
    assert all(c.startswith("ChipBit/") for c in copied)


def test_export_syncs_and_unmounts_before_saying_it_is_safe(tmp_path: Path) -> None:
    """Parents pull the stick the moment the bar stops."""
    app, _home = _work_app(tmp_path, write_work_catalog(tmp_path))
    drive = tmp_path / "media" / "STICK"
    drive.mkdir(parents=True)
    app._detect_drives = lambda: [drive]
    app._device_for_mount = lambda mount: "/dev/sda1"

    ran: list[list[str]] = []

    def runner(argv, **kwargs):
        ran.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    app.runner = runner
    job = app.start_work_export(str(drive))
    state = _await_export(app, job)

    assert ["sync"] in ran
    assert ["udisksctl", "unmount", "-b", "/dev/sda1"] in ran
    assert ran.index(["sync"]) < ran.index(["udisksctl", "unmount", "-b", "/dev/sda1"])
    assert state["unmounted"] is True


def test_export_that_cannot_unmount_says_so(tmp_path: Path) -> None:
    app, _home = _work_app(tmp_path, write_work_catalog(tmp_path))
    drive = tmp_path / "media" / "STICK"
    drive.mkdir(parents=True)
    app._detect_drives = lambda: [drive]
    app._device_for_mount = lambda mount: "/dev/sda1"
    app.runner = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "busy")

    job = app.start_work_export(str(drive))
    state = _await_export(app, job)
    assert state["unmounted"] is False
    assert "Wait a few seconds" in app.render_work_export_status(job)


def test_export_refuses_a_drive_that_is_not_mounted(tmp_path: Path) -> None:
    app, _home = _work_app(tmp_path, write_work_catalog(tmp_path))
    app._detect_drives = lambda: []
    with pytest.raises(ValueError):
        app.start_work_export("/media/not-there")


def test_work_page_has_an_empty_state(tmp_path: Path) -> None:
    catalog_path = write_work_catalog(tmp_path)
    cards_path = tmp_path / "cards.yaml"
    cards_path.write_text('system:\n  unlock: "ff-ee-dd"\ncards: {}\n')

    class Control:
        def status(self):
            return {"unlocked": True}

    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    app = web_module.WebApp(
        catalog_path=catalog_path, cards_path=cards_path,
        control=Control(), home_path=empty_home,
    )
    assert "Nothing saved yet" in app.render_work()


def _await_export(app, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with app._export_jobs_lock:
            state = dict(app._export_jobs.get(job_id, {}))
        if state.get("done"):
            return state
        time.sleep(0.02)
    raise AssertionError("export job never finished")


def test_work_dirs_can_live_outside_home(tmp_path: Path) -> None:
    """Titles are often pointed at the state root rather than $HOME.

    TuxPaint runs with --savedir=/var/lib/chipbit/tuxpaint, so a gallery that
    only looked under home found nothing at all on a real device.
    """
    state = tmp_path / "state"
    saved = state / "tuxpaint" / "saved"
    saved.mkdir(parents=True)
    (saved / "drawing.png").write_bytes(FAKE_PNG)

    catalog_path = tmp_path / "abs.yaml"
    catalog_path.write_text(
        f"""
meta:
  catalog_version: 1
settings:
  games_root: /games
titles:
  - id: tuxpaint
    label: Tux Paint
    type: exec
    bundled: true
    cmd: [tuxpaint]
    user_dirs: ["{state}/tuxpaint"]
""",
        encoding="utf-8",
    )
    cards_path = tmp_path / "cards.yaml"
    cards_path.write_text('system:\n  unlock: "ff-ee-dd"\ncards: {}\n')

    class Control:
        def status(self):
            return {"unlocked": True}

    app = web_module.WebApp(
        catalog_path=catalog_path, cards_path=cards_path, control=Control(),
        home_path=tmp_path / "home", state_path=state,
    )
    assert [d for _label, d in app.work_dirs()] == [(state / "tuxpaint").resolve()]


def test_shipped_catalog_backs_up_where_titles_actually_save() -> None:
    """A title that pins its save location must declare that same location.

    These drifted apart once: tuxpaint saved to --savedir=/var/lib/chipbit/...
    while user_dirs still said ".tuxpaint", so the backup gallery was empty.
    """
    repo_catalog = Path(__file__).resolve().parents[2] / "catalog" / "catalog.yaml"
    if not repo_catalog.exists():
        pytest.skip("repo catalog not present")
    catalog = load_catalog_merged(repo_catalog, None)

    for title in catalog.titles.values():
        savedirs = [
            arg.split("=", 1)[1]
            for arg in title.cmd
            if arg.startswith("--savedir=")
        ]
        for savedir in savedirs:
            assert savedir in title.user_dirs, (
                f"{title.id} saves into {savedir} but does not declare it in "
                "user_dirs, so its work would not be backed up"
            )


def test_xdg_document_pin_matches_the_catalog() -> None:
    """A title that relies on the XDG pin must declare the same directory.

    LibreOffice has no --savedir flag, so its save location is pinned by
    ~/.config/user-dirs.dirs in the image instead. Same drift risk as TuxPaint,
    different mechanism: if these two disagree, documents are saved somewhere
    "Your child's work" never looks.
    """
    repo = Path(__file__).resolve().parents[2]
    user_dirs_file = (
        repo / "image" / "modules" / "chipbit" / "filesystem"
        / "home" / "chipbit" / ".config" / "user-dirs.dirs"
    )
    catalog_file = repo / "catalog" / "catalog.yaml"
    if not user_dirs_file.exists() or not catalog_file.exists():
        pytest.skip("image overlay or catalog not present")

    pinned = {}
    for line in user_dirs_file.read_text().splitlines():
        if line.startswith("XDG_") and "=" in line:
            key, value = line.split("=", 1)
            pinned[key] = value.strip().strip('"')

    documents = pinned.get("XDG_DOCUMENTS_DIR", "")
    assert documents.startswith("$HOME/"), documents
    relative = documents[len("$HOME/"):]

    catalog = load_catalog_merged(catalog_file, None)
    users_of_xdg = [
        title for title in catalog.titles.values()
        if title.cmd and title.cmd[0] == "soffice"
    ]
    assert users_of_xdg, "expected at least one XDG-following title"
    for title in users_of_xdg:
        assert relative in title.user_dirs, (
            f"{title.id} saves into $HOME/{relative} via the XDG pin but does "
            "not declare it in user_dirs, so its documents would not be "
            "backed up"
        )


def test_catalog_and_image_overlay_agree() -> None:
    """The image ships its own copy of the catalog; keep them identical."""
    repo = Path(__file__).resolve().parents[2]
    src = repo / "catalog" / "catalog.yaml"
    overlay = (
        repo / "image" / "modules" / "chipbit" / "filesystem"
        / "usr" / "share" / "chipbit" / "catalog.yaml"
    )
    if not src.exists() or not overlay.exists():
        pytest.skip("catalog or overlay not present")
    assert src.read_text() == overlay.read_text(), (
        "catalog/catalog.yaml and the image overlay copy have drifted; "
        "the device would ship a different catalog than the repo shows"
    )

