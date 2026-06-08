from __future__ import annotations

from types import SimpleNamespace

import pytest

from chipbit.reader import _decode_key_event, ecodes


@pytest.mark.skipif(ecodes is None, reason="evdev not installed")
def test_decode_key_event_accepts_numeric_keypad_digits() -> None:
    event = SimpleNamespace(
        type=ecodes.EV_KEY,
        value=1,
        code=ecodes.KEY_KP7,
    )

    assert _decode_key_event(event) == "7"
