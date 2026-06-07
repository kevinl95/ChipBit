#!/usr/bin/env python3
"""
RFID kiosk launcher daemon.

Reads a HID-keyboard RFID reader, maps card UIDs to launch actions defined in
cards.yaml, and runs the target fullscreen. A "Home" card kills the running
app and returns to idle. A small localhost HTTP control API lets the
registration web UI request a one-shot UID capture and trigger config reloads.

Deps:  pip install evdev pyyaml
Run:   sudo python3 kiosk_launcher.py --config /boot/kiosk/cards.yaml
       (root, or a user in the `input` group, is needed to grab the reader)

Deliberate simplifications worth knowing:
  * Config changes are picked up by polling the file mtime (every POLL_SECS)
    plus an explicit POST /reload, to avoid an inotify dependency.
  * `web` cards launch Chromium in --app kiosk mode; true domain allow-listing
    is enforced *outside* this daemon (DNS / proxy), as discussed.
  * The reader device is grab()'d so card digits never leak to the console or
    the focused app.
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml
import evdev
from evdev import ecodes

POLL_SECS = 2.0
STOP_GRACE_SECS = 5.0
CAPTURE_TIMEOUT_SECS = 30.0

log = logging.getLogger("kiosk")

# --- HID scancode -> character (numeric readers; hex-capable readers covered) -
_KEYMAP = {getattr(ecodes, f"KEY_{d}"): d for d in "0123456789"}
_KEYMAP.update({getattr(ecodes, f"KEY_{c}"): c for c in "ABCDEF"})
_ENTER = {ecodes.KEY_ENTER, ecodes.KEY_KPENTER}

# Required fields per card type. `label` is required for all.
_REQUIRED = {
    "scummvm": ("game_id",),
    "dosbox": ("conf",),
    "exec": ("cmd",),
    "web": ("url",),
    "ruffle": ("swf",),
    "system": ("action",),
}


def normalize_uid(raw: str) -> str:
    """Uppercase, strip separators -> stable key regardless of reader format."""
    return "".join(ch for ch in raw.upper() if ch.isalnum())


# ---------------------------------------------------------------- config -----
class Config:
    def __init__(self, path: Path):
        self.path = path
        self.mtime = 0.0
        self.settings: dict = {}
        self.cards: dict = {}

    def load(self) -> bool:
        """Parse + validate into new dicts, swap atomically. Returns True on (re)load."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError as e:
            log.error("config stat failed: %s", e)
            return False
        if mtime == self.mtime:
            return False

        try:
            data = yaml.safe_load(self.path.read_text()) or {}
        except (OSError, yaml.YAMLError) as e:
            log.error("config parse failed, keeping previous: %s", e)
            return False

        settings = data.get("settings", {}) or {}
        raw_cards = data.get("cards", {}) or {}
        cards = {}
        for key, card in raw_cards.items():
            ok, why = self._validate(card)
            if not ok:
                log.warning("skipping card %s: %s", key, why)
                continue
            cards[normalize_uid(str(key))] = card

        self.settings, self.cards, self.mtime = settings, cards, mtime
        log.info("loaded %d card(s)", len(cards))
        return True

    @staticmethod
    def _validate(card) -> tuple[bool, str]:
        if not isinstance(card, dict):
            return False, "not a mapping"
        t = card.get("type")
        if t not in _REQUIRED:
            return False, f"unknown type {t!r}"
        if not card.get("label"):
            return False, "missing label"
        for f in _REQUIRED[t]:
            if f not in card:
                return False, f"type {t} missing {f!r}"
        if t == "exec" and not isinstance(card["cmd"], list):
            return False, "cmd must be an argv list"
        return True, ""


def build_command(card: dict, settings: dict):
    """Return argv (list) for a launchable card, or None for non-launch types."""
    t = card["type"]
    if t == "scummvm":
        return ["scummvm", "-f", card["game_id"]]
    if t == "dosbox":
        binary = settings.get("dosbox_bin", "dosbox-staging")
        return [binary, "-conf", card["conf"], "-fullscreen"]
    if t == "exec":
        return list(card["cmd"])
    if t == "ruffle":
        return ["ruffle", "--fullscreen", card["swf"]]
    if t == "web":
        chrome = settings.get("chromium_bin", "chromium-browser")
        return [
            chrome, "--kiosk", "--noerrdialogs", "--no-first-run",
            "--disable-pinch", "--disable-features=TranslateUI",
            "--overscroll-history-navigation=0",
            f"--app={card['url']}",
        ]
    return None


# --------------------------------------------------------------- launcher ----
class Launcher:
    """Owns the child process and the launch/capture state machine."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.current: subprocess.Popen | None = None
        self.current_label: str | None = None
        self.unlocked = False  # config mode gate, read by the registration UI

        # one-shot capture handshake with the HTTP control API
        self._capture_armed = False
        self._capture_uid: str | None = None
        self._capture_event = threading.Event()

    # ---- called by the reader thread on every completed scan ----
    def on_scan(self, uid: str):
        uid = normalize_uid(uid)
        with self.lock:
            if self._capture_armed:
                self._capture_uid = uid
                self._capture_armed = False
                self._capture_event.set()
                log.info("captured uid %s for registration", uid)
                return
            card = self.cfg.cards.get(uid)

        if card is None or not card.get("enabled", True):
            self._on_unknown(uid)
            return
        if card["type"] == "system":
            self._system(card.get("action"))
            return
        self._launch(card)

    # ---- launch / stop ----
    def _launch(self, card: dict):
        policy = self.cfg.settings.get("while_running", "home_only")
        if self.is_running():
            if policy in ("home_only", "ignore"):
                log.info("ignoring %s (app running, policy=%s)", card["label"], policy)
                return
            self._stop_current()  # policy == "swap"

        argv = build_command(card, self.cfg.settings)
        if not argv:
            return
        try:
            proc = subprocess.Popen(argv, start_new_session=True)
        except (OSError, ValueError) as e:
            log.error("launch failed for %s: %s", card["label"], e)
            return
        with self.lock:
            self.current = proc
            self.current_label = card["label"]
        log.info("launched %s (pid %d)", card["label"], proc.pid)
        threading.Thread(target=self._reap, args=(proc,), daemon=True).start()

    def _reap(self, proc: subprocess.Popen):
        proc.wait()
        with self.lock:
            if self.current is proc:
                self.current = None
                self.current_label = None
                log.info("app exited -> idle")

    def _stop_current(self):
        with self.lock:
            proc = self.current
        if not proc or proc.poll() is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=STOP_GRACE_SECS)
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        with self.lock:
            if self.current is proc:
                self.current = None
                self.current_label = None
        log.info("stopped current app -> idle")

    # ---- helpers ----
    def is_running(self) -> bool:
        with self.lock:
            return self.current is not None and self.current.poll() is None

    def _on_unknown(self, uid: str):
        mode = self.cfg.settings.get("on_unknown_card", "prompt")
        log.info("unknown card %s (on_unknown=%s)", uid, mode)
        hook = self.cfg.settings.get("unknown_hook")
        if mode == "prompt" and hook:
            subprocess.Popen([hook, uid], start_new_session=True)

    def _system(self, action: str | None):
        log.info("system action: %s", action)
        if action == "home":
            self._stop_current()
        elif action == "unlock":
            with self.lock:
                self.unlocked = True
        elif action == "shutdown" and self.cfg.settings.get("allow_shutdown"):
            subprocess.Popen(["systemctl", "poweroff"], start_new_session=True)
        elif action == "volume":  # tap-to-toggle mute as a simple default
            subprocess.Popen(["amixer", "set", "Master", "toggle"],
                             start_new_session=True)

    # ---- capture handshake for the registration UI ----
    def capture(self, timeout: float = CAPTURE_TIMEOUT_SECS) -> str | None:
        with self.lock:
            self._capture_uid = None
            self._capture_armed = True
            self._capture_event.clear()
        got = self._capture_event.wait(timeout)
        with self.lock:
            self._capture_armed = False
            return self._capture_uid if got else None

    def status(self) -> dict:
        with self.lock:
            return {
                "running": self.is_running(),
                "current": self.current_label,
                "unlocked": self.unlocked,
                "cards": len(self.cfg.cards),
            }


# ---------------------------------------------------------- reader thread ----
def reader_loop(device_path: str, launcher: Launcher, stop: threading.Event):
    """Open + grab the HID reader, accumulate digits until Enter, dispatch."""
    while not stop.is_set():
        try:
            dev = evdev.InputDevice(device_path)
            dev.grab()
            log.info("reader open: %s (%s)", device_path, dev.name)
        except OSError as e:
            log.warning("reader open failed (%s), retrying in 3s", e)
            time.sleep(3)
            continue

        buf = []
        try:
            for event in dev.read_loop():
                if stop.is_set():
                    break
                if event.type != ecodes.EV_KEY or event.value != 1:  # keydown only
                    continue
                if event.code in _ENTER:
                    if buf:
                        launcher.on_scan("".join(buf))
                    buf = []
                elif event.code in _KEYMAP:
                    buf.append(_KEYMAP[event.code])
        except OSError as e:
            log.warning("reader read error (%s), reopening", e)
        finally:
            try:
                dev.ungrab()
            except Exception:
                pass
        time.sleep(1)


# ----------------------------------------------------------- control API -----
def make_handler(launcher: Launcher, cfg: Config):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/status":
                self._send(200, launcher.status())
            elif self.path == "/cards":
                self._send(200, cfg.cards)
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path == "/capture":
                uid = launcher.capture()
                if uid:
                    self._send(200, {"uid": uid})
                else:
                    self._send(408, {"error": "no card within timeout"})
            elif self.path == "/reload":
                changed = cfg.load()
                self._send(200, {"reloaded": changed, "cards": len(cfg.cards)})
            else:
                self._send(404, {"error": "not found"})

        def log_message(self, *args):  # quiet the default stderr spam
            pass

    return Handler


def config_poller(cfg: Config, stop: threading.Event):
    while not stop.wait(POLL_SECS):
        cfg.load()


# ----------------------------------------------------------------- main ------
def main():
    ap = argparse.ArgumentParser(description="RFID kiosk launcher daemon")
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--control-port", type=int, default=8765)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = Config(args.config)
    if not cfg.load():
        log.error("could not load initial config; exiting")
        return 1

    launcher = Launcher(cfg)
    stop = threading.Event()

    device = cfg.settings.get("reader", {}).get("device")
    if not device:
        log.error("settings.reader.device not set; exiting")
        return 1

    threading.Thread(target=reader_loop, args=(device, launcher, stop),
                     daemon=True).start()
    threading.Thread(target=config_poller, args=(cfg, stop), daemon=True).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", args.control_port),
                                make_handler(launcher, cfg))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log.info("control API on 127.0.0.1:%d", args.control_port)

    def shutdown(signum, frame):
        log.info("signal %d -> shutting down", signum)
        stop.set()
        launcher._stop_current()
        httpd.shutdown()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        while not stop.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        shutdown(signal.SIGINT, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
