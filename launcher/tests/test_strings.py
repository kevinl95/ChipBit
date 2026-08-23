"""Locale loading, fallback, and the guarantees promised to translators."""

from __future__ import annotations

from pathlib import Path

import pytest

from chipbit import strings


@pytest.fixture(autouse=True)
def _english_between_tests():
    """Locale state is module-global; never leak it into the next test."""
    strings.use_english()
    yield
    strings.use_english()


def _locale_dir(tmp_path: Path, code: str, body: str) -> tuple[Path, ...]:
    (tmp_path / f"{code}.yaml").write_text(body, encoding="utf-8")
    return (tmp_path,)


def test_partial_translation_falls_back_key_by_key(tmp_path: Path) -> None:
    """The promise made to contributors: send ten strings, break nothing."""
    dirs = _locale_dir(tmp_path, "de", 'kiosk.idle.title: "Karte auflegen"\n')
    report = strings.load_locale("de", dirs)

    assert strings.t("kiosk.idle.title") == "Karte auflegen"
    assert strings.t("kiosk.idle.body") == strings.STRINGS["kiosk.idle.body"]
    assert report.translated == 1
    assert report.missing == len(strings.STRINGS) - 1


def test_placeholders_survive_translation(tmp_path: Path) -> None:
    dirs = _locale_dir(
        tmp_path, "de", 'kiosk.unknown.body_uid: "Karte {uid} fehlt"\n'
    )
    strings.load_locale("de", dirs)
    assert strings.t("kiosk.unknown.body_uid", uid="DEADBEEF") == "Karte DEADBEEF fehlt"


def test_language_tag_follows_the_loaded_locale(tmp_path: Path) -> None:
    dirs = _locale_dir(tmp_path, "de", 'kiosk.idle.title: "Karte"\n')
    assert strings.language_tag() == "en"
    strings.load_locale("de", dirs)
    assert strings.language_tag() == "de"


def test_missing_locale_file_degrades_to_english(tmp_path: Path) -> None:
    report = strings.load_locale("xx", (tmp_path,))
    assert report.path is None
    assert report.translated == 0
    assert strings.language_tag() == "en"
    assert strings.t("kiosk.idle.title") == strings.STRINGS["kiosk.idle.title"]


def test_user_dir_overrides_the_image_locale(tmp_path: Path) -> None:
    """A translator drops a file on a running Pi and it wins over the image's."""
    system, user = tmp_path / "system", tmp_path / "user"
    system.mkdir()
    user.mkdir()
    (system / "de.yaml").write_text('kiosk.idle.title: "Aus dem Image"\n')
    (user / "de.yaml").write_text('kiosk.idle.title: "Vom Übersetzer"\n')

    strings.load_locale("de", (system, user))
    assert strings.t("kiosk.idle.title") == "Vom Übersetzer"


def test_unknown_keys_are_ignored_not_fatal(tmp_path: Path) -> None:
    """A translation written against a newer ChipBit must not brick an older one."""
    dirs = _locale_dir(
        tmp_path,
        "de",
        'kiosk.idle.title: "Karte auflegen"\nnot.a.real.key: "was auch immer"\n',
    )
    report = strings.load_locale("de", dirs)
    assert report.unknown_keys == ("not.a.real.key",)
    assert strings.t("kiosk.idle.title") == "Karte auflegen"


def test_blank_and_non_string_values_fall_back(tmp_path: Path) -> None:
    """A half-filled file (empty value, or a stray number) must not blank a screen."""
    dirs = _locale_dir(
        tmp_path,
        "de",
        'kiosk.idle.title: ""\nkiosk.idle.body: "   "\nkiosk.enroll.title: 42\n',
    )
    strings.load_locale("de", dirs)
    for key in ("kiosk.idle.title", "kiosk.idle.body", "kiosk.enroll.title"):
        assert strings.t(key) == strings.STRINGS[key]


def test_malformed_yaml_does_not_raise(tmp_path: Path) -> None:
    dirs = _locale_dir(tmp_path, "de", "this: is: not: valid: yaml:\n")
    report = strings.load_locale("de", dirs)
    assert report.translated == 0
    assert strings.t("kiosk.idle.title") == strings.STRINGS["kiosk.idle.title"]


def test_unknown_key_still_raises(tmp_path: Path) -> None:
    """A typo in our own code should fail loudly rather than render nothing."""
    with pytest.raises(KeyError):
        strings.t("console.nope.missing")


REPO_LOCALES = Path(__file__).resolve().parents[2] / "locales"
SHIPPED = (
    sorted(p.stem for p in REPO_LOCALES.glob("*.yaml"))
    if REPO_LOCALES.is_dir()
    else []
)


@pytest.mark.parametrize("code", SHIPPED)
def test_shipped_locale_has_no_stale_keys(code: str) -> None:
    """A key we renamed must not linger in a translation, silently doing nothing."""
    report = strings.load_locale(code, (REPO_LOCALES,))
    assert report.unknown_keys == (), f"{code}: stale keys {report.unknown_keys}"


@pytest.mark.parametrize("code", SHIPPED)
def test_shipped_locale_covers_the_whole_kiosk(code: str) -> None:
    """Every shipped locale must finish the child-facing block it starts with."""
    strings.load_locale(code, (REPO_LOCALES,))
    for key in (k for k in strings.STRINGS if k.startswith("kiosk.")):
        assert strings.t(key) != strings.STRINGS[key], f"{code}: {key} left in English"


@pytest.mark.parametrize("code", SHIPPED)
def test_shipped_locale_is_registered_as_a_language(code: str) -> None:
    """A locale file nobody can select is dead weight."""
    assert code in strings.LANGUAGES, f"add {code!r} to LANGUAGES to make it selectable"


@pytest.mark.parametrize("code", SHIPPED)
def test_button_label_quoted_in_a_message_matches_the_button(code: str) -> None:
    """msg.added quotes the enroll button by name.

    In English these drifted apart once already -- the message told parents to
    press a button that had been renamed. A translation can reintroduce that
    per-language, and only a reader of that language would ever notice.
    """
    strings.load_locale(code, (REPO_LOCALES,))
    button = strings.t("console.tile.bind")
    message = strings.t("msg.added", label="X")
    assert button in message, (
        f"{code}: msg.added does not quote console.tile.bind ({button!r})"
    )


# --- the system-language half: what launched titles are told ---------------


def test_english_adds_no_environment() -> None:
    """English is the app default; don't set variables we don't need to."""
    assert strings.child_locale_env("en") == {}


def test_gettext_apps_work_without_a_generated_locale() -> None:
    """LANGUAGE needs no locale-gen, so TuxPaint/SuperTux translate regardless."""
    env = strings.child_locale_env("de", generated=frozenset())
    assert env == {"LANGUAGE": "de"}


def test_lang_is_only_set_once_the_locale_really_exists() -> None:
    """Qt titles need LANG, but LANG pointing at an ungenerated locale silently
    falls back to C -- worse than leaving it unset."""
    env = strings.child_locale_env("de", generated=frozenset({"de_DE.UTF-8"}))
    assert env == {"LANGUAGE": "de", "LANG": "de_DE.UTF-8"}


def test_unknown_language_is_ignored_not_exported() -> None:
    assert strings.child_locale_env("zz", generated=frozenset()) == {}


def test_language_round_trips_through_the_file(tmp_path: Path) -> None:
    target = tmp_path / "language"
    assert strings.read_language(target) == "en"
    strings.write_language("de", target)
    assert strings.read_language(target) == "de"


def test_unreadable_or_bogus_language_file_means_english(tmp_path: Path) -> None:
    target = tmp_path / "language"
    target.write_text("klingon\n")
    assert strings.read_language(target) == "en"
    assert strings.read_language(tmp_path / "missing") == "en"


def test_write_language_rejects_unknown_codes(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        strings.write_language("zz", tmp_path / "language")


def test_available_languages_follows_the_locale_files(tmp_path: Path) -> None:
    assert [c.code for c in strings.available_languages((tmp_path,))] == ["en"]
    (tmp_path / "de.yaml").write_text('kiosk.idle.title: "Karte"\n')
    (tmp_path / "notalang.yaml").write_text("x: y\n")
    codes = [c.code for c in strings.available_languages((tmp_path,))]
    assert codes == ["de", "en"]
    names = {c.code: c.name for c in strings.available_languages((tmp_path,))}
    assert names["de"] == "Deutsch"


def test_generated_locales_normalises_utf8_spelling() -> None:
    """`locale -a` prints de_DE.utf8; our table holds de_DE.UTF-8."""
    class R:
        returncode = 0
        stdout = "C\nde_DE.utf8\nen_US.utf8\n"
    assert "de_DE.UTF-8" in strings.generated_locales(lambda *a, **k: R())

