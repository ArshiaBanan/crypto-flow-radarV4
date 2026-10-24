from __future__ import annotations

from app.storage.sqlite import SQLiteStore


def build_durable_store(settings):
    backend = str(settings.storage_backend).strip().lower()
    if backend == "sqlite":
        return SQLiteStore(settings.sqlite_path)
    if backend == "postgres":
        from app.storage.postgres_v31 import PostgreSQLStoreV31
        return PostgreSQLStoreV31(settings.postgres_url or "")
    raise ValueError(f"Unsupported storage backend: {backend}")
