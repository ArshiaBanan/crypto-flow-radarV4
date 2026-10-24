from __future__ import annotations

from typing import Any


def production_gate_v30(*, app_env: str, admin_api_key: str | None, require_admin_key: bool) -> dict[str, Any]:
    is_prod = str(app_env).lower() in {"prod", "production"}
    configured = bool(admin_api_key)
    ready = not (is_prod and require_admin_key and not configured)
    reasons = []
    if is_prod and require_admin_key and not configured:
        reasons.append("ADMIN_API_KEY_REQUIRED_IN_PRODUCTION")
    return {"is_production": is_prod, "admin_key_configured": configured, "ready": ready, "reasons": reasons}
