"""Host guard for the copilot's local HTTP server.

Binding to 127.0.0.1 keeps remote hosts out, but not web pages that reach it by DNS
rebinding: requests must name a loopback Host (and Origin, when sent). The copilot's
per-launch token (servo/daemon/copilot_app.py) does the rest.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Set

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
