"""Quest Hub GUI: `./questhub.command` or `./.mamba-env/bin/python -m questhub`."""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
import signal
import subprocess
import sys

from questhub import __version__, paths


def open_browser(url: str) -> None:
    chrome = [p for p in ("/Applications/Google Chrome.app", str(Path.home() / "Applications/Google Chrome.app"))
              if Path(p).exists()]
    try:
        if chrome:  # app window without address bar (docs §3.1)
            subprocess.Popen(["open", "-na", chrome[0], "--args", f"--app={url}"])
        else:
            subprocess.Popen(["open", url])
    except OSError as exc:
        print(f"[questhub] open {url} manually ({exc})", file=sys.stderr)


async def main_async(args) -> int:
    from questhub.application import AlreadyRunning, Application
    from questhub.web.servers import Servers

    app = Application(data_dir=args.data_dir)
    if args.port:
        app.settings.admin_port = args.port
    try:
        app.lock.acquire()
    except AlreadyRunning as exc:
        print(f"[questhub] {exc}. Open http://127.0.0.1:{app.settings.admin_port}/", file=sys.stderr)
        if not args.no_browser:
            open_browser(f"http://127.0.0.1:{app.settings.admin_port}/")
        return 1
    servers = Servers(app)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await app.start()
        await servers.start()
        url = f"http://127.0.0.1:{app.settings.admin_port}/"
        print(f"[questhub] {__version__} running at {url} (Ctrl+C to quit)", file=sys.stderr)
        if not args.no_browser:
            open_browser(url)
        await stop.wait()
    finally:
        print("[questhub] shutting down…", file=sys.stderr)
        await servers.stop()
        await app.stop()
        app.lock.release()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="questhub", description="Quest Hub GUI")
    parser.add_argument("--port", type=int, help="admin port (default 8765)")
    parser.add_argument("--data-dir", default=None, help=f"data directory (default {paths.DATA_DIR})")
    parser.add_argument("--no-browser", action="store_true", help="do not open the GUI window")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
