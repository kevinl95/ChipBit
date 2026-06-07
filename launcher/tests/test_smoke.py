from chipbit import __version__
from chipbit.cli import launcher_main, web_main


def test_package_has_version() -> None:
    assert __version__ == "0.1.0"


def test_launcher_main_runs_in_mock_mode(capsys) -> None:
    assert launcher_main(["--mock-reader"]) == 0
    assert "M2" in capsys.readouterr().out


def test_web_main_runs(capsys) -> None:
    assert web_main([]) == 0
    assert "M4" in capsys.readouterr().out
