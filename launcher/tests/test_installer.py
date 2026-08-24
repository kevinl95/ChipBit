from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

import chipbit.installer as installer
from chipbit.installer import (
    DataMissingError,
    InstallationError,
    InstallSpecError,
    enroll_card,
    ensure_install_spec_installed,
    has_required_data,
)
from chipbit.models import CatalogTitle, load_cards


@dataclass(frozen=True)
class ExpectedCall:
    argv: list[str]
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeRunner:
    def __init__(self, expected_calls: list[ExpectedCall]) -> None:
        self.calls: list[list[str]] = []
        self._expected_calls = list(expected_calls)

    def __call__(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if not self._expected_calls:
            raise AssertionError(f"Unexpected subprocess call: {argv}")

        expected = self._expected_calls.pop(0)
        assert argv == expected.argv
        assert kwargs["check"] is False
        assert kwargs["text"] is True
        # Output must be captured through files, never pipes: communicate()
        # waits for EOF, which a daemon left running by a package's postinst
        # can hold open long after the install succeeded.
        assert "capture_output" not in kwargs, "pipes reintroduced"
        assert kwargs["stdout"] is not subprocess.PIPE
        assert kwargs["stderr"] is not subprocess.PIPE
        assert kwargs["stdin"] is subprocess.DEVNULL
        return subprocess.CompletedProcess(
            argv,
            expected.returncode,
            expected.stdout,
            expected.stderr,
        )

    def assert_consumed(self) -> None:
        assert self._expected_calls == []


@pytest.mark.parametrize(
    ("manager", "package", "expected_check"),
    [
        (
            "apt",
            "demo-app",
            ["dpkg-query", "--show", "--showformat=${Status}", "demo-app"],
        ),
        (
            "flatpak",
            "org.demo.App",
            ["flatpak", "info", "--show-ref", "org.demo.App"],
        ),
        (
            "pip",
            "demo-app",
            [sys.executable, "-m", "pip", "show", "demo-app"],
        ),
    ],
)
def test_ensure_install_spec_skips_already_installed_packages(
    manager: str,
    package: str,
    expected_check: list[str],
) -> None:
    stdout = "install ok installed" if manager == "apt" else "installed"
    runner = FakeRunner([ExpectedCall(expected_check, stdout=stdout)])

    progress = list(
        ensure_install_spec_installed(
            {manager: (package,)},
            runner=runner,
            network_checker=lambda: True,
        )
    )

    runner.assert_consumed()
    assert [event.step for event in progress] == ["checking", "already-installed"]


def test_ensure_install_spec_installs_missing_packages() -> None:
    runner = FakeRunner(
        [
            ExpectedCall(
                ["dpkg-query", "--show", "--showformat=${Status}", "demo-app"],
                returncode=1,
            ),
            ExpectedCall(
                [
                    "timeout", "--signal=TERM", "--kill-after=10", "120",
                    "sudo", "apt-get", "update", "-qq",
                    "-o", "DPkg::Lock::Timeout=60",
                    "-o", "Acquire::http::Timeout=15",
                    "-o", "Acquire::https::Timeout=15",
                    "-o", "Acquire::Retries=1",
                ],
            ),
            ExpectedCall(
                [
                    "timeout", "--signal=TERM", "--kill-after=10", "900",
                    "sudo", "apt-get", "install", "-y", "--download-only",
                    "--no-install-recommends",
                    "-o", "DPkg::Lock::Timeout=60",
                    "-o", "Acquire::http::Timeout=15",
                    "-o", "Acquire::https::Timeout=15",
                    "-o", "Acquire::Retries=1",
                    "demo-app",
                ]
            ),
            ExpectedCall(
                [
                    "timeout", "--signal=TERM", "--kill-after=10", "600",
                    "sudo", "apt-get", "install", "-y",
                    "--no-install-recommends",
                    "-o", "DPkg::Lock::Timeout=60",
                    "-o", "Acquire::http::Timeout=15",
                    "-o", "Acquire::https::Timeout=15",
                    "-o", "Acquire::Retries=1",
                    "demo-app",
                ]
            ),
            ExpectedCall(
                ["dpkg-query", "--show", "--showformat=${Status}", "demo-app"],
                stdout="install ok installed",
            ),
        ]
    )

    progress = list(
        ensure_install_spec_installed(
            {"apt": ("demo-app",)},
            runner=runner,
            network_checker=lambda: True,
        )
    )

    runner.assert_consumed()
    assert [event.step for event in progress] == [
        "checking",
        "network-check",
        "installing",
        "installing",  # update package lists
        "installing",  # download into the apt cache
        "verifying",
        "installed",
    ]


def test_ensure_install_spec_uses_requested_python_for_pip() -> None:
    runner = FakeRunner(
        [
            ExpectedCall(
                ["/tmp/custom-python", "-m", "pip", "show", "demo-app"],
                returncode=1,
            ),
            ExpectedCall(
                [
                    "/tmp/custom-python",
                    "-m",
                    "pip",
                    "install",
                    "--break-system-packages",
                    "demo-app",
                ]
            ),
            ExpectedCall(
                ["/tmp/custom-python", "-m", "pip", "show", "demo-app"],
                stdout="Name: demo-app\n",
            ),
        ]
    )

    progress = list(
        ensure_install_spec_installed(
            {"pip": ("demo-app",)},
            runner=runner,
            network_checker=lambda: True,
            python_executable="/tmp/custom-python",
        )
    )

    runner.assert_consumed()
    assert [event.step for event in progress] == [
        "checking",
        "network-check",
        "installing",
        "verifying",
        "installed",
    ]


def test_enroll_card_does_not_bind_when_install_fails(tmp_path: Path) -> None:
    cards_path = tmp_path / "cards.yaml"
    runner = FakeRunner(
        [
            ExpectedCall(
                ["dpkg-query", "--show", "--showformat=${Status}", "demo-app"],
                returncode=1,
            ),
            ExpectedCall(
                [
                    "timeout", "--signal=TERM", "--kill-after=10", "120",
                    "sudo", "apt-get", "update", "-qq",
                    "-o", "DPkg::Lock::Timeout=60",
                    "-o", "Acquire::http::Timeout=15",
                    "-o", "Acquire::https::Timeout=15",
                    "-o", "Acquire::Retries=1",
                ],
            ),
            ExpectedCall(
                [
                    "timeout", "--signal=TERM", "--kill-after=10", "900",
                    "sudo", "apt-get", "install", "-y", "--download-only",
                    "--no-install-recommends",
                    "-o", "DPkg::Lock::Timeout=60",
                    "-o", "Acquire::http::Timeout=15",
                    "-o", "Acquire::https::Timeout=15",
                    "-o", "Acquire::Retries=1",
                    "demo-app",
                ],
                returncode=1,
                stderr="package failure",
            ),
        ]
    )

    title = CatalogTitle(
        id="demo",
        label="Demo App",
        type="exec",
        bundled=False,
        install={"apt": ("demo-app",)},
        cmd=("demo-app",),
    )

    with pytest.raises(InstallationError):
        list(
            enroll_card(
                "aa-bb-cc",
                title,
                cards_path=cards_path,
                runner=runner,
                network_checker=lambda: True,
            )
        )

    runner.assert_consumed()
    assert load_cards(cards_path).title_cards == {}


def test_ensure_install_spec_rejects_non_declarative_manager() -> None:
    runner = FakeRunner([])

    with pytest.raises(InstallSpecError):
        list(ensure_install_spec_installed({"shell": ("rm -rf /",)}, runner=runner))

    assert runner.calls == []


def test_enroll_card_requires_data_before_binding(tmp_path: Path) -> None:
    cards_path = tmp_path / "cards.yaml"
    games_root = tmp_path / "games"
    games_root.mkdir()

    title = CatalogTitle(
        id="mathblaster-flash",
        label="Math Blaster",
        type="ruffle",
        bundled=False,
        data="required",
        swf="flash/mathblaster.swf",
    )

    with pytest.raises(DataMissingError):
        list(
            enroll_card(
                "aa-bb-cc",
                title,
                cards_path=cards_path,
                games_root=games_root,
            )
        )

    assert load_cards(cards_path).title_cards == {}


def test_has_required_data_uses_scummvm_detect_with_data_dir(tmp_path: Path) -> None:
    games_root = tmp_path / "games"
    data_dir = games_root / "shared" / "puttmoon-cd"
    data_dir.mkdir(parents=True)
    runner = FakeRunner(
        [
            ExpectedCall(
                ["scummvm", "--detect", f"--path={data_dir}"],
                stdout="puttmoon: Putt-Putt Goes to the Moon\n",
            )
        ]
    )
    title = CatalogTitle(
        id="puttmoon",
        label="Putt-Putt",
        type="scummvm",
        bundled=False,
        data="required",
        game_id="puttmoon",
        data_dir="shared/puttmoon-cd",
    )

    assert has_required_data(title, games_root, runner=runner) is True
    runner.assert_consumed()


@pytest.mark.parametrize(
    "title",
    [
        CatalogTitle(
            id="readerrabbit-dos",
            label="Reader Rabbit",
            type="dosbox",
            bundled=False,
            data="required",
            conf="readerrabbit/rr.conf",
        ),
        CatalogTitle(
            id="mathblaster-flash",
            label="Math Blaster",
            type="ruffle",
            bundled=False,
            data="required",
            swf="flash/mathblaster.swf",
        ),
    ],
)
def test_has_required_data_checks_engine_paths_exist(
    tmp_path: Path,
    title: CatalogTitle,
) -> None:
    games_root = tmp_path / "games"
    games_root.mkdir()
    path_parts = title.conf if title.conf is not None else title.swf
    assert path_parts is not None
    (games_root / path_parts).parent.mkdir(parents=True, exist_ok=True)
    (games_root / path_parts).write_text("present", encoding="utf-8")

    assert has_required_data(title, games_root) is True


def test_has_required_data_requires_scummvm_game_id_match(tmp_path: Path) -> None:
    games_root = tmp_path / "games"
    data_dir = games_root / "scummvm" / "puttmoon"
    data_dir.mkdir(parents=True)
    runner = FakeRunner(
        [
            ExpectedCall(
                ["scummvm", "--detect", f"--path={data_dir}"],
                stdout="monkey: The Secret of Monkey Island\n",
            )
        ]
    )
    title = CatalogTitle(
        id="puttmoon",
        label="Putt-Putt",
        type="scummvm",
        bundled=False,
        data="required",
        game_id="puttmoon",
    )

    assert has_required_data(title, games_root, runner=runner) is False
    runner.assert_consumed()


def test_apt_lock_error_is_reported_in_words_a_parent_can_act_on() -> None:
    """Regression: a stranded apt-get used to surface its raw lock error.

    A Pi that dropped off Wi-Fi mid-enrollment left an orphaned apt-get holding
    /var/lib/apt/lists/lock, and the next enrollment showed the parent
    "E: Could not get lock ... It is held by process 1246 (apt-get)".
    """
    lock_error = (
        "E: Could not get lock /var/lib/apt/lists/lock. "
        "It is held by process 1246 (apt-get)"
    )
    hint = installer._apt_failure_hint(lock_error)
    assert hint is not None
    assert "process 1246" not in hint
    assert "lock" not in hint.lower()
    assert "again" in hint.lower()


def test_offline_apt_error_points_at_wifi() -> None:
    hint = installer._apt_failure_hint(
        "E: Temporary failure resolving 'deb.debian.org'"
    )
    assert hint is not None
    assert "Wi-Fi" in hint


def test_unrecognised_apt_error_keeps_the_raw_detail() -> None:
    """Anything we have not seen before must not be silently swallowed."""
    assert installer._apt_failure_hint("E: Something entirely new") is None


def test_apt_commands_run_under_timeout_so_they_cannot_strand_the_lock() -> None:
    """timeout(1) signals the process group; subprocess's own timeout SIGKILLs
    only `sudo`, leaving apt-get orphaned and holding the lock."""
    managers = installer._manager_definitions()
    apt = managers["apt"]
    assert apt.pre_install_argv[0] == "timeout"
    assert "--signal=TERM" in apt.pre_install_argv
    assert apt.install_argv(("demo",), None)[0] == "timeout"


DPKG_INTERRUPTED = (
    "E: dpkg was interrupted, you must manually run "
    "'sudo dpkg --configure -a' to correct the problem."
)


def test_interrupted_dpkg_is_detected() -> None:
    assert installer._dpkg_needs_repair(DPKG_INTERRUPTED)
    assert not installer._dpkg_needs_repair("E: Unable to locate package foo")


def test_timeout_exit_codes_get_a_message_about_waiting_not_a_blank_error() -> None:
    """timeout(1) kills quietly, so rc alone has to carry the explanation."""
    for rc in (124, 137):
        assert rc in installer._TIMEOUT_EXIT_CODES
    msg = installer._apt_timed_out(124)
    assert "Wi-Fi" in msg and "again" in msg


def test_interrupted_dpkg_is_repaired_and_the_install_retried() -> None:
    """A reboot mid-install used to brick enrollment until someone SSHed in.

    The device must heal itself on the next card tap instead of telling a
    parent to open a terminal.
    """
    apt = installer._manager_definitions()["apt"]
    download = apt.download_argv(("demo-app",), None)
    install = apt.install_argv(("demo-app",), None)

    runner = FakeRunner(
        [
            ExpectedCall(
                ["dpkg-query", "--show", "--showformat=${Status}", "demo-app"],
                returncode=1,
            ),
            ExpectedCall(apt.pre_install_argv),
            ExpectedCall(download),
            # first unpack refuses: dpkg is half-configured
            ExpectedCall(install, returncode=100, stderr=DPKG_INTERRUPTED),
            ExpectedCall(apt.repair_argv),
            # retried, and this time it works
            ExpectedCall(install),
            ExpectedCall(
                ["dpkg-query", "--show", "--showformat=${Status}", "demo-app"],
                stdout="install ok installed",
            ),
        ]
    )

    progress = list(
        ensure_install_spec_installed(
            {"apt": ("demo-app",)}, runner=runner, network_checker=lambda: True
        )
    )
    runner.assert_consumed()
    assert progress[-1].step == "installed"
    assert any("interrupted" in e.message.lower() for e in progress)


def test_repair_that_itself_fails_surfaces_the_original_problem() -> None:
    """If we cannot heal it, say so rather than pretending the install worked."""
    apt = installer._manager_definitions()["apt"]
    runner = FakeRunner(
        [
            ExpectedCall(
                ["dpkg-query", "--show", "--showformat=${Status}", "demo-app"],
                returncode=1,
            ),
            ExpectedCall(apt.pre_install_argv),
            ExpectedCall(apt.download_argv(("demo-app",), None)),
            ExpectedCall(
                apt.install_argv(("demo-app",), None),
                returncode=100, stderr=DPKG_INTERRUPTED,
            ),
            ExpectedCall(apt.repair_argv, returncode=1, stderr="still broken"),
        ]
    )
    with pytest.raises(InstallationError):
        list(
            ensure_install_spec_installed(
                {"apt": ("demo-app",)}, runner=runner, network_checker=lambda: True
            )
        )
    runner.assert_consumed()


def test_download_is_the_step_under_the_long_timeout() -> None:
    """Fetching is safe to interrupt; unpacking is not. Keep it that way."""
    apt = installer._manager_definitions()["apt"]
    download = apt.download_argv(("demo-app",), None)
    install = apt.install_argv(("demo-app",), None)
    assert "--download-only" in download
    assert "--download-only" not in install
    assert int(download[3]) > int(install[3]), (
        "the network-bound step should get the longer budget"
    )


def test_command_returns_as_soon_as_the_child_exits(tmp_path: Path) -> None:
    """Regression: enrollment used to hang for the whole timeout after a
    successful install.

    apt exits, but hands its stdout to whatever a package's postinst leaves
    running.  With pipes, communicate() waits for that write end to close, so
    ChipBit sat on "installing" and then reported a timeout for a package that
    had installed perfectly -- the user restarted, tapped the card, and found
    it working.
    """
    import time

    argv = ["sh", "-c", "sleep 30 & echo done; echo warn >&2; exit 0"]
    start = time.monotonic()
    result = installer._run_command(argv, runner=subprocess.run, timeout=20.0)
    elapsed = time.monotonic() - start

    assert result.returncode == 0
    assert "done" in result.stdout
    assert "warn" in result.stderr
    assert elapsed < 5.0, (
        f"blocked {elapsed:.1f}s waiting on a background child that inherited "
        "our output"
    )

