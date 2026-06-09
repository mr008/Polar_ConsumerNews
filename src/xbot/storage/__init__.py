"""Storage layer. All DB access goes through the Repository interface so the
local SQLite driver can be swapped for Turso/libSQL later without touching callers.
"""
import os

from .repo import Repository
from .sqlite_repo import SqliteRepository


def get_repository(cfg) -> Repository:
    """Turso when TURSO_DATABASE_URL is set (cloud + shared state), else local SQLite."""
    if os.environ.get("TURSO_DATABASE_URL"):
        from .turso_repo import TursoRepository
        return TursoRepository()
    from ..config import db_path
    return SqliteRepository(db_path(cfg))


__all__ = ["Repository", "SqliteRepository", "get_repository"]
