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
from typing import Optional

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


def require_token(authorization: Optional[str] = Header(default=None)) -> None:
    expected = get_api_token()
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(supplied.strip(), expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing or invalid bearer token (see {token_path()} or SERVO_API_TOKEN).",
            headers={"WWW-Authenticate": "Bearer"},
        )
