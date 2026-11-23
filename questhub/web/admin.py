"""Admin GUI + API (http://127.0.0.1:8765). Routing and validation only (§3.2 rule 2)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from questhub.application import Application
from questhub.core import events as ev
from questhub.stream.quality import PRESETS
from questhub.web import auth
from questhub.web.media_ws import serve_media
from questhub.web.qr import qr_svg

STATIC = Path(__file__).parent / "static"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _err(exc: Exception, status: int = 409):
    raise HTTPException(status, str(exc)) from None


def create_admin_app(app: Application) -> FastAPI:
    api = FastAPI(title="Quest Hub", docs_url=None, redoc_url=None)
    api.mount("/static/common", StaticFiles(directory=STATIC / "common"), name="common")
    api.mount("/static/host", StaticFiles(directory=STATIC / "host"), name="host")

    @api.middleware("http")
    async def guard(request: Request, call_next):
        if request.method not in SAFE_METHODS and not auth.same(request.headers.get(auth.ADMIN_HEADER),
                                                                app.admin_token):
            return JSONResponse({"detail": "invalid admin token"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    def ws_allowed(ws: WebSocket) -> bool:
        origin = ws.headers.get("origin", "")
        host = ws.headers.get("host", "")
        return origin in (f"http://{host}", "") and auth.same(ws.query_params.get("token"), app.admin_token)

    @api.get("/", response_class=HTMLResponse)
    async def index():
        html = (STATIC / "host/index.html").read_text()
        return html.replace("{{ADMIN_TOKEN}}", app.admin_token)

    # ------------------------------------------------------------ state

    @api.get("/api/state")
    async def state():
        return {
            "devices": app.devices(),
            "usb": app.manager.usb_devices(),
            "warnings": app.registry.warnings(),
            "recording": app.recording.status(),
            "settings": app.settings.to_dict(),
            "presets": app.presets(),
            "viewer": viewer_info(),
            "scanning": app.provisioning.scanning,
            "auto_suspended": app.streaming.auto_suspended,
        }

    @api.get("/api/events/recent")
    async def recent(limit: int = 100):
        return app.bus.recent(limit)

    @api.websocket("/ws/events")
    async def events_ws(ws: WebSocket):
        if not ws_allowed(ws):
            await ws.close(code=4403)
            return
        await ws.accept()
        sub = app.bus.subscribe()
        try:
            async for event in sub:
                await ws.send_json(event.to_dict())
        except Exception:
            pass
        finally:
            sub.close()

    # ------------------------------------------------------------ devices

    @api.patch("/api/devices/{serial}")
    async def edit_device(serial: str, body: dict = Body(...)):
        if "quality" in body:
            try:
                await app.streaming.set_quality(serial, body.pop("quality"))
            except (RuntimeError, ValueError) as exc:
                _err(exc)
        return app.registry.update(serial, **body)

    @api.delete("/api/devices/{serial}")
    async def delete_device(serial: str):
        try:
            await app.streaming.stop_stream(serial)
        except RuntimeError as exc:
            _err(exc)
        app.registry.delete(serial)
        app.manager.status.pop(serial, None)
        return {"ok": True}

    @api.post("/api/devices/{serial}/connect")
    async def connect(serial: str):
        try:
            return {"state": await app.manager.connect_now(serial)}
        except ValueError as exc:
            _err(exc, 400)

    @api.post("/api/devices/{serial}/disconnect")
    async def disconnect(serial: str):
        try:
            await app.streaming.stop_stream(serial)
        except RuntimeError as exc:
            _err(exc)
        await app.manager.disconnect(serial)
        return {"ok": True}

    @api.post("/api/usb/enable-wifi")
    async def enable_wifi(body: dict = Body(...)):
        usb_ids = body.get("usb_ids") or [d["usb_id"] for d in app.manager.usb_devices() if d["state"] == "device"]
        if not usb_ids:
            _err(RuntimeError("USB 接続の端末がありません"), 400)
        return await app.provisioning.enable_wifi(usb_ids, int(body.get("port") or 5555))

    @api.post("/api/scan")
    async def scan(body: dict = Body(default={})):
        try:
            return await app.provisioning.scan(body.get("cidr") or None)
        except (RuntimeError, ValueError) as exc:
            _err(exc, 400)

    @api.get("/api/registry/export.tsv")
    async def export_tsv():
        return PlainTextResponse(app.registry.export_tsv(), media_type="text/tab-separated-values",
                                 headers={"Content-Disposition": 'attachment; filename="quest_devices.tsv"'})

    @api.post("/api/registry/import")
    async def import_tsv(request: Request):
        text = (await request.body()).decode("utf-8", errors="replace")
        return {"imported": app.registry.import_tsv(text)}

    # ------------------------------------------------------------ network

    @api.get("/api/network")
    async def network():
        return app.network_summary()

    @api.get("/api/network/export.csv")
    async def network_csv():
        return PlainTextResponse(app.registry.export_network_csv(), media_type="text/csv",
                                 headers={"Content-Disposition": 'attachment; filename="quest_network.csv"'})

    @api.post("/api/warnings/{warning_id}/ack")
    async def ack(warning_id: int):
        row = app.registry.ack_warning(warning_id)
        if row is None:
            _err(KeyError("no such warning"), 404)
        return {"ok": True}

    # ------------------------------------------------------------ streams

    @api.post("/api/streams/start-all")
    async def start_all():
        return {"started": await app.streaming.start_all()}

    @api.post("/api/streams/stop-all")
    async def stop_all():
        try:
            await app.streaming.stop_all()
        except RuntimeError as exc:
            _err(exc)
        return {"ok": True}

    @api.post("/api/streams/quality-all")
    async def quality_all(body: dict = Body(...)):
        preset = body.get("preset")
        if preset not in PRESETS:
            _err(ValueError(f"unknown preset {preset!r}"), 400)
        result = await app.streaming.set_quality_all(preset)
        app.save_settings()
        return result

    @api.post("/api/streams/{serial}/{action}")
    async def stream_action(serial: str, action: str, body: dict = Body(default={})):
        try:
            if action == "start":
                app.streaming.start_stream(serial)
            elif action == "stop":
                await app.streaming.stop_stream(serial)
            elif action == "quality":
                await app.streaming.set_quality(serial, body.get("preset", ""))
            elif action == "keyframe":
                sup = app.streaming.supervisor(serial)
                if sup:
                    await sup.request_key_frame()
            elif action == "native":
                await app.streaming.open_native(serial)
            else:
                _err(ValueError(f"unknown action {action}"), 404)
        except (RuntimeError, ValueError, FileNotFoundError) as exc:
            _err(exc)
        return {"ok": True}

    @api.websocket("/ws/media/{serial}")
    async def media(ws: WebSocket, serial: str, video: str = "full", every: int = 1, audio: int = 0):
        sup = app.streaming.supervisor(serial)
        if not ws_allowed(ws) or sup is None:
            await ws.close(code=4404 if sup is None else 4403)
            return
        await ws.accept()
        await serve_media(ws, sup.hub, video=video, every=every, audio=bool(audio))

    # ------------------------------------------------------------ recording

    @api.get("/api/recordings")
    async def recordings():
        return {**app.recording.status(), "sessions": app.recording.list_sessions()}

    @api.post("/api/recordings/start")
    async def recording_start(body: dict = Body(default={})):
        try:
            return await app.recording.start(body.get("serials") or None, body.get("name") or None)
        except RuntimeError as exc:
            _err(exc)

    @api.post("/api/recordings/stop")
    async def recording_stop(body: dict = Body(default={})):
        return await app.recording.stop(body.get("serials") or None)

    # ------------------------------------------------------------ input

    @api.post("/api/input/broadcast")
    async def broadcast(body: dict = Body(...)):
        serials = body.get("serials") or [s for s, sup in app.streaming.supervisors.items() if sup.state == "streaming"]
        tap = (float(body["x"]), float(body["y"])) if "x" in body else None
        return await app.input.broadcast(serials, key=body.get("key"), tap=tap)

    @api.post("/api/input/{serial}/{kind}")
    async def input_one(serial: str, kind: str, body: dict = Body(...)):
        try:
            if kind == "key":
                await app.input.key(serial, body["key"])
            elif kind == "text":
                await app.input.text(serial, body["text"])
            elif kind == "tap":
                await app.input.tap(serial, float(body["x"]), float(body["y"]))
            else:
                _err(ValueError(kind), 404)
        except (RuntimeError, ValueError, KeyError) as exc:
            _err(exc, 409)
        return {"ok": True}

    @api.websocket("/ws/control/{serial}")
    async def control_ws(ws: WebSocket, serial: str):
        if not ws_allowed(ws):
            await ws.close(code=4403)
            return
        await ws.accept()
        try:
            while True:
                msg = await ws.receive_json()
                try:
                    await app.input.touch(serial, msg["action"], float(msg["x"]), float(msg["y"]))
                except (RuntimeError, ValueError, KeyError) as exc:
                    await ws.send_json({"error": str(exc)})
        except Exception:
            return

    # ------------------------------------------------------------ settings / viewer / session

    def viewer_info() -> dict:
        servers = app.servers
        return {
            "enabled": app.settings.viewer_enabled,
            "running": bool(servers and servers.viewer_running),
            "code": app.viewer_auth.code,
            "urls": app.viewer_urls(),
            "ca_url": f"http://{app.lan_addresses()[0]}:{app.settings.ca_port}/",
            "fingerprint": app.ca.fingerprint() if app.ca.ca_cert_path.exists() else None,
            "sessions": app.viewer_auth.session_count,
            "insecure": app.settings.viewer_insecure_http,
            "error": servers.viewer_error if servers else None,
        }

    @api.get("/api/viewer")
    async def viewer():
        info = viewer_info()
        info["qr_viewer"] = qr_svg(info["urls"][0])
        info["qr_ca"] = qr_svg(info["ca_url"])
        return info

    @api.post("/api/viewer/{action}")
    async def viewer_action(action: str):
        if action == "rotate-code":
            app.viewer_auth.rotate()
        elif action in ("enable", "disable"):
            app.settings.viewer_enabled = action == "enable"
            app.save_settings()
            if app.servers:
                await app.servers.set_viewer(app.settings.viewer_enabled)
        else:
            _err(ValueError(action), 404)
        return viewer_info()

    @api.get("/api/settings")
    async def get_settings():
        return app.settings.to_dict()

    @api.put("/api/settings")
    async def put_settings(body: dict = Body(...)):
        body.pop("admin_host", None)
        try:
            changed = app.settings.update(body)
        except (TypeError, ValueError) as exc:
            _err(exc, 400)
        app.save_settings()
        if {"viewer_port", "ca_port", "viewer_insecure_http"} & set(changed) and app.servers \
                and app.settings.viewer_enabled:
            await app.servers.set_viewer(False)
            await app.servers.set_viewer(True)
        return {"changed": changed, "settings": app.settings.to_dict()}

    @api.post("/api/session/end")
    async def end_session(body: dict = Body(default={})):
        return await app.end_session(bool(body.get("back_to_usb")))

    @api.get("/api/ca.crt")
    async def ca_download():
        return Response(app.ca.ca_pem(), media_type="application/x-pem-file",
                        headers={"Content-Disposition": 'attachment; filename="QuestHubLocalCA.crt"'})

    return api
