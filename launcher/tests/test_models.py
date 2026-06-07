from __future__ import annotations

import os
from pathlib import Path

import yaml

from chipbit.models import (
    CardsConfig,
    Catalog,
    CatalogSettings,
    ConfigLoadError,
    EnrolledCard,
    SystemCard,
    load_cards,
    load_catalog,
    normalize_uid,
    resolve_title_content_path,
    save_cards,
)


def test_repo_catalog_loads_every_supported_type() -> None:
    catalog_path = Path(__file__).resolve().parents[2] / "catalog" / "catalog.yaml"

    catalog = load_catalog(catalog_path)

    assert isinstance(catalog, Catalog)
    assert catalog.catalog_version == 1
    assert catalog.settings.games_root == Path("/games")
    assert {title.type for title in catalog.titles.values()} == {
        "exec",
        "scummvm",
        "dosbox",
        "web",
        "ruffle",
    }
    assert catalog.titles["gcompris"].cmd == ("gcompris-qt", "--fullscreen")
    assert catalog.titles["puttmoon"].game_id == "puttmoon"
    assert catalog.titles["puttmoon"].data_dir == "scummvm/puttmoon"
    assert catalog.titles["readerrabbit-dos"].conf == "readerrabbit/rr.conf"
    assert catalog.titles["pbskids"].allowlist == (
        "pbskids.org",
        "*.pbskids.org",
    )
    assert catalog.titles["mathblaster-flash"].swf == "flash/mathblaster.swf"


def test_load_catalog_skips_invalid_entries_with_warning(
    tmp_path: Path, caplog
) -> None:
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(
        """
meta:
  catalog_version: 1
titles:
  - id: valid-exec
    label: Valid Exec
    type: exec
    bundled: true
    install: {apt: [demo-app]}
    cmd: [demo-app, --fullscreen]
  - id: bad-web
    label: Bad Web
    type: web
    bundled: true
    url: https://example.test
  - id: bad-install
    label: Bad Install
    type: exec
    bundled: false
    install: {shell: [rm -rf /]}
    cmd: [demo]
  - id: bad-cmd
    label: Bad Cmd
    type: exec
    bundled: true
    cmd: demo
""",
        encoding="utf-8",
    )

    with caplog.at_level("WARNING"):
        catalog = load_catalog(catalog_path)

    assert list(catalog.titles) == ["valid-exec"]
    assert "bad-web" in caplog.text
    assert "bad-install" in caplog.text
    assert "bad-cmd" in caplog.text


def test_resolve_title_content_path_uses_games_root_and_scummvm_default() -> None:
    settings = CatalogSettings(games_root=Path("/mnt/games"))
    scummvm_title = catalog_title(
        id="puttmoon",
        type="scummvm",
        game_id="puttmoon",
    )
    dosbox_title = catalog_title(
        id="readerrabbit-dos",
        type="dosbox",
        conf="readerrabbit/rr.conf",
    )
    ruffle_title = catalog_title(
        id="mathblaster-flash",
        type="ruffle",
        swf="flash/mathblaster.swf",
    )

    assert resolve_title_content_path(scummvm_title, settings.games_root) == Path(
        "/mnt/games/scummvm/puttmoon"
    )
    assert resolve_title_content_path(dosbox_title, settings.games_root) == Path(
        "/mnt/games/readerrabbit/rr.conf"
    )
    assert resolve_title_content_path(ruffle_title, settings.games_root) == Path(
        "/mnt/games/flash/mathblaster.swf"
    )


def test_load_catalog_rejects_relative_games_root(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(
        """
meta:
  catalog_version: 1
settings:
  games_root: games
titles: []
""",
        encoding="utf-8",
    )

    try:
        load_catalog(catalog_path)
    except ConfigLoadError as exc:
        assert "games_root" in str(exc)
    else:
        raise AssertionError("Expected ConfigLoadError")


def test_normalize_uid_strips_separators_and_uppercases() -> None:
    assert normalize_uid("aa-bb cc:11\n") == "AABBCC11"


def test_load_cards_normalizes_uids_and_skips_invalid_entries(
    tmp_path: Path, caplog
) -> None:
    cards_path = tmp_path / "cards.yaml"
    cards_path.write_text(
        """
cards:
  aa-bb-cc: gcompris
  "11 22 33": tuxpaint
  bad-null:
  "aa bb cc": marble
system:
  home: "ff-ee-dd"
  unlock: "12 34 56"
  invalid: "00 11"
  "ff ee dd": shutdown
""",
        encoding="utf-8",
    )

    with caplog.at_level("WARNING"):
        config = load_cards(cards_path)

    assert config.title_id_for_uid("AA:BB:CC") == "gcompris"
    assert config.title_id_for_uid("11-22-33") == "tuxpaint"
    assert config.title_id_for_uid("00") is None
    assert config.system_action_for_uid("FFEEDD") == "home"
    assert config.system_action_for_uid("12-34-56") == "unlock"
    assert config.system_action_for_uid("9999") is None
    assert "bad-null" in caplog.text
    assert "duplicate uid" in caplog.text
    assert "invalid" in caplog.text


def test_load_cards_missing_file_is_empty(tmp_path: Path) -> None:
    config = load_cards(tmp_path / "missing.yaml")

    assert config == CardsConfig()


def test_save_cards_writes_atomically(tmp_path: Path, monkeypatch) -> None:
    cards_path = tmp_path / "cards.yaml"
    cards_path.write_text("cards:\n  OLD: stale\n", encoding="utf-8")

    replace_calls: list[tuple[str, Path]] = []
    real_replace = os.replace

    def recording_replace(src: str, dst: Path) -> None:
        replace_calls.append((src, dst))
        real_replace(src, dst)

    monkeypatch.setattr("chipbit.models.os.replace", recording_replace)

    config = CardsConfig(
        title_cards={"AABB": EnrolledCard(uid="AABB", title_id="gcompris")},
        system_cards={"home": SystemCard(action="home", uid="FFEE")},
    )

    save_cards(cards_path, config)

    assert len(replace_calls) == 1
    temp_src, dest = replace_calls[0]
    assert dest == cards_path
    assert Path(temp_src) != cards_path

    saved = yaml.safe_load(cards_path.read_text(encoding="utf-8"))
    assert saved == {
        "cards": {"AABB": "gcompris"},
        "system": {"home": "FFEE"},
    }


def test_load_catalog_rejects_invalid_top_level_structure(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text("titles: wrong\n", encoding="utf-8")

    try:
        load_catalog(catalog_path)
    except ConfigLoadError as exc:
        assert "titles" in str(exc)
    else:
        raise AssertionError("Expected ConfigLoadError")


def test_load_catalog_rejects_bool_for_integer_fields(tmp_path: Path, caplog) -> None:
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(
        """
meta:
  catalog_version: true
titles:
  - id: bad-age
    label: Bad Age
    type: exec
    bundled: true
    cmd: [demo]
    min_age: false
""",
        encoding="utf-8",
    )

    try:
        load_catalog(catalog_path)
    except ConfigLoadError as exc:
        assert "catalog_version" in str(exc)
    else:
        raise AssertionError("Expected ConfigLoadError")

    catalog_path.write_text(
        """
meta:
  catalog_version: 1
titles:
  - id: bad-age
    label: Bad Age
    type: exec
    bundled: true
    cmd: [demo]
    min_age: false
""",
        encoding="utf-8",
    )

    with caplog.at_level("WARNING"):
        catalog = load_catalog(catalog_path)

    assert catalog.titles == {}
    assert "min_age must be an integer" in caplog.text


def catalog_title(
    *,
    id: str,
    type: str,
    bundled: bool = False,
    game_id: str | None = None,
    conf: str | None = None,
    swf: str | None = None,
) -> object:
    from chipbit.models import CatalogTitle

    return CatalogTitle(
        id=id,
        label=id,
        type=type,
        bundled=bundled,
        game_id=game_id,
        conf=conf,
        swf=swf,
    )
