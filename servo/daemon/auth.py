"""Bearer-token authentication for the local REST API.

Binding to 127.0.0.1 keeps remote hosts out, but not other local processes or web
pages (via DNS rebinding or a page served from the daemon's own origin). Since the
API can click and type on the user's desktop, every endpoint except /health needs a
token. The token comes from SERVO_API_TOKEN or is generated once and stored in
~/.servo/api_token.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any, Dict, Optional, Set

from fastapi import Header, HTTPException, status

from servo.config import settings

_token: Optional[str] = None


def token_path() -> Path:
    return Path.home() / ".servo" / "api_token"


def get_api_token() -> str:
    global _token
    if _token:
        return _token
    if settings.api_token:
        _token = settings.api_token
        return _token
    path = token_path()
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            _token = existing
            return _token
    path.parent.mkdir(parents=True, exist_ok=True)
    new_token = secrets.token_urlsafe(32)
    # O_EXCL so two daemons starting at once don't overwrite each other's token.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        _token = path.read_text(encoding="utf-8").strip()
        return _token
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new_token)
    _token = new_token
    return _token


# --------------------------------------------------------------- host guard

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _hostname(host_header: str) -> str:
    """'127.0.0.1:8000' -> '127.0.0.1'; '[::1]:8000' -> '::1'; lowercased."""
    h = host_header.strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


class LocalHostGuard:
    """ASGI middleware that rejects requests whose Host (or WebSocket Origin) is not ours.

    A token alone does not stop DNS rebinding: a malicious site can point its own
    domain at 127.0.0.1, become same-origin with this server, and read any page we
    serve (including the copilot page, which embeds its token). Such requests still
    carry the attacker's domain in the Host header, so refusing unknown Hosts closes
    the hole. WebSockets are also checked by Origin, since browsers send it on every
    handshake. ``allowed_hosts`` is a mutable set so a server bound to a non-loopback
    address can add that address at startup.
    """

    def __init__(self, app: Any, allowed_hosts: Optional[Set[str]] = None) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts if allowed_hosts is not None else set(LOOPBACK_HOSTS)

    def _host_ok(self, value: str) -> bool:
        return bool(value) and _hostname(value) in self.allowed_hosts

    def _origin_ok(self, origin: str) -> bool:
        if not origin or origin == "null":
            return origin == ""  # no Origin = non-browser client; "null" = sandboxed/file page
        _, _, rest = origin.partition("://")
        return self._host_ok(rest.split("/", 1)[0])

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        ok = self._host_ok(headers.get("host", ""))
        if ok and scope["type"] == "websocket":
            ok = self._origin_ok(headers.get("origin", ""))
        if ok:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = b"Host not allowed."
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def require_token(authorization: Optional[str] = Header(default=None)) -> None:
    expected = get_api_token()
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(supplied.strip(), expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing or invalid bearer token (see {token_path()} or SERVO_API_TOKEN).",
            headers={"WWW-Authenticate": "Bearer"},
        )
