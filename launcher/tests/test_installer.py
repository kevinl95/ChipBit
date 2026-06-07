from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

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
        assert kwargs == {"check": False, "capture_output": True, "text": True}
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
            ExpectedCall(["apt-get", "install", "-y", "demo-app"]),
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
                ["apt-get", "install", "-y", "demo-app"],
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
