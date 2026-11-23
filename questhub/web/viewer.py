"""Read-only browser viewer (HTTPS :8443) and certificate distribution page (HTTP :8766)."""
from __future__ import annotations

import html
from pathlib import Path

from fastapi import Body, FastAPI, Request, Response, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from questhub.application import Application
from questhub.web import auth
from questhub.web.media_ws import serve_media
from questhub.web.qr import qr_svg

STATIC = Path(__file__).parent / "static"
MAX_FOCUS_PER_VIEWER = 1
MAX_FOCUS_TOTAL = 8


def create_viewer_app(app: Application, secure: bool = True) -> FastAPI:
    api = FastAPI(title="Quest Hub Viewer", docs_url=None, redoc_url=None)
    api.mount("/static/common", StaticFiles(directory=STATIC / "common"), name="common")
    api.mount("/static/viewer", StaticFiles(directory=STATIC / "viewer"), name="viewer")
    focus_by_session: dict[str, int] = {}

    def session_of(request) -> str | None:
        token = request.cookies.get(auth.VIEWER_COOKIE)
        return token if app.viewer_auth.valid(token) else None

    @api.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @api.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        page = "index.html" if session_of(request) else "login.html"
        return (STATIC / "viewer" / page).read_text()

    @api.post("/login")
    async def login(request: Request, body: dict = Body(...)):
        client = request.client.host if request.client else "?"
        token = app.viewer_auth.login(str(body.get("code", "")), client)
        if not token:
            return JSONResponse({"detail": "アクセスコードが違います"}, status_code=403)
        response = JSONResponse({"ok": True})
        response.set_cookie(auth.VIEWER_COOKIE, token, httponly=True, secure=secure, samesite="strict",
                            max_age=auth.SESSION_TTL)
        return response

    @api.post("/logout")
    async def logout(request: Request):
        app.viewer_auth.logout(request.cookies.get(auth.VIEWER_COOKIE))
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(auth.VIEWER_COOKIE)
        return response

    @api.get("/api/devices")
    async def devices(request: Request):
        if not session_of(request):
            return JSONResponse({"detail": "login required"}, status_code=401)
        return app.viewer_devices()

    @api.websocket("/ws/media/{serial}")
    async def media(ws: WebSocket, serial: str, video: str = "keyframes", every: int = 1, audio: int = 0):
        token = session_of(ws)
        sup = app.streaming.supervisor(serial)
        if not token or sup is None:
            await ws.close(code=4401 if not token else 4404)
            return
        focus = video == "full"
        if focus and (focus_by_session.get(token, 0) >= MAX_FOCUS_PER_VIEWER
                      or sum(focus_by_session.values()) >= MAX_FOCUS_TOTAL):
            await ws.close(code=4429)  # too many full-rate streams
            return
        await ws.accept()
        if focus:
            focus_by_session[token] = focus_by_session.get(token, 0) + 1
        try:
            await serve_media(ws, sup.hub, video="full" if focus else "keyframes", every=max(1, every),
                              audio=bool(audio) and focus)
        finally:
            if focus:
                focus_by_session[token] -= 1
                if focus_by_session[token] <= 0:
                    focus_by_session.pop(token, None)

    return api


def create_ca_app(app: Application) -> FastAPI:
    """Plain-HTTP page that hands out the CA certificate (nothing secret here)."""
    api = FastAPI(title="Quest Hub certificate", docs_url=None, redoc_url=None)

    @api.get("/", response_class=HTMLResponse)
    async def page():
        viewer_url = app.viewer_urls()[0]
        template = (STATIC / "viewer" / "ca.html").read_text()
        return (template
                .replace("{{VIEWER_URL}}", html.escape(viewer_url))
                .replace("{{FINGERPRINT}}", html.escape(app.ca.fingerprint()))
                .replace("{{QR_VIEWER}}", qr_svg(viewer_url)))

    @api.get("/ca.crt")
    async def ca_pem():
        return Response(app.ca.ca_pem(), media_type="application/x-x509-ca-cert",
                        headers={"Content-Disposition": 'attachment; filename="QuestHubLocalCA.crt"'})

    @api.get("/ca.cer")
    async def ca_der():
        return Response(app.ca.ca_der(), media_type="application/x-x509-ca-cert",
                        headers={"Content-Disposition": 'attachment; filename="QuestHubLocalCA.cer"'})

    @api.get("/ca.mobileconfig")
    async def mobileconfig():
        return Response(app.ca.mobileconfig(), media_type="application/x-apple-aspen-config",
                        headers={"Content-Disposition": 'attachment; filename="QuestHub.mobileconfig"'})

    return api
