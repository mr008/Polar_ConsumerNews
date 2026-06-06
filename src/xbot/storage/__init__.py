"""Storage layer. All DB access goes through the Repository interface so the
local SQLite driver can be swapped for Turso/libSQL later without touching callers.
"""
from .repo import Repository
from .sqlite_repo import SqliteRepository

__all__ = ["Repository", "SqliteRepository"]
