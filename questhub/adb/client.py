"""Minimal asyncio wrapper around the adb CLI.

All short commands share one concurrency limit so 50 headsets polling at
once do not spawn 100 adb processes (docs/GUI_DESIGN.md §6).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from questhub import paths

_limit = 8
_semaphore: asyncio.Semaphore | None = None


def set_concurrency(limit: int) -> None:
    global _limit, _semaphore
    _limit = max(1, int(limit))
    _semaphore = None


def _sem() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_limit)
    return _semaphore


class AdbError(RuntimeError):
    def __init__(self, args: tuple[str, ...], returncode: int, output: str):
        super().__init__(f"adb {' '.join(args)} failed ({returncode}): {output.strip()[:500]}")
        self.returncode = returncode
        self.output = output


@dataclass
class Adb:
    serial: str | None = None
    binary: str | None = None

    def __post_init__(self) -> None:
        if self.binary is None:
            self.binary = paths.binary("adb", "ADB_BIN")

    def for_serial(self, serial: str | None) -> "Adb":
        return Adb(serial=serial, binary=self.binary)

    def _argv(self, args: tuple[str, ...]) -> list[str]:
        argv = [self.binary]
        if self.serial:
            argv += ["-s", self.serial]
        return argv + list(args)

    async def run(self, *args: str, timeout: float = 30, check: bool = True) -> str:
        async with _sem():
            proc = await asyncio.create_subprocess_exec(
                *self._argv(args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise AdbError(args, -1, "timed out") from None
        text = out.decode("utf-8", errors="replace")
        if check and proc.returncode != 0:
            raise AdbError(args, proc.returncode, text)
        return text

    async def shell(self, command: str, timeout: float = 30, check: bool = True) -> str:
        return await self.run("shell", command, timeout=timeout, check=check)

    async def getprop(self, name: str, timeout: float = 10) -> str:
        return (await self.shell(f"getprop {name}", timeout=timeout)).strip()

    async def spawn(self, *args: str) -> asyncio.subprocess.Process:
        """Start a long-running adb command (output merged, readable from stdout)."""
        return await asyncio.create_subprocess_exec(
            *self._argv(args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL)
