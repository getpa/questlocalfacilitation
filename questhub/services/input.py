"""Use case: send key/touch/text input to one or many headsets (docs/GUI_DESIGN.md §4.5)."""
from __future__ import annotations

import asyncio

from questhub.services.streaming import StreamingService
from questhub.stream import control


class InputService:
    def __init__(self, streaming: StreamingService):
        self.streaming = streaming

    def _sup(self, serial: str):
        sup = self.streaming.supervisor(serial)
        if sup is None or sup.state != "streaming":
            raise RuntimeError("その端末は表示中ではありません")
        return sup

    def _size(self, sup) -> tuple[int, int]:
        session = sup.hub.session
        if not session:
            raise RuntimeError("画面サイズが未確定です")
        return session.width, session.height

    async def key(self, serial: str, name: str) -> None:
        code = control.KEYCODES.get(name.upper())
        if code is None:
            raise ValueError(f"unknown key {name!r}")
        await self._sup(serial).send_control(control.key_press(code))

    async def text(self, serial: str, value: str) -> None:
        await self._sup(serial).send_control(control.text(value))

    async def touch(self, serial: str, action: str, x: float, y: float) -> None:
        """x, y in 0..1 of the video frame (resolution independent)."""
        sup = self._sup(serial)
        w, h = self._size(sup)
        act = {"down": control.ACTION_DOWN, "up": control.ACTION_UP, "move": control.ACTION_MOVE}[action]
        await sup.send_control(control.touch(act, round(x * (w - 1)), round(y * (h - 1)), w, h))

    async def tap(self, serial: str, x: float, y: float) -> None:
        sup = self._sup(serial)
        w, h = self._size(sup)
        await sup.send_control(control.tap(round(x * (w - 1)), round(y * (h - 1)), w, h))

    async def broadcast(self, serials: list[str], key: str | None = None,
                        tap: tuple[float, float] | None = None) -> dict:
        async def one(serial):
            try:
                if key:
                    await self.key(serial, key)
                elif tap:
                    await self.tap(serial, *tap)
                return serial, "ok"
            except (RuntimeError, ValueError) as exc:
                return serial, str(exc)
        return dict(await asyncio.gather(*(one(s) for s in serials)))
