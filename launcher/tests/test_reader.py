from __future__ import annotations

from types import SimpleNamespace

import pytest

from chipbit.reader import _decode_key_event, _looks_like_rfid_reader, ecodes, find_rfid_reader


@pytest.mark.skipif(ecodes is None, reason="evdev not installed")
def test_decode_key_event_accepts_numeric_keypad_digits() -> None:
    event = SimpleNamespace(
        type=ecodes.EV_KEY,
        value=1,
        code=ecodes.KEY_KP7,
    )

    assert _decode_key_event(event) == "7"


# ---------------------------------------------------------------------------
# find_rfid_reader / _looks_like_rfid_reader
# ---------------------------------------------------------------------------

def _rfid_caps() -> dict:
    """Minimal evdev capabilities for a typical HID RFID dongle."""
    return {
        ecodes.EV_KEY: [
            ecodes.KEY_0, ecodes.KEY_1, ecodes.KEY_2, ecodes.KEY_3,
            ecodes.KEY_4, ecodes.KEY_5, ecodes.KEY_6, ecodes.KEY_7,
            ecodes.KEY_8, ecodes.KEY_9,
            ecodes.KEY_A, ecodes.KEY_B, ecodes.KEY_C,
            ecodes.KEY_D, ecodes.KEY_E, ecodes.KEY_F,
            ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT,
            ecodes.KEY_ENTER,
        ],
    }


def _make_device(caps: dict):
    closed = []
    dev = SimpleNamespace(capabilities=lambda: caps, close=lambda: closed.append(1))
    return dev


@pytest.mark.skipif(ecodes is None, reason="evdev not installed")
def test_rfid_caps_are_recognised_as_rfid_reader() -> None:
    assert _looks_like_rfid_reader(_rfid_caps()) is True


@pytest.mark.skipif(ecodes is None, reason="evdev not installed")
def test_device_with_mouse_axes_is_rejected() -> None:
    caps = _rfid_caps()
    caps[ecodes.EV_REL] = [ecodes.REL_X, ecodes.REL_Y]
    assert _looks_like_rfid_reader(caps) is False


@pytest.mark.skipif(ecodes is None, reason="evdev not installed")
def test_device_with_absolute_axes_is_rejected() -> None:
    caps = _rfid_caps()
    caps[ecodes.EV_ABS] = [ecodes.ABS_X, ecodes.ABS_Y]
    assert _looks_like_rfid_reader(caps) is False


@pytest.mark.skipif(ecodes is None, reason="evdev not installed")
def test_device_without_ev_key_is_rejected() -> None:
    assert _looks_like_rfid_reader({}) is False


@pytest.mark.skipif(ecodes is None, reason="evdev not installed")
def test_device_without_enter_key_is_rejected() -> None:
    caps = _rfid_caps()
    caps[ecodes.EV_KEY] = [ecodes.KEY_0, ecodes.KEY_1]
    assert _looks_like_rfid_reader(caps) is False


@pytest.mark.skipif(ecodes is None, reason="evdev not installed")
def test_device_without_any_digit_key_is_rejected() -> None:
    caps = {ecodes.EV_KEY: [ecodes.KEY_ENTER]}
    assert _looks_like_rfid_reader(caps) is False


@pytest.mark.skipif(ecodes is None, reason="evdev not installed")
def test_full_keyboard_marker_causes_rejection() -> None:
    caps = _rfid_caps()
    caps[ecodes.EV_KEY] = list(caps[ecodes.EV_KEY]) + [ecodes.KEY_ESC]
    assert _looks_like_rfid_reader(caps) is False


@pytest.mark.skipif(ecodes is None, reason="evdev not installed")
def test_find_rfid_reader_returns_path_of_matching_device() -> None:
    dev = _make_device(_rfid_caps())

    result = find_rfid_reader(
        list_devices=lambda: ["/dev/input/event3"],
        input_device_factory=lambda path: dev,
    )
    assert result == "/dev/input/event3"


@pytest.mark.skipif(ecodes is None, reason="evdev not installed")
def test_find_rfid_reader_skips_non_matching_device_and_returns_none() -> None:
    keyboard_caps = _rfid_caps()
    keyboard_caps[ecodes.EV_KEY] = list(keyboard_caps[ecodes.EV_KEY]) + [ecodes.KEY_ESC]

    result = find_rfid_reader(
        list_devices=lambda: ["/dev/input/event0"],
        input_device_factory=lambda path: _make_device(keyboard_caps),
    )
    assert result is None


@pytest.mark.skipif(ecodes is None, reason="evdev not installed")
def test_find_rfid_reader_returns_none_when_no_devices() -> None:
    result = find_rfid_reader(list_devices=lambda: [])
    assert result is None


@pytest.mark.skipif(ecodes is None, reason="evdev not installed")
def test_find_rfid_reader_skips_unreadable_device_and_continues() -> None:
    def factory(path: str):
        if path == "/dev/input/event0":
            raise OSError("permission denied")
        return _make_device(_rfid_caps())

    result = find_rfid_reader(
        list_devices=lambda: ["/dev/input/event0", "/dev/input/event1"],
        input_device_factory=factory,
    )
    assert result == "/dev/input/event1"


@pytest.mark.skipif(ecodes is None, reason="evdev not installed")
def test_find_rfid_reader_returns_first_match_in_sorted_order() -> None:
    result = find_rfid_reader(
        list_devices=lambda: ["/dev/input/event5", "/dev/input/event2"],
        input_device_factory=lambda path: _make_device(_rfid_caps()),
    )
    assert result == "/dev/input/event2"
