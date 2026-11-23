"""Quest Hub PoC: one headset, streamed to the browser and recorded to MKV.

  ./.mamba-env/bin/python -m questhub.poc                      # first adb device, http://127.0.0.1:8765
  ./.mamba-env/bin/python -m questhub.poc --serial 192.168.22.201:5555
  ./.mamba-env/bin/python -m questhub.poc --record-seconds 10  # headless: record and exit
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from questhub.adb.client import Adb
from questhub.stream.scrcpy_server import StreamOptions


async def first_device() -> str:
    out = await Adb().run("devices")
    devices = [line.split()[0] for line in out.splitlines()[1:] if line.strip().endswith("device")]
    if not devices:
        raise SystemExit("No authorized adb device. Connect a headset first (adb devices).")
    wifi = [d for d in devices if ":" in d]
    return (wifi or devices)[0]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="python -m questhub.poc", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serial", help="adb serial (default: first device, Wi-Fi preferred)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--record-dir", default="recordings/poc")
    parser.add_argument("--record-seconds", type=float, help="record for N seconds without the web UI, then exit")
    parser.add_argument("--max-size", type=int, default=1280)
    parser.add_argument("--max-fps", type=int, default=30)
    parser.add_argument("--bit-rate", type=int, default=4_000_000)
    parser.add_argument("--key-interval", type=float, default=1.0, help="requested key frame interval (s)")
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--no-audio-dup", action="store_true", help="capture output instead of duplicating playback")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


async def record_headless(session, seconds: float) -> dict:
    await session.start()
    path = await session.start_recording()
    print(f"[poc] recording {path} for {seconds:g}s", file=sys.stderr)
    await asyncio.sleep(seconds)
    stats = await session.stop_recording()
    status = session.status()
    await session.stop()
    return {"recording": stats, "status": status}


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from questhub.poc.session import DeviceSession

    serial = args.serial or asyncio.run(first_device())
    options = StreamOptions(max_size=args.max_size, max_fps=args.max_fps, video_bit_rate=args.bit_rate,
                            key_frame_interval=args.key_interval, audio=not args.no_audio,
                            audio_dup=not args.no_audio_dup)
    session = DeviceSession(serial, options, record_dir=args.record_dir)

    if args.record_seconds:
        result = asyncio.run(record_headless(session, args.record_seconds))
        print(json.dumps(result, indent=2, default=str))
        return 0 if not result["recording"].get("error") else 1

    import uvicorn
    from questhub.poc.app import create_app

    print(f"[poc] {serial} -> http://{args.host}:{args.port}", file=sys.stderr)
    uvicorn.run(create_app(session), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
