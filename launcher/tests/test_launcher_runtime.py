from __future__ import annotations

import io
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from chipbit.launcher import (
    FileBackedConfig,
    LauncherService,
    LaunchSettings,
    build_launch_argv,
)
from chipbit.models import CatalogTitle, load_cards
from chipbit.reader import MockReader, pump_reader


class FakeProcess:
    def __init__(self, pid: int, wait_side_effects: list[object] | None = None) -> None:
        self.pid = pid
        self._returncode: int | None = None
        self._wait_side_effects = list(wait_side_effects or [])
        self.wait_calls: list[float | None] = []

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self._wait_side_effects:
            effect = self._wait_side_effects.pop(0)
            if isinstance(effect, BaseException):
                raise effect
            self._returncode = int(effect)
            return self._returncode

        self._returncode = 0
        return 0


class PopenRecorder:
    def __init__(self, processes: list[FakeProcess]) -> None:
        self.calls: list[tuple[list[str], bool]] = []
        self.envs: list[dict | None] = []
        self._processes = list(processes)

    def __call__(
        self, argv: list[str], *, start_new_session: bool, env: dict | None = None
    ) -> FakeProcess:
        self.calls.append((list(argv), start_new_session))
        self.envs.append(env)
        if not self._processes:
            raise AssertionError("No fake process left for Popen call")
        return self._processes.pop(0)


class FakeClock:
    def __init__(self) -> None:
        self.current = 0.0

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


def test_mock_reader_launches_bound_card(tmp_path: Path) -> None:
    service, popen_recorder, _, _ = make_service(tmp_path)
    stop = threading.Event()

    pump_reader(MockReader(io.StringIO("aa-bb-cc\n")), service.on_scan, stop)

    assert popen_recorder.calls == [(["demo-app", "--fullscreen"], True)]
    assert service.status()["current"] == "Demo App"


def test_home_card_stops_running_process_group(tmp_path: Path) -> None:
    service, popen_recorder, killpg_calls, _ = make_service(tmp_path)

    service.on_scan("aa-bb-cc")
    service.on_scan("ff-ee-dd")

    assert popen_recorder.calls == [(["demo-app", "--fullscreen"], True)]
    assert killpg_calls == [(5101, signal.SIGTERM)]
    assert service.status()["running"] is False
    assert service.status()["current"] is None


def test_capture_mode_intercepts_scan_without_launching(tmp_path: Path) -> None:
    service, popen_recorder, _, _ = make_service(tmp_path)
    captured: list[str | None] = []

    capture_thread = threading.Thread(
        target=lambda: captured.append(service.capture(timeout=1.0)),
        daemon=True,
    )
    capture_thread.start()

    for _ in range(1000):
        if service.status()["capture_mode"]:
            break
    else:
        raise AssertionError("capture mode never armed")

    pump_reader(
        MockReader(io.StringIO("aa-bb-cc\n")),
        service.on_scan,
        threading.Event(),
    )
    capture_thread.join(timeout=1.0)

    assert captured == ["AABBCC"]
    assert popen_recorder.calls == []
    assert service.status()["current"] is None


@pytest.mark.parametrize(
    ("policy", "expected_launches", "expected_kills"),
    [
        ("home_only", 1, 0),
        ("ignore", 1, 0),
        ("swap", 2, 1),
    ],
)
def test_while_running_policy_behavior(
    tmp_path: Path,
    policy: str,
    expected_launches: int,
    expected_kills: int,
) -> None:
    service, popen_recorder, killpg_calls, _ = make_service(
        tmp_path,
        while_running=policy,
    )

    service.on_scan("aa-bb-cc")
    service.on_scan("11-22-33")

    assert len(popen_recorder.calls) == expected_launches
    assert len(killpg_calls) == expected_kills
    if policy == "swap":
        assert popen_recorder.calls[-1][0] == ["two-app", "--fullscreen"]
        assert service.status()["current"] == "Second App"
    else:
        assert service.status()["current"] == "Demo App"


def test_stop_current_escalates_to_sigkill_after_timeout(tmp_path: Path) -> None:
    timeout_process = FakeProcess(
        pid=101,
        wait_side_effects=[subprocess.TimeoutExpired(cmd=["demo-app"], timeout=0.1), 0],
    )
    service, _, killpg_calls, _ = make_service(tmp_path, processes=[timeout_process])

    service.on_scan("aa-bb-cc")
    service.stop_current()

    assert killpg_calls == [
        (5101, signal.SIGTERM),
        (5101, signal.SIGKILL),
    ]
    assert service.status()["running"] is False


def test_reload_replaces_cards_after_file_change(tmp_path: Path) -> None:
    service, popen_recorder, _, config = make_service(tmp_path)
    cards_path = config.cards_path
    cards_path.write_text(
        """
cards:
  "99-88-77": second
system:
    home: "ff-ee-dd"
    unlock: "12-34-56"
""",
        encoding="utf-8",
    )
    os.utime(cards_path, None)

    assert service.reload(force=True) is True

    service.on_scan("99-88-77")

    assert popen_recorder.calls == [(["two-app", "--fullscreen"], True)]


def test_unlock_times_out_after_idle(tmp_path: Path) -> None:
    clock = FakeClock()
    service, _, _, _ = make_service(
        tmp_path,
        unlock_uid="12-34-56",
        unlock_timeout_secs=60.0,
        monotonic=clock,
    )

    service.on_scan("12-34-56")
    assert service.status()["unlocked"] is True

    clock.advance(61.0)

    assert service.status()["unlocked"] is False


def test_lock_clears_unlock_state_immediately(tmp_path: Path) -> None:
    clock = FakeClock()
    service, _, _, _ = make_service(
        tmp_path,
        unlock_uid="12-34-56",
        monotonic=clock,
    )

    service.on_scan("12-34-56")
    assert service.status()["unlocked"] is True

    service.lock()

    assert service.status()["unlocked"] is False


def test_capturing_admin_card_during_enrollment_refreshes_unlock(
    tmp_path: Path,
) -> None:
    # If the admin card (uid "12-34-56") is the only card a parent has, they will
    # tap it during the title-enrollment capture window.  The capture should still
    # succeed AND the unlock deadline should be extended so the subsequent
    # enroll_title_for_uid call does not see a stale unlock state.
    clock = FakeClock()
    service, _, _, _ = make_service(
        tmp_path,
        unlock_uid="12-34-56",
        unlock_timeout_secs=10.0,
        monotonic=clock,
    )

    # Unlock via the admin card.
    service.on_scan("12-34-56")
    assert service.status()["unlocked"] is True

    # Advance time so the unlock is almost expired.
    clock.advance(9.5)
    assert service.status()["unlocked"] is True

    # Start a capture in another thread (simulating the web service's capture()).
    captured: list[str | None] = []
    capture_thread = threading.Thread(
        target=lambda: captured.append(service.capture(timeout=1.0)),
        daemon=True,
    )
    capture_thread.start()

    for _ in range(1000):
        if service.status()["capture_mode"]:
            break
    else:
        raise AssertionError("capture mode never armed")

    # Tap the admin card during the capture window.  on_scan should both capture
    # the UID and refresh the unlock deadline.
    service.on_scan("12-34-56")
    capture_thread.join(timeout=1.0)

    assert captured == ["123456"], "admin card UID was not captured"
    # The deadline should have been pushed forward; unlock must still be active.
    assert service.status()["unlocked"] is True, (
        "unlock expired after capture of admin card"
    )


def test_unknown_card_sets_transient_status_event(tmp_path: Path) -> None:
    clock = FakeClock()
    service, _, _, _ = make_service(
        tmp_path,
        monotonic=clock,
    )

    service.on_scan("de-ad-be-ef")

    status = service.status()
    assert status["last_event"] == {
        "kind": "unknown-card",
        "uid": "DEADBEEF",
    }

    clock.advance(6.0)

    assert service.status()["last_event"] is None


def test_first_scan_auto_enrolls_admin_card_when_no_admin_exists(
    tmp_path: Path,
) -> None:
    service, popen_recorder, _, config = make_service(tmp_path)

    service.on_scan("12-34-56")

    cards = load_cards(config.cards_path)
    assert cards.system_cards["unlock"].uid == "123456"
    assert popen_recorder.calls == []
    assert service.status()["last_event"] is None


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (
            CatalogTitle(
                id="puttmoon",
                label="Putt-Putt",
                type="scummvm",
                bundled=False,
                data="required",
                game_id="puttmoon",
                data_dir="scummvm/puttmoon",
            ),
            ["scummvm", "-f", "-p", "/games/scummvm/puttmoon", "puttmoon"],
        ),
        (
            CatalogTitle(
                id="readerrabbit-dos",
                label="Reader Rabbit",
                type="dosbox",
                bundled=False,
                data="required",
                conf="readerrabbit/rr.conf",
            ),
            [
                "dosbox-staging",
                "-conf",
                "/games/readerrabbit/rr.conf",
                "-fullscreen",
            ],
        ),
        (
            CatalogTitle(
                id="mathblaster-flash",
                label="Math Blaster",
                type="ruffle",
                bundled=False,
                data="required",
                swf="flash/mathblaster.swf",
            ),
            [
                "chromium",
                "--ozone-platform=wayland",
                "--kiosk",
                "--noerrdialogs",
                "--no-first-run",
                "--disable-features=TranslateUI",
                "--user-data-dir=/tmp/chipbit-ruffle",
                "--app=http://127.0.0.1:8080/ruffle/player.html?swf=/swf/flash/mathblaster.swf",
            ],
        ),
        (
            CatalogTitle(
                id="reader-rabbit-web",
                label="Reader Rabbit Web",
                type="web",
                bundled=False,
                url="https://example.invalid/game",
            ),
            [
                "chromium",
                "--ozone-platform=wayland",
                "--kiosk",
                "--noerrdialogs",
                "--no-first-run",
                "--disable-pinch",
                "--disable-features=TranslateUI",
                "--overscroll-history-navigation=0",
                "--user-data-dir=/tmp/chipbit-web-app",
                "--app=https://example.invalid/game",
            ],
        ),
    ],
)
def test_build_launch_argv_resolves_engine_paths_from_games_root(
    title: CatalogTitle,
    expected: list[str],
) -> None:
    assert build_launch_argv(title, Path("/games"), LaunchSettings()) == expected


def make_service(
    tmp_path: Path,
    *,
    while_running: str = "home_only",
    processes: list[FakeProcess] | None = None,
    unlock_uid: str | None = "12-34-56",
    unlock_timeout_secs: float = 300.0,
    monotonic=None,
) -> tuple[
    LauncherService,
    PopenRecorder,
    list[tuple[int, signal.Signals]],
    FileBackedConfig,
]:
    catalog_path = tmp_path / "catalog.yaml"
    cards_path = tmp_path / "cards.yaml"

    catalog_path.write_text(
        """
meta:
  catalog_version: 1
titles:
  - id: demo
    label: Demo App
    type: exec
    bundled: true
    cmd: [demo-app, --fullscreen]
  - id: second
    label: Second App
    type: exec
    bundled: true
    cmd: [two-app, --fullscreen]
""",
        encoding="utf-8",
    )
    cards_path.write_text(
        build_cards_yaml(unlock_uid),
        encoding="utf-8",
    )

    config = FileBackedConfig(catalog_path=catalog_path, cards_path=cards_path)
    assert config.load(force=True) is True

    fake_processes = processes or [FakeProcess(pid=101), FakeProcess(pid=202)]
    popen_recorder = PopenRecorder(fake_processes)
    killpg_calls: list[tuple[int, signal.Signals]] = []

    def record_killpg(pgid: int, sig: signal.Signals) -> None:
        killpg_calls.append((pgid, sig))

    service = LauncherService(
        config,
        settings=LaunchSettings(
            while_running=policy_value(while_running),
            stop_grace_secs=0.1,
            unlock_timeout_secs=unlock_timeout_secs,
        ),
        popen_factory=popen_recorder,
        killpg=record_killpg,
        getpgid=lambda pid: pid + 5000,
        thread_factory=None,
        monotonic=time.monotonic if monotonic is None else monotonic,
    )
    return service, popen_recorder, killpg_calls, config


def policy_value(policy: str) -> str:
    return policy


def build_cards_yaml(unlock_uid: str | None) -> str:
    unlock_line = ""
    if unlock_uid is not None:
        unlock_line = f'  unlock: "{unlock_uid}"\n'
    return (
        "\n"
        "cards:\n"
        '  "aa-bb-cc": demo\n'
        '  "11-22-33": second\n'
        "system:\n"
        '  home: "ff-ee-dd"\n'
        f"{unlock_line}"
    )
