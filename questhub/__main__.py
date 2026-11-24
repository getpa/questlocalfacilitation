"""Quest Hub GUI: `./questhub.command` (macOS), `questhub.cmd` (Windows) or `python -m questhub`."""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
import signal
import subprocess
import sys

from questhub import __version__, paths


def _windows_chromium() -> str | None:
    """Chrome, else Edge (Chromium, preinstalled on Windows 10/11)."""
    import os
    roots = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    for rel in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"):
        for root in roots:
            if root and (Path(root) / rel).exists():
                return str(Path(root) / rel)
    return None


def open_browser(url: str) -> None:
    """Open the admin UI as an app window without an address bar (docs §3.1) when possible."""
    try:
        if sys.platform == "win32":
            browser = _windows_chromium()
            if browser:
                subprocess.Popen([browser, f"--app={url}"])
            else:
                import webbrowser
                webbrowser.open(url)
            return
        chrome = [p for p in ("/Applications/Google Chrome.app", str(Path.home() / "Applications/Google Chrome.app"))
                  if Path(p).exists()]
        if chrome:
            subprocess.Popen(["open", "-na", chrome[0], "--args", f"--app={url}"])
        else:
            subprocess.Popen(["open", url])
    except OSError as exc:
        print(f"[questhub] open {url} manually ({exc})", file=sys.stderr)


def install_stop_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    """Ctrl+C / termination ends the run cleanly (Windows has no loop signal handlers)."""
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: loop.call_soon_threadsafe(stop.set))  # console window closed


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
    install_stop_handlers(loop, stop)
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
