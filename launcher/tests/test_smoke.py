import threading

from chipbit import __version__
from chipbit.cli import launcher_main, web_main


def test_package_has_version() -> None:
    assert __version__ == "0.1.0"


def test_launcher_main_runs_in_mock_mode(tmp_path) -> None:
    catalog_path = tmp_path / "catalog.yaml"
    mock_uids = tmp_path / "uids.txt"

    catalog_path.write_text(
        """
meta:
  catalog_version: 1
titles:
  - id: demo
    label: Demo
    type: exec
    bundled: true
    cmd: [demo-app]
""",
        encoding="utf-8",
    )
    mock_uids.write_text("", encoding="utf-8")

    assert (
        launcher_main(
            [
                "--catalog",
                str(catalog_path),
                "--cards",
                str(tmp_path / "cards.yaml"),
                "--mock-reader",
                str(mock_uids),
                "--control-port",
                "0",
            ]
        )
        == 0
    )


def test_web_main_runs(tmp_path) -> None:
    catalog_path = tmp_path / "catalog.yaml"
    stop = threading.Event()
    stop.set()

    catalog_path.write_text(
        """
meta:
      catalog_version: 1
settings:
      games_root: /games
titles:
      - id: demo
        label: Demo
        type: exec
        bundled: true
        cmd: [demo-app]
""",
        encoding="utf-8",
    )

    assert (
        web_main(
            [
                "--catalog",
                str(catalog_path),
                "--cards",
                str(tmp_path / "cards.yaml"),
                "--control-url",
                "http://127.0.0.1:8765",
                "--port",
                "0",
            ],
            stop_event=stop,
        )
        == 0
    )
