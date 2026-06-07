"""Catalog and card models plus YAML load/save helpers."""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

log = logging.getLogger(__name__)

TitleType = Literal["scummvm", "dosbox", "exec", "web", "ruffle"]
SystemAction = Literal["home", "unlock", "shutdown", "volume"]
DEFAULT_GAMES_ROOT = Path("/games")
_DEFAULT_SCUMMVM_DATA_DIR = Path("scummvm")

_ALLOWED_INSTALL_MANAGERS = frozenset({"apt", "flatpak", "pip"})
_ALLOWED_SYSTEM_ACTIONS = frozenset({"home", "unlock", "shutdown", "volume"})


class ConfigLoadError(ValueError):
    """Raised when a config file cannot be parsed at the file level."""


@dataclass(frozen=True)
class CatalogSettings:
    """Catalog-wide runtime settings."""

    games_root: Path = DEFAULT_GAMES_ROOT


@dataclass(frozen=True)
class CatalogTitle:
    """One launchable title from the catalog."""

    id: str
    label: str
    type: TitleType
    bundled: bool
    install: dict[str, tuple[str, ...]] = field(default_factory=dict)
    data: str | None = None
    data_dir: str | None = None
    game_id: str | None = None
    conf: str | None = None
    cmd: tuple[str, ...] = ()
    url: str | None = None
    allowlist: tuple[str, ...] = ()
    swf: str | None = None
    subject: str | None = None
    min_age: int | None = None
    blurb: str | None = None
    art: str | None = None


@dataclass(frozen=True)
class Catalog:
    """Parsed catalog file."""

    catalog_version: int | None
    settings: CatalogSettings = field(default_factory=CatalogSettings)
    titles: dict[str, CatalogTitle] = field(default_factory=dict)


@dataclass(frozen=True)
class EnrolledCard:
    """A user-enrolled UID to title binding."""

    uid: str
    title_id: str


@dataclass(frozen=True)
class SystemCard:
    """A reserved system card action."""

    action: SystemAction
    uid: str


@dataclass(frozen=True)
class CardsConfig:
    """Parsed cards file."""

    title_cards: dict[str, EnrolledCard] = field(default_factory=dict)
    system_cards: dict[str, SystemCard] = field(default_factory=dict)

    def title_id_for_uid(self, uid: str) -> str | None:
        """Resolve a scanned UID to a title id, if any."""
        card = self.title_cards.get(normalize_uid(uid))
        return None if card is None else card.title_id

    def system_action_for_uid(self, uid: str) -> str | None:
        """Resolve a scanned UID to a system action, if any."""
        normalized = normalize_uid(uid)
        for action, system_card in self.system_cards.items():
            if system_card.uid == normalized:
                return action
        return None


def normalize_uid(raw: str) -> str:
    """Uppercase and strip separators from a reader UID."""
    return "".join(ch for ch in raw.upper() if ch.isalnum())


def load_catalog(path: Path) -> Catalog:
    """Load the title catalog from YAML, skipping malformed entries."""
    data = _read_yaml_mapping(path, allow_missing=False)

    meta = data.get("meta") or {}
    if meta and not isinstance(meta, dict):
        raise ConfigLoadError("catalog 'meta' must be a mapping")

    raw_settings = data.get("settings") or {}
    if raw_settings and not isinstance(raw_settings, dict):
        raise ConfigLoadError("catalog 'settings' must be a mapping")

    raw_titles = data.get("titles") or []
    if not isinstance(raw_titles, list):
        raise ConfigLoadError("catalog 'titles' must be a list")

    version = meta.get("catalog_version") if isinstance(meta, dict) else None
    if version is not None and type(version) is not int:
        raise ConfigLoadError("catalog meta.catalog_version must be an integer")

    settings = _parse_catalog_settings(raw_settings)

    titles: dict[str, CatalogTitle] = {}
    for index, raw_title in enumerate(raw_titles, start=1):
        title = _parse_catalog_title(raw_title, index)
        if title is None:
            continue
        if title.id in titles:
            log.warning("Skipping catalog title '%s': duplicate id", title.id)
            continue
        titles[title.id] = title

    return Catalog(catalog_version=version, settings=settings, titles=titles)


def resolve_title_content_path(
    title: CatalogTitle,
    games_root: Path,
) -> Path | None:
    """Resolve a title's catalog-declared content path against games_root."""
    if title.type == "scummvm":
        relative_path = Path(title.data_dir or _DEFAULT_SCUMMVM_DATA_DIR / title.id)
    elif title.type == "dosbox" and title.conf is not None:
        relative_path = Path(title.conf)
    elif title.type == "ruffle" and title.swf is not None:
        relative_path = Path(title.swf)
    else:
        return None

    return games_root / relative_path


def load_cards(path: Path) -> CardsConfig:
    """Load enrolled and system cards from YAML, skipping malformed entries."""
    data = _read_yaml_mapping(path, allow_missing=True)

    if "cards" in data or "system" in data:
        raw_title_cards = data.get("cards") or {}
        raw_system_cards = data.get("system") or {}
    else:
        raw_title_cards = data
        raw_system_cards = {}

    if not isinstance(raw_title_cards, dict):
        raise ConfigLoadError("cards 'cards' section must be a mapping")
    if not isinstance(raw_system_cards, dict):
        raise ConfigLoadError("cards 'system' section must be a mapping")

    title_cards: dict[str, EnrolledCard] = {}
    system_cards: dict[str, SystemCard] = {}
    used_uids: set[str] = set()

    for raw_uid, raw_title_id in raw_title_cards.items():
        uid = _normalize_config_uid(raw_uid)
        if not uid:
            log.warning("Skipping enrolled card %r: invalid uid", raw_uid)
            continue

        title_id = _non_empty_string(raw_title_id)
        if title_id is None:
            log.warning("Skipping enrolled card %r: invalid catalog id", raw_uid)
            continue

        if uid in used_uids:
            log.warning("Skipping enrolled card %s: duplicate uid", uid)
            continue

        title_cards[uid] = EnrolledCard(uid=uid, title_id=title_id)
        used_uids.add(uid)

    for raw_key, raw_value in raw_system_cards.items():
        parsed = _parse_system_card_entry(raw_key, raw_value)
        if parsed is None:
            continue

        action, uid = parsed
        if action in system_cards:
            log.warning("Skipping system card '%s': duplicate action", action)
            continue
        if uid in used_uids:
            log.warning(
                "Skipping system card '%s': uid %s already assigned",
                action,
                uid,
            )
            continue

        system_cards[action] = SystemCard(action=action, uid=uid)
        used_uids.add(uid)

    return CardsConfig(title_cards=title_cards, system_cards=system_cards)


def save_cards(path: Path, config: CardsConfig) -> None:
    """Atomically write a cards config to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, dict[str, str]] = {}
    if config.title_cards:
        payload["cards"] = {
            uid: config.title_cards[uid].title_id for uid in sorted(config.title_cards)
        }
    if config.system_cards:
        payload["system"] = {
            action: config.system_cards[action].uid
            for action in sorted(config.system_cards)
        }

    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            yaml.safe_dump(payload, handle, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = handle.name

        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def _read_yaml_mapping(path: Path, *, allow_missing: bool) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        if allow_missing:
            return {}
        raise ConfigLoadError(f"missing config file: {path}") from None
    except OSError as exc:
        raise ConfigLoadError(f"failed to read config file: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigLoadError(f"failed to parse YAML: {path}") from exc

    if not isinstance(data, dict):
        raise ConfigLoadError(f"top-level config in {path} must be a mapping")
    return data


def _parse_catalog_title(raw_title: Any, index: int) -> CatalogTitle | None:
    if not isinstance(raw_title, dict):
        log.warning("Skipping catalog title #%d: entry is not a mapping", index)
        return None

    title_id = _non_empty_string(raw_title.get("id"))
    if title_id is None:
        log.warning("Skipping catalog title #%d: missing id", index)
        return None

    label = _non_empty_string(raw_title.get("label"))
    if label is None:
        log.warning("Skipping catalog title '%s': missing label", title_id)
        return None

    raw_type = _non_empty_string(raw_title.get("type"))
    if raw_type not in {"scummvm", "dosbox", "exec", "web", "ruffle"}:
        log.warning("Skipping catalog title '%s': invalid type %r", title_id, raw_type)
        return None

    bundled = raw_title.get("bundled")
    if not isinstance(bundled, bool):
        log.warning("Skipping catalog title '%s': bundled must be a boolean", title_id)
        return None

    install = _parse_install(raw_title.get("install"), title_id)
    if install is None:
        return None

    data = raw_title.get("data")
    if data is not None and data != "required":
        log.warning(
            "Skipping catalog title '%s': data must be 'required' when present",
            title_id,
        )
        return None

    data_dir = None
    if raw_title.get("data_dir") is not None:
        data_dir = _optional_string(raw_title.get("data_dir"), title_id, "data_dir")
        if data_dir is None:
            return None

    subject = _optional_string(raw_title.get("subject"), title_id, "subject")
    if raw_title.get("subject") is not None and subject is None:
        return None

    blurb = _optional_string(raw_title.get("blurb"), title_id, "blurb")
    if raw_title.get("blurb") is not None and blurb is None:
        return None

    art = _optional_string(raw_title.get("art"), title_id, "art")
    if raw_title.get("art") is not None and art is None:
        return None

    min_age = raw_title.get("min_age")
    if min_age is not None and type(min_age) is not int:
        log.warning("Skipping catalog title '%s': min_age must be an integer", title_id)
        return None

    game_id: str | None = None
    conf: str | None = None
    cmd: tuple[str, ...] = ()
    url: str | None = None
    allowlist: tuple[str, ...] = ()
    swf: str | None = None

    if raw_type == "scummvm":
        game_id = _optional_string(raw_title.get("game_id"), title_id, "game_id")
        if game_id is None:
            return None
    elif raw_type == "dosbox":
        conf = _optional_string(raw_title.get("conf"), title_id, "conf")
        if conf is None:
            return None
    elif raw_type == "exec":
        cmd = _parse_argv(raw_title.get("cmd"), title_id)
        if not cmd:
            return None
    elif raw_type == "web":
        url = _optional_string(raw_title.get("url"), title_id, "url")
        allowlist = _parse_allowlist(raw_title.get("allowlist"), title_id)
        if url is None or not allowlist:
            return None
    elif raw_type == "ruffle":
        swf = _optional_string(raw_title.get("swf"), title_id, "swf")
        if swf is None:
            return None

    return CatalogTitle(
        id=title_id,
        label=label,
        type=raw_type,
        bundled=bundled,
        install=install,
        data=data,
        data_dir=data_dir,
        game_id=game_id,
        conf=conf,
        cmd=cmd,
        url=url,
        allowlist=allowlist,
        swf=swf,
        subject=subject,
        min_age=min_age,
        blurb=blurb,
        art=art,
    )


def _parse_install(
    raw_install: Any, title_id: str
) -> dict[str, tuple[str, ...]] | None:
    if raw_install is None:
        return {}
    if not isinstance(raw_install, dict):
        log.warning("Skipping catalog title '%s': install must be a mapping", title_id)
        return None

    install: dict[str, tuple[str, ...]] = {}
    for manager, packages in raw_install.items():
        if manager not in _ALLOWED_INSTALL_MANAGERS:
            log.warning(
                "Skipping catalog title '%s': install manager %r is not allowed",
                title_id,
                manager,
            )
            return None
        if not isinstance(packages, list) or any(
            not isinstance(package, str) or not package.strip() for package in packages
        ):
            log.warning(
                "Skipping catalog title '%s': install.%s must be a list of strings",
                title_id,
                manager,
            )
            return None
        install[manager] = tuple(packages)

    return install


def _parse_catalog_settings(raw_settings: dict[str, Any]) -> CatalogSettings:
    games_root_value = raw_settings.get("games_root")
    if games_root_value is None:
        return CatalogSettings()

    games_root = _non_empty_string(games_root_value)
    if games_root is None:
        raise ConfigLoadError("catalog settings.games_root must be a non-empty string")

    parsed_games_root = Path(games_root)
    if not parsed_games_root.is_absolute():
        raise ConfigLoadError("catalog settings.games_root must be an absolute path")

    return CatalogSettings(games_root=parsed_games_root)


def _parse_argv(raw_argv: Any, title_id: str) -> tuple[str, ...]:
    if not isinstance(raw_argv, list) or not raw_argv:
        log.warning(
            "Skipping catalog title '%s': cmd must be a non-empty argv list",
            title_id,
        )
        return ()
    if any(not isinstance(arg, str) or not arg for arg in raw_argv):
        log.warning(
            "Skipping catalog title '%s': cmd must contain only strings",
            title_id,
        )
        return ()
    return tuple(raw_argv)


def _parse_allowlist(raw_allowlist: Any, title_id: str) -> tuple[str, ...]:
    if not isinstance(raw_allowlist, list) or not raw_allowlist:
        log.warning(
            "Skipping catalog title '%s': allowlist must be a non-empty list",
            title_id,
        )
        return ()
    if any(not isinstance(entry, str) or not entry.strip() for entry in raw_allowlist):
        log.warning(
            "Skipping catalog title '%s': allowlist must contain only strings",
            title_id,
        )
        return ()
    return tuple(raw_allowlist)


def _optional_string(value: Any, title_id: str, field_name: str) -> str | None:
    string_value = _non_empty_string(value)
    if string_value is None:
        log.warning(
            "Skipping catalog title '%s': %s must be a non-empty string",
            title_id,
            field_name,
        )
    return string_value


def _non_empty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _normalize_config_uid(raw_uid: Any) -> str:
    if raw_uid is None or isinstance(raw_uid, bool):
        return ""
    return normalize_uid(str(raw_uid))


def _parse_system_card_entry(raw_key: Any, raw_value: Any) -> tuple[str, str] | None:
    key = _non_empty_string(raw_key)
    value = _non_empty_string(raw_value)

    action: str | None = None
    uid: str | None = None

    if key is not None and key.lower() in _ALLOWED_SYSTEM_ACTIONS and value is not None:
        action = key.lower()
        uid = normalize_uid(value)
    elif (
        value is not None
        and value.lower() in _ALLOWED_SYSTEM_ACTIONS
        and key is not None
    ):
        action = value.lower()
        uid = normalize_uid(key)

    if action is None or uid is None or not uid:
        log.warning("Skipping system card %r: invalid action/uid pair", raw_key)
        return None

    return action, uid
