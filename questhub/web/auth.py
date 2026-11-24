"""Admin / viewer access control (docs/GUI_DESIGN.md §4.8).

Admin: bound to 127.0.0.1; state-changing requests need the per-run token that
is embedded in the admin page (other web pages cannot read it), and WebSockets
must come from the admin origin.

Viewer: a short access code exchanged for an HttpOnly session cookie.
Rotating the code ends every viewer session.
"""
from __future__ import annotations

import hmac
import secrets
import time

ADMIN_HEADER = "x-qh-token"
VIEWER_COOKIE = "qh_viewer"
SESSION_TTL = 12 * 3600


def new_token() -> str:
    return secrets.token_urlsafe(24)


def new_access_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a, b)


class ViewerAuth:
    def __init__(self, max_sessions: int = 64):
        self.code = new_access_code()
        self.max_sessions = max_sessions
        self._sessions: dict[str, float] = {}
        self._failures: dict[str, list[float]] = {}

    def rotate(self) -> str:
        self.code = new_access_code()
        self._sessions.clear()
        return self.code

    def login(self, code: str, client: str) -> str | None:
        now = time.time()
        recent = [t for t in self._failures.get(client, []) if now - t < 60]
        if len(recent) >= 10:      # simple brute-force brake: 10 tries per minute per client
            self._failures[client] = recent
            return None
        if not same(code.strip(), self.code):
            self._failures[client] = recent + [now]
            return None
        self._failures.pop(client, None)
        if len(self._sessions) >= self.max_sessions:
            oldest = min(self._sessions, key=self._sessions.get)
            self._sessions.pop(oldest)
        token = secrets.token_urlsafe(24)
        self._sessions[token] = now
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        started = self._sessions.get(token)
        if started is None or time.time() - started > SESSION_TTL:
            self._sessions.pop(token, None)
            return False
        return True

    def logout(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)

    @property
    def session_count(self) -> int:
        return len(self._sessions)
