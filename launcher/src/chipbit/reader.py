"""RFID reader implementations for the ChipBit runtime."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from typing import TextIO

from .models import normalize_uid

try:
    import evdev
    from evdev import ecodes
except ImportError:  # pragma: no cover - dependency is present in normal installs
    evdev = None
    ecodes = None

log = logging.getLogger(__name__)

RECONNECT_DELAY_SECS = 3.0
REOPEN_DELAY_SECS = 1.0

if ecodes is not None:
    _KEYMAP = {getattr(ecodes, f"KEY_{digit}"): digit for digit in "0123456789"}
    _KEYMAP.update(
        {
            getattr(ecodes, f"KEY_KP{digit}"): digit
            for digit in "0123456789"
            if hasattr(ecodes, f"KEY_KP{digit}")
        }
    )
    _KEYMAP.update({getattr(ecodes, f"KEY_{letter}"): letter for letter in "ABCDEF"})
    _ENTER = {ecodes.KEY_ENTER, ecodes.KEY_KPENTER}

    # Keys present on every full keyboard but never on a bare RFID reader.
    # If a device has any of these we know it is not an RFID reader.
    _FULL_KEYBOARD_MARKERS: frozenset[int] = frozenset(
        filter(
            None,
            [
                getattr(ecodes, k, None)
                for k in (
                    "KEY_ESC",
                    "KEY_TAB",
                    "KEY_SPACE",
                    "KEY_BACKSPACE",
                    "KEY_F1",
                    "KEY_LEFTCTRL",
                    "KEY_RIGHTCTRL",
                    "KEY_LEFTALT",
                    "KEY_RIGHTALT",
                    # Non-hex letters that no RFID reader would emit
                    "KEY_Q",
                    "KEY_W",
                    "KEY_T",
                    "KEY_Y",
                    "KEY_U",
                    "KEY_I",
                    "KEY_O",
                    "KEY_P",
                )
            ],
        )
    )
else:  # pragma: no cover - only used if evdev import fails
    _KEYMAP: dict[int, str] = {}
    _ENTER: set[int] = set()
    _FULL_KEYBOARD_MARKERS: frozenset[int] = frozenset()


def find_rfid_reader(
    *,
    list_devices: Callable[[], Iterable[str]] | None = None,
    input_device_factory: Callable[[str], object] | None = None,
) -> str | None:
    """Return the path of the first attached RFID reader, or None.

    Identifies RFID readers by their evdev capability shape: they look like
    HID keyboards but carry only digit, hex-letter, and Enter keys — not the
    full set present on a real keyboard.  Mouse and joystick devices (EV_REL /
    EV_ABS) are rejected immediately.

    Both parameters exist for testing only; production code leaves them None.
    """
    if evdev is None or ecodes is None:
        return None

    _list = list_devices or evdev.list_devices
    _factory = input_device_factory or evdev.InputDevice

    for path in sorted(_list()):
        dev = None
        try:
            dev = _factory(path)
            caps = dev.capabilities()
        except OSError:
            continue
        finally:
            if dev is not None:
                try:
                    dev.close()
                except Exception:  # pragma: no cover
                    pass

        if _looks_like_rfid_reader(caps):
            log.info("auto-detected RFID reader: %s", path)
            return path

    return None


def _looks_like_rfid_reader(caps: dict) -> bool:
    if ecodes.EV_KEY not in caps:
        return False
    if ecodes.EV_REL in caps or ecodes.EV_ABS in caps:
        return False

    keys = frozenset(caps[ecodes.EV_KEY])

    if not (keys & _ENTER):
        return False
    if not (keys & frozenset(_KEYMAP)):
        return False
    if keys & _FULL_KEYBOARD_MARKERS:
        return False

    return True


class MockReader:
    """Read UIDs from stdin or any file-like/iterable source."""

    def __init__(self, source: TextIO | Iterable[str]) -> None:
        self._source = source

    def read_uids(self, stop: threading.Event) -> Iterator[str]:
        """Yield normalized UIDs from the configured source until EOF or stop."""
        if hasattr(self._source, "readline"):
            while not stop.is_set():
                line = self._source.readline()
                if line == "":
                    break
                uid = normalize_uid(line)
                if uid:
                    yield uid
            return

        for line in self._source:
            if stop.is_set():
                break
            uid = normalize_uid(str(line))
            if uid:
                yield uid


class EvdevReader:
    """Read UIDs from a grabbed HID keyboard RFID reader."""

    def __init__(
        self,
        device_path: str,
        *,
        input_device_factory: Callable[[str], object] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        reconnect_delay_secs: float = RECONNECT_DELAY_SECS,
        reopen_delay_secs: float = REOPEN_DELAY_SECS,
    ) -> None:
        if evdev is None or ecodes is None:
            raise RuntimeError("evdev is required for hardware reader support")

        self.device_path = device_path
        self._input_device_factory = input_device_factory or evdev.InputDevice
        self._sleep = sleep
        self._reconnect_delay_secs = reconnect_delay_secs
        self._reopen_delay_secs = reopen_delay_secs

    def read_uids(self, stop: threading.Event) -> Iterator[str]:
        """Yield normalized UIDs from the RFID reader, auto-reconnecting."""
        while not stop.is_set():
            try:
                device = self._input_device_factory(self.device_path)
                device.grab()
                log.info("reader open: %s (%s)", self.device_path, device.name)
            except OSError as exc:
                log.warning("reader open failed (%s), retrying in 3s", exc)
                self._sleep(self._reconnect_delay_secs)
                continue

            buffer: list[str] = []
            try:
                for event in device.read_loop():
                    if stop.is_set():
                        break

                    decoded = _decode_key_event(event)
                    if decoded is None:
                        continue
                    if decoded == "\n":
                        if buffer:
                            yield normalize_uid("".join(buffer))
                        buffer = []
                    else:
                        buffer.append(decoded)
            except OSError as exc:
                log.warning("reader read error (%s), reopening", exc)
            finally:
                try:
                    device.ungrab()
                except Exception:  # pragma: no cover - best effort cleanup
                    pass

            self._sleep(self._reopen_delay_secs)


def pump_reader(
    reader: MockReader | EvdevReader,
    on_scan: Callable[[str], None],
    stop: threading.Event,
) -> None:
    """Dispatch UIDs from a reader into the launcher state machine."""
    for uid in reader.read_uids(stop):
        if stop.is_set():
            break
        on_scan(uid)


def _decode_key_event(event: object) -> str | None:
    if ecodes is None:
        return None
    if event.type != ecodes.EV_KEY or event.value != 1:
        return None
    if event.code in _ENTER:
        return "\n"
    return _KEYMAP.get(event.code)
