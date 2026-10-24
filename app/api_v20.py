from __future__ import annotations

from fastapi import Header, HTTPException


def require_admin_key(configured_key: str | None, provided: str | None, *, fail_closed: bool = True) -> None:
    # Production safety: mutating admin endpoints must not be exposed when no key is configured.
    if not configured_key:
        if fail_closed:
            raise HTTPException(status_code=503, detail="admin API key is not configured")
        return
    if not provided or provided != configured_key:
        raise HTTPException(status_code=401, detail="invalid admin key")
