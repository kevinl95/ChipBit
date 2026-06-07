from __future__ import annotations

import io
import os
import signal
import subprocess
import threading
from pathlib import Path

import pytest

from chipbit.launcher import (
    FileBackedConfig,
    LauncherService,
    LaunchSettings,
    build_launch_argv,
)
from chipbit.models import CatalogTitle
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
        self._processes = list(processes)

    def __call__(self, argv: list[str], *, start_new_session: bool) -> FakeProcess:
        self.calls.append((list(argv), start_new_session))
        if not self._processes:
            raise AssertionError("No fake process left for Popen call")
        return self._processes.pop(0)


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
""",
        encoding="utf-8",
    )
    os.utime(cards_path, None)

    assert service.reload(force=True) is True

    service.on_scan("99-88-77")

    assert popen_recorder.calls == [(["two-app", "--fullscreen"], True)]


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
            ["ruffle", "--fullscreen", "/games/flash/mathblaster.swf"],
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
        """
cards:
  "aa-bb-cc": demo
  "11-22-33": second
system:
  home: "ff-ee-dd"
""",
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
        ),
        popen_factory=popen_recorder,
        killpg=record_killpg,
        getpgid=lambda pid: pid + 5000,
        thread_factory=None,
    )
    return service, popen_recorder, killpg_calls, config


def policy_value(policy: str) -> str:
    return policy
