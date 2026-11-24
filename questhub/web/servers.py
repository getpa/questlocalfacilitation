"""Run the admin (always), viewer and certificate servers in one event loop."""
from __future__ import annotations

import asyncio
import logging

import uvicorn

from questhub.application import Application
from questhub.web.admin import create_admin_app
from questhub.web.viewer import create_ca_app, create_viewer_app

log = logging.getLogger(__name__)


def _server(api, host: str, port: int, **kwargs) -> uvicorn.Server:
    config = uvicorn.Config(api, host=host, port=port, log_level="warning", lifespan="off",
                            ws_ping_interval=20, ws_ping_timeout=20, **kwargs)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # the launcher handles signals
    return server


class Servers:
    def __init__(self, app: Application):
        self.app = app
        app.servers = self
        self.admin = _server(create_admin_app(app), app.settings.admin_host, app.settings.admin_port)
        self._tasks: dict[str, asyncio.Task] = {}
        self._viewer: list[uvicorn.Server] = []
        self.viewer_error: str | None = None

    @property
    def viewer_running(self) -> bool:
        return any(t for name, t in self._tasks.items() if name != "admin" and not t.done())

    async def start(self) -> None:
        self._tasks["admin"] = asyncio.create_task(self.admin.serve())
        for _ in range(100):
            if self.admin.started:
                break
            if self._tasks["admin"].done():
                self._tasks["admin"].result()  # raise (e.g. port in use)
            await asyncio.sleep(0.05)
        if self.app.settings.viewer_enabled:
            await self.set_viewer(True)

    async def set_viewer(self, enabled: bool) -> None:
        for server in self._viewer:
            server.should_exit = True
        for name in [n for n in self._tasks if n != "admin"]:
            await asyncio.gather(self._tasks.pop(name), return_exceptions=True)
        self._viewer.clear()
        self.viewer_error = None
        if not enabled:
            return
        settings = self.app.settings
        try:
            if settings.viewer_insecure_http:
                viewer = _server(create_viewer_app(self.app, secure=False), "0.0.0.0", settings.viewer_port)
            else:
                hostnames = [self.app.hostname(), "localhost"]
                ips = self.app.lan_addresses() + ["127.0.0.1"]
                cert, key = self.app.ca.ensure_server_cert(hostnames, sorted(set(ips)))
                viewer = _server(create_viewer_app(self.app, secure=True), "0.0.0.0", settings.viewer_port,
                                 ssl_certfile=str(cert), ssl_keyfile=str(key))
            ca = _server(create_ca_app(self.app), "0.0.0.0", settings.ca_port)
        except Exception as exc:  # certificate problems must not take the admin GUI down
            self.viewer_error = f"{type(exc).__name__}: {exc}"
            log.exception("viewer start failed")
            return
        self._viewer = [viewer, ca]
        self._tasks["viewer"] = asyncio.create_task(viewer.serve())
        self._tasks["ca"] = asyncio.create_task(ca.serve())
        await asyncio.sleep(0.3)
        for name in ("viewer", "ca"):
            task = self._tasks[name]
            if task.done() and task.exception():
                self.viewer_error = f"{name}: {task.exception()}"

    async def stop(self) -> None:
        await self.set_viewer(False)
        self.admin.should_exit = True
        if "admin" in self._tasks:
            await asyncio.gather(self._tasks.pop("admin"), return_exceptions=True)
