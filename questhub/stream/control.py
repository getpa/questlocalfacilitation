"""scrcpy control messages (ControlMessageReader.java @ v5.0.1). Pure functions."""
from __future__ import annotations

import struct

TYPE_INJECT_KEYCODE = 0
TYPE_INJECT_TEXT = 1
TYPE_INJECT_TOUCH_EVENT = 2
TYPE_BACK_OR_SCREEN_ON = 4
TYPE_RESET_VIDEO = 17

ACTION_DOWN, ACTION_UP, ACTION_MOVE = 0, 1, 2
POINTER_ID_FINGER = 0

# android.view.KeyEvent codes used by the GUI
KEYCODES = {
    "HOME": 3, "BACK": 4, "DPAD_UP": 19, "DPAD_DOWN": 20, "DPAD_LEFT": 21, "DPAD_RIGHT": 22,
    "DPAD_CENTER": 23, "VOLUME_UP": 24, "VOLUME_DOWN": 25, "POWER": 26, "ENTER": 66,
    "MENU": 82, "WAKEUP": 224, "SLEEP": 223, "APP_SWITCH": 187,
}

MAX_TEXT_BYTES = 300


def keycode(action: int, code: int, repeat: int = 0, meta: int = 0) -> bytes:
    return struct.pack(">BBiii", TYPE_INJECT_KEYCODE, action, code, repeat, meta)


def key_press(code: int) -> bytes:
    return keycode(ACTION_DOWN, code) + keycode(ACTION_UP, code)


def text(value: str) -> bytes:
    data = value.encode("utf-8")
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError(f"text longer than {MAX_TEXT_BYTES} bytes")
    return struct.pack(">BI", TYPE_INJECT_TEXT, len(data)) + data


def _u16_fixed(value: float) -> int:
    value = max(0.0, min(1.0, value))
    return 0xFFFF if value >= 1.0 else int(value * 0x10000)


def touch(action: int, x: int, y: int, width: int, height: int, pointer_id: int = POINTER_ID_FINGER,
          pressure: float = 1.0) -> bytes:
    """Coordinates are in the video frame (width x height must match the stream)."""
    return struct.pack(">BBqiiHHHii", TYPE_INJECT_TOUCH_EVENT, action, pointer_id, int(x), int(y),
                       width, height, _u16_fixed(pressure if action != ACTION_UP else 0.0), 0, 0)


def tap(x: int, y: int, width: int, height: int) -> bytes:
    return touch(ACTION_DOWN, x, y, width, height) + touch(ACTION_UP, x, y, width, height)


def swipe(points: list[tuple[int, int]], width: int, height: int) -> list[bytes]:
    if len(points) < 2:
        raise ValueError("a swipe needs two points")
    messages = [touch(ACTION_DOWN, *points[0], width, height)]
    messages += [touch(ACTION_MOVE, x, y, width, height) for x, y in points[1:-1]]
    messages.append(touch(ACTION_UP, *points[-1], width, height))
    return messages


def reset_video() -> bytes:
    return bytes([TYPE_RESET_VIDEO])
