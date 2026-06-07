"""On-demand installer and card-enrollment helpers for the ChipBit runtime."""

from __future__ import annotations

import socket
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .models import (
    DEFAULT_GAMES_ROOT,
    CardsConfig,
    CatalogTitle,
    EnrolledCard,
    load_cards,
    normalize_uid,
    resolve_title_content_path,
    save_cards,
)

DEFAULT_FLATPAK_REMOTE: Final[str] = "flathub"
_ALLOWED_INSTALL_MANAGERS: Final[frozenset[str]] = frozenset({"apt", "flatpak", "pip"})

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
NetworkChecker = Callable[[], bool]
DataChecker = Callable[[CatalogTitle, Path], bool]


class InstallSpecError(ValueError):
    """Raised when an install spec is not declarative or not allowlisted."""


class InstallationError(RuntimeError):
    """Raised when package installation or verification fails."""


class NetworkUnavailableError(RuntimeError):
    """Raised when an install is needed but the network is unavailable."""


class DataMissingError(RuntimeError):
    """Raised when a title requiring external data cannot be verified."""


@dataclass(frozen=True)
class InstallProgress:
    """A coarse-grained progress event for install/enroll workflows."""

    step: str
    message: str
    manager: str | None = None
    packages: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class _ManagerDefinition:
    name: str
    check_argv: Callable[[str, str], list[str]]
    install_argv: Callable[[tuple[str, ...], str], list[str]]
    is_installed: Callable[[subprocess.CompletedProcess[str]], bool]


def ensure_install_spec_installed(
    install_spec: Mapping[str, Sequence[str]] | None,
    *,
    runner: CommandRunner = subprocess.run,
    network_checker: NetworkChecker | None = None,
    python_executable: str | None = None,
    flatpak_remote: str = DEFAULT_FLATPAK_REMOTE,
) -> Iterator[InstallProgress]:
    """Ensure every declared package is installed, yielding progress as it goes."""
    normalized_spec = normalize_install_spec(install_spec)
    if not normalized_spec:
        return

    executable = sys.executable if python_executable is None else python_executable
    managers = _manager_definitions()
    missing_by_manager: dict[str, tuple[str, ...]] = {}

    for manager_name, packages in normalized_spec.items():
        manager = managers[manager_name]
        missing: list[str] = []
        for package in packages:
            yield InstallProgress(
                step="checking",
                message=f"Checking whether {package} is already installed",
                manager=manager_name,
                packages=(package,),
            )
            if _package_is_installed(
                manager,
                package,
                runner=runner,
                python_executable=executable,
                flatpak_remote=flatpak_remote,
            ):
                yield InstallProgress(
                    step="already-installed",
                    message=f"{package} is already installed",
                    manager=manager_name,
                    packages=(package,),
                )
            else:
                missing.append(package)

        if missing:
            missing_by_manager[manager_name] = tuple(missing)

    if not missing_by_manager:
        return

    yield InstallProgress(
        step="network-check",
        message="Checking network before installing missing packages",
    )
    checker = has_network_connection if network_checker is None else network_checker
    if not checker():
        raise NetworkUnavailableError(
            "network is unavailable; refusing to bind a card to missing software"
        )

    for manager_name, packages in missing_by_manager.items():
        manager = managers[manager_name]
        yield InstallProgress(
            step="installing",
            message=f"Installing {', '.join(packages)} via {manager_name}",
            manager=manager_name,
            packages=packages,
        )
        install_result = _run_command(
            manager.install_argv(packages, flatpak_remote),
            runner=runner,
        )
        if install_result.returncode != 0:
            raise InstallationError(
                f"{manager_name} install failed for {', '.join(packages)}: "
                f"{install_result.stderr.strip() or 'unknown error'}"
            )

        yield InstallProgress(
            step="verifying",
            message=f"Verifying {', '.join(packages)} after install",
            manager=manager_name,
            packages=packages,
        )
        for package in packages:
            if not _package_is_installed(
                manager,
                package,
                runner=runner,
                python_executable=executable,
                flatpak_remote=flatpak_remote,
            ):
                raise InstallationError(
                    f"{manager_name} install completed but {package} is still missing"
                )

        yield InstallProgress(
            step="installed",
            message=f"Installed {', '.join(packages)} via {manager_name}",
            manager=manager_name,
            packages=packages,
        )


def enroll_card(
    uid: str,
    title: CatalogTitle,
    *,
    cards_path: Path,
    games_root: Path = DEFAULT_GAMES_ROOT,
    runner: CommandRunner = subprocess.run,
    network_checker: NetworkChecker | None = None,
    data_checker: DataChecker | None = None,
    python_executable: str | None = None,
    flatpak_remote: str = DEFAULT_FLATPAK_REMOTE,
    scummvm_executable: str = "scummvm",
) -> Iterator[InstallProgress]:
    """Install, verify, and only then bind a UID to a catalog title."""
    normalized_uid = normalize_uid(uid)
    if not normalized_uid:
        raise ValueError("uid must contain at least one alphanumeric character")

    yield from ensure_install_spec_installed(
        title.install,
        runner=runner,
        network_checker=network_checker,
        python_executable=python_executable,
        flatpak_remote=flatpak_remote,
    )

    if title.data == "required":
        yield InstallProgress(
            step="checking-data",
            message=f"Checking required data for {title.label}",
        )
        if not has_required_data(
            title,
            games_root,
            runner=runner,
            data_checker=data_checker,
            scummvm_executable=scummvm_executable,
        ):
            raise DataMissingError(
                f"required data for {title.id} is missing; refusing to bind card"
            )
        yield InstallProgress(
            step="data-ready",
            message=f"Verified required data for {title.label}",
        )

    cards = load_cards(cards_path)
    title_cards = dict(cards.title_cards)
    title_cards[normalized_uid] = EnrolledCard(uid=normalized_uid, title_id=title.id)
    save_cards(
        cards_path,
        CardsConfig(title_cards=title_cards, system_cards=dict(cards.system_cards)),
    )
    yield InstallProgress(
        step="bound",
        message=f"Bound {normalized_uid} to {title.id}",
        packages=(title.id,),
    )


def normalize_install_spec(
    install_spec: Mapping[str, Sequence[str]] | None,
) -> dict[str, tuple[str, ...]]:
    """Validate and normalize a declarative install spec."""
    if install_spec is None:
        return {}
    if not isinstance(install_spec, Mapping):
        raise InstallSpecError("install spec must be a mapping of manager -> packages")

    normalized: dict[str, tuple[str, ...]] = {}
    for manager_name, packages in install_spec.items():
        if manager_name not in _ALLOWED_INSTALL_MANAGERS:
            raise InstallSpecError(f"install manager {manager_name!r} is not allowed")
        if isinstance(packages, str) or not isinstance(packages, Sequence):
            raise InstallSpecError(
                f"install.{manager_name} must be a sequence of package names"
            )

        normalized_packages = tuple(
            _normalize_package_name(package, manager_name) for package in packages
        )
        if not normalized_packages:
            raise InstallSpecError(f"install.{manager_name} cannot be empty")
        normalized[manager_name] = normalized_packages

    return normalized


def has_network_connection(
    host: str = "1.1.1.1",
    port: int = 443,
    timeout: float = 2.0,
) -> bool:
    """Return whether the system can reach a network endpoint for installs."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def has_required_data(
    title: CatalogTitle,
    games_root: Path = DEFAULT_GAMES_ROOT,
    *,
    runner: CommandRunner = subprocess.run,
    data_checker: DataChecker | None = None,
    scummvm_executable: str = "scummvm",
) -> bool:
    """Return whether a title's required external data can be verified."""
    if title.data != "required":
        return True
    content_path = resolve_title_content_path(title, games_root)
    if content_path is None:
        return False
    if data_checker is not None:
        return data_checker(title, content_path)

    if title.type == "scummvm":
        if title.game_id is None or not content_path.exists():
            return False
        result = _run_command(
            [scummvm_executable, "--detect", f"--path={content_path}"],
            runner=runner,
        )
        return result.returncode == 0 and _scummvm_detect_output_has_game_id(
            result.stdout,
            title.game_id,
        )

    if title.type in {"dosbox", "ruffle"}:
        return content_path.exists()

    return False


def _manager_definitions() -> dict[str, _ManagerDefinition]:
    return {
        "apt": _ManagerDefinition(
            name="apt",
            check_argv=lambda package, _remote: [
                "dpkg-query",
                "--show",
                "--showformat=${Status}",
                package,
            ],
            install_argv=lambda packages, _remote: [
                "apt-get",
                "install",
                "-y",
                *packages,
            ],
            is_installed=lambda result: result.returncode == 0
            and "install ok installed" in result.stdout.lower(),
        ),
        "flatpak": _ManagerDefinition(
            name="flatpak",
            check_argv=lambda package, _remote: [
                "flatpak",
                "info",
                "--show-ref",
                package,
            ],
            install_argv=lambda packages, remote: [
                "flatpak",
                "install",
                "-y",
                remote,
                *packages,
            ],
            is_installed=lambda result: result.returncode == 0,
        ),
        "pip": _ManagerDefinition(
            name="pip",
            check_argv=lambda package, _remote: [
                sys.executable,
                "-m",
                "pip",
                "show",
                package,
            ],
            install_argv=lambda packages, _remote: [
                sys.executable,
                "-m",
                "pip",
                "install",
                *packages,
            ],
            is_installed=lambda result: result.returncode == 0,
        ),
    }


def _package_is_installed(
    manager: _ManagerDefinition,
    package: str,
    *,
    runner: CommandRunner,
    python_executable: str,
    flatpak_remote: str,
) -> bool:
    argv = manager.check_argv(package, flatpak_remote)
    if argv[:3] == [sys.executable, "-m", "pip"]:
        argv = [python_executable, *argv[1:]]
    result = _run_command(argv, runner=runner)
    return manager.is_installed(result)


def _run_command(
    argv: Iterable[str],
    *,
    runner: CommandRunner,
) -> subprocess.CompletedProcess[str]:
    return runner(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
    )


def _normalize_package_name(package: object, manager_name: str) -> str:
    if not isinstance(package, str):
        raise InstallSpecError(
            f"install.{manager_name} must contain only string package names"
        )
    normalized = package.strip()
    if not normalized:
        raise InstallSpecError(
            f"install.{manager_name} cannot contain empty package names"
        )
    return normalized


def _scummvm_detect_output_has_game_id(output: str, game_id: str) -> bool:
    expected = game_id.lower()
    for line in output.splitlines():
        fields = line.split()
        if fields and fields[0].rstrip(":").lower() == expected:
            return True
    return False
