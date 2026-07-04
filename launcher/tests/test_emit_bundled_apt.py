"""Tests for image/build_helpers/emit_bundled_apt.py."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

# Import the helper directly from the image directory (not installed).
_HELPER = (
    Path(__file__).parent.parent.parent
    / "image" / "build_helpers" / "emit_bundled_apt.py"
)
_spec = importlib.util.spec_from_file_location("emit_bundled_apt", _HELPER)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]
emit_bundled_apt = _mod.emit_bundled_apt


def _write_catalog(tmp_path: Path, titles: list[dict]) -> Path:
    data = {
        "meta": {"catalog_version": 1},
        "settings": {"games_root": "/games"},
        "titles": titles,
    }
    p = tmp_path / "catalog.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_bundled_exec_titles_emit_their_apt_packages(tmp_path: Path) -> None:
    catalog = _write_catalog(tmp_path, [
        {"id": "gcompris", "label": "GCompris", "type": "exec", "bundled": True,
         "install": {"apt": ["gcompris-qt"]}, "cmd": ["gcompris-qt", "--fullscreen"]},
        {"id": "tuxpaint", "label": "Tux Paint", "type": "exec", "bundled": True,
         "install": {"apt": ["tuxpaint", "tuxpaint-stamps-default"]},
         "cmd": ["tuxpaint", "--fullscreen"]},
    ])
    pkgs = emit_bundled_apt(catalog)
    assert pkgs == ["gcompris-qt", "tuxpaint", "tuxpaint-stamps-default"]


def test_non_bundled_titles_are_excluded(tmp_path: Path) -> None:
    catalog = _write_catalog(tmp_path, [
        {"id": "marble", "label": "Marble", "type": "exec", "bundled": False,
         "install": {"apt": ["marble"]}, "cmd": ["marble"]},
    ])
    assert emit_bundled_apt(catalog) == []


def test_bundled_title_with_no_apt_install_is_excluded(tmp_path: Path) -> None:
    # e.g. a web title with bundled: true but no apt install block
    catalog = _write_catalog(tmp_path, [
        {"id": "pbskids", "label": "PBS Kids", "type": "web", "bundled": True,
         "url": "https://pbskids.org/", "allowlist": ["pbskids.org"]},
    ])
    assert emit_bundled_apt(catalog) == []


def test_packages_are_deduplicated(tmp_path: Path) -> None:
    catalog = _write_catalog(tmp_path, [
        {"id": "a", "label": "A", "type": "exec", "bundled": True,
         "install": {"apt": ["shared-lib", "pkg-a"]}, "cmd": ["a"]},
        {"id": "b", "label": "B", "type": "exec", "bundled": True,
         "install": {"apt": ["shared-lib", "pkg-b"]}, "cmd": ["b"]},
    ])
    pkgs = emit_bundled_apt(catalog)
    assert pkgs.count("shared-lib") == 1
    assert "pkg-a" in pkgs
    assert "pkg-b" in pkgs


def test_real_catalog_emits_expected_bundled_packages() -> None:
    catalog_path = Path(__file__).parent.parent.parent / "catalog" / "catalog.yaml"
    pkgs = emit_bundled_apt(catalog_path)
    # gcompris and tuxpaint are bundled: true in the real catalog
    assert "gcompris-qt" in pkgs
    assert "tuxpaint" in pkgs
    assert "tuxpaint-stamps-default" in pkgs
    # marble is bundled: false — must not appear
    assert "marble" not in pkgs


def test_main_returns_zero_for_valid_catalog(tmp_path: Path) -> None:
    catalog = _write_catalog(tmp_path, [
        {"id": "gcompris", "label": "GCompris", "type": "exec", "bundled": True,
         "install": {"apt": ["gcompris-qt"]}, "cmd": ["gcompris-qt"]},
    ])
    rc = _mod.main([str(catalog)])
    assert rc == 0


def test_main_returns_nonzero_for_missing_catalog(tmp_path: Path) -> None:
    rc = _mod.main([str(tmp_path / "nonexistent.yaml")])
    assert rc != 0
