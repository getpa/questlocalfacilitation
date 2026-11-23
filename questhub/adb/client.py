"""Minimal asyncio wrapper around the adb CLI."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from questhub import paths


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

    def _argv(self, args: tuple[str, ...]) -> list[str]:
        argv = [self.binary]
        if self.serial:
            argv += ["-s", self.serial]
        return argv + list(args)

    async def run(self, *args: str, timeout: float = 30, check: bool = True) -> str:
        proc = await asyncio.create_subprocess_exec(
            *self._argv(args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
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

    async def spawn(self, *args: str) -> asyncio.subprocess.Process:
        """Start a long-running adb command (output merged, readable from stdout)."""
        return await asyncio.create_subprocess_exec(
            *self._argv(args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL)
