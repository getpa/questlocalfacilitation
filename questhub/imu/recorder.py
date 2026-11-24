"""Record the Quest headset IMU to CSV next to a video recording (docs/GUI_DESIGN.md §4.11).

The headset IMU is an Android sensor (``oculus.sensor.hmd_imu.2``) that needs no permission, but no adb
command streams sensor values. ``questhub-imu.dex`` (built from ``java/questhub/ImuStream.java``) runs on the
headset with ``app_process`` as the adb shell user, like scrcpy's server, and prints averaged samples.

The sensor has its own clock, so the helper prints ``# sync`` lines (sensor time of the newest sample, then
an immediate flush) about once a second. The time this computer reads a sync line minus the sensor time is
the clock offset plus the delivery latency; the smallest value over the last ``SYNC_WINDOW`` syncs is used,
which follows slow clock drift and ignores latency spikes. ``host_time`` in the CSV is therefore on this
computer's clock, the same clock as the recording's ``started_at``, within a few milliseconds on a good LAN.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import hashlib
import logging
from pathlib import Path
import time
from typing import Callable

from questhub.adb.client import Adb

log = logging.getLogger(__name__)

DEX = Path(__file__).with_name("questhub-imu.dex")
MAIN_CLASS = "questhub.ImuStream"
CSV_COLUMNS = "host_time,t_us,n,temp_c,ax,ay,az,gx,gy,gz"
IMU_COLUMNS = "t_us,n,temp_c,ax,ay,az,gx,gy,gz"
SYNC_WINDOW = 30
MAX_BACKOFF = 10.0
FLUSH_INTERVAL = 1.0


def dex_sha256() -> str:
    return hashlib.sha256(DEX.read_bytes()).hexdigest()


def remote_dex() -> str:
    return f"/data/local/tmp/questhub-imu-{dex_sha256()[:12]}.dex"


def helper_command(rate_hz: int) -> str:
    return f"CLASSPATH={remote_dex()} app_process / {MAIN_CLASS} --rate {int(rate_hz)}"


async def push_helper(adb: Adb) -> bool:
    """Copy the helper to the headset unless that exact version is already there."""
    path = remote_dex()
    found = await adb.shell(f"ls {path} 2>/dev/null", timeout=10, check=False)
    if path in found:
        return False
    await adb.run("push", str(DEX), path, timeout=60)
    return True


class ClockMap:
    """Sensor time (us) -> this computer's Unix time, from (receive time, sensor time) pairs."""

    def __init__(self, window: int = SYNC_WINDOW):
        self._offsets: deque[float] = deque(maxlen=window)
        self._last_us: int | None = None

    def add(self, sensor_us: int, received_at: float) -> None:
        if self._last_us is not None and sensor_us < self._last_us:
            self._offsets.clear()   # the sensor clock restarted (headset reboot): old offsets no longer apply
        self._last_us = sensor_us
        self._offsets.append(received_at - sensor_us / 1e6)

    @property
    def ready(self) -> bool:
        return bool(self._offsets)

    def to_host(self, sensor_us: int) -> float:
        return sensor_us / 1e6 + min(self._offsets)


@dataclass
class ImuStats:
    rows: int = 0
    samples: int = 0
    restarts: int = 0
    started_at: float | None = None
    error: str | None = None
    sensor: str | None = None
    last_row_at: float | None = None
    messages: list[str] = field(default_factory=list)


class ImuParser:
    """Turns the helper's lines into CSV lines. Rows before the first sync line wait for it."""

    def __init__(self, clock: ClockMap):
        self.clock = clock
        self.pending: list[list[str]] = []
        self.sensor: str | None = None
        self.columns_ok = True

    def feed(self, line: str, received_at: float, stats: ImuStats) -> list[str]:
        if not line:
            return []
        if line.startswith("#"):
            body = line[1:].strip()
            if body.startswith("sync "):
                fields = dict(part.split("=", 1) for part in body.split()[1:] if "=" in part)
                if "t" in fields:
                    self.clock.add(int(fields["t"]), received_at)
                    ready, self.pending = self.pending, []
                    return [self._csv(row, stats) for row in ready]
            elif body.startswith("sensor "):
                self.sensor = body[len("sensor "):]
                stats.sensor = self.sensor
            elif body.startswith("columns "):
                self.columns_ok = body.split()[1] == IMU_COLUMNS
                if not self.columns_ok:
                    stats.error = f"unexpected columns: {body}"
            elif body.startswith("error "):
                stats.error = body[len("error "):]
            return []
        if not line[0].isdigit():
            stats.messages = (stats.messages + [line])[-20:]   # e.g. app_process warnings
            return []
        row = line.split(",")
        if not self.columns_ok or len(row) != 9:
            return []
        if not self.clock.ready:
            self.pending.append(row)
            return []
        return [self._csv(row, stats)]

    def _csv(self, row: list[str], stats: ImuStats) -> str:
        stats.rows += 1
        stats.samples += int(row[1])
        stats.last_row_at = time.time()
        return f"{self.clock.to_host(int(row[0])):.6f}," + ",".join(row)


class ImuRecorder:
    """One headset's IMU to ``path`` until stop(); reconnects (with a ``# restart`` line) after a loss."""

    def __init__(self, path: str | Path, adb_fn: Callable[[], Adb | None], rate_hz: int = 100,
                 serial: str = "", spawn=None, clock=time.time):
        self.path = Path(path)
        self.adb_fn = adb_fn
        self.rate_hz = int(rate_hz)
        self.serial = serial
        self.stats = ImuStats()
        self._spawn = spawn or self._spawn_adb
        self._clock = clock
        self._task: asyncio.Task | None = None
        self._proc = None
        self._file = None

    def start(self) -> None:
        self.stats.started_at = self._clock()
        self._file = self.path.open("w", encoding="utf-8", newline="\n")
        self._file.write("# questhub imu 1\n"
                         f"# device {self.serial}\n"
                         f"# rate_hz {self.rate_hz} (mean of the n samples the headset delivered in each bin)\n"
                         "# host_time: Unix time (s) on the recording computer; t_us: headset IMU clock (us)\n"
                         "# temp_c: C, ax ay az: m/s^2, gx gy gz: rad/s (headset IMU axes)\n"
                         f"{CSV_COLUMNS}\n")
        self._file.flush()
        self._task = asyncio.create_task(self._run())

    async def _spawn_adb(self, adb: Adb):
        await adb.shell(f"pkill -f {MAIN_CLASS}", timeout=10, check=False)   # a helper left over from a crash
        await push_helper(adb)
        return await adb.spawn("exec-out", helper_command(self.rate_hz))

    async def _run(self) -> None:
        backoff = 1.0
        clock = ClockMap()   # the sensor clock is the same across restarts
        first = True
        while True:
            try:
                adb = self.adb_fn()
            except Exception as exc:   # the device left the registry, etc.: keep trying until stop()
                self.stats.error = str(exc) or type(exc).__name__
                adb = None
            if adb is None:
                await asyncio.sleep(backoff)
                continue
            try:
                self._proc = await self._spawn(adb)
                if not first:
                    self.stats.restarts += 1
                    self._write([f"# restart {self.stats.restarts}"])
                first = False
                parser = ImuParser(clock)
                last_flush = time.monotonic()
                got_rows = self.stats.rows
                async for raw in self._proc.stdout:
                    lines = parser.feed(raw.decode("utf-8", errors="replace").strip(), self._clock(), self.stats)
                    if lines:
                        self._write(lines, flush=False)
                        backoff = 1.0
                    if time.monotonic() - last_flush >= FLUSH_INTERVAL:
                        self._file.flush()
                        last_flush = time.monotonic()
                self._file.flush()
                await self._proc.wait()
                if self.stats.rows == got_rows and not self.stats.error:
                    self.stats.error = "IMU helper exited without data: " + " / ".join(self.stats.messages[-3:])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats.error = str(exc)
                log.debug("%s: IMU stream failed: %s", self.serial, exc)
            finally:
                await self._terminate()
            await asyncio.sleep(backoff)
            backoff = min(MAX_BACKOFF, backoff * 2)

    def _write(self, lines: list[str], flush: bool = True) -> None:
        if self._file:
            self._file.write("\n".join(lines) + "\n")
            if flush:
                self._file.flush()

    async def _terminate(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), 5)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

    async def stop(self) -> ImuStats:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._terminate()
        if self._spawn == self._spawn_adb:
            # the helper notices the closed pipe only at its next write; stop it now
            try:
                adb = self.adb_fn()
                if adb is not None:
                    await adb.shell(f"pkill -f {MAIN_CLASS}", timeout=10, check=False)
            except Exception as exc:
                log.debug("%s: could not stop the IMU helper: %s", self.serial, exc)
        if self._file:
            self._file.close()
            self._file = None
        if self.stats.rows:
            self.stats.error = None   # a recovered hiccup is not an error of the recording
        return self.stats
