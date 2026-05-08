"""Data persistence layer for coralforge.

Defines the StateStore interface for persisting per-repo release state,
and provides a Postgres implementation.

StateStore is the single source of truth for working state (current status,
version metadata, build artifacts); brinecrypt is used only for secrets
and final broadcast data.
"""

import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

logger = logging.getLogger("coralforge.data")


class StateStore(ABC):
    """Interface for persisting per-repo release lifecycle state."""

    @abstractmethod
    def get_repo_state(self, repo_name: str) -> Optional[Dict[str, Any]]:
        """Return the full state dict for a repo, or None if unknown."""
        ...

    @abstractmethod
    def set_repo_state(self, repo_name: str, state: Dict[str, Any]) -> bool:
        """Persist the full state dict for a repo. Returns True on success."""
        ...

    @abstractmethod
    def list_repos(self) -> List[str]:
        """Return names of all repos with stored state."""
        ...

    @abstractmethod
    def get_stable(self, repo_name: str) -> Optional[str]:
        """Return the stable version string for a repo, or None."""
        ...

    @abstractmethod
    def set_stable(self, repo_name: str, version: str) -> bool:
        """Record a version as stable for a repo."""
        ...


class PostgresStateStore(StateStore):
    """Postgres-backed state store.

    Schema (auto-created):
      coralforge_repos (
        repo_name      TEXT PRIMARY KEY,
        state          JSONB NOT NULL,
        stable_version TEXT,
        updated_at     TIMESTAMPTZ DEFAULT now()
      )
    """

    def __init__(self, dsn: str, auto_create: bool = True):
        import psycopg2
        import psycopg2.extras

        self._dsn = dsn
        self._conn: Optional[psycopg2.extensions.connection] = None
        self._connect()
        if auto_create:
            self._ensure_schema()

    def _connect(self) -> None:
        import psycopg2

        try:
            self._conn = psycopg2.connect(self._dsn)
            self._conn.autocommit = True
            logger.info("Connected to Postgres state store")
        except psycopg2.Error as e:
            logger.error(f"Failed to connect to Postgres: {e}")
            self._conn = None

    def _ensure_schema(self) -> None:
        if not self._conn:
            return
        try:
            with self._conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS coralforge_repos (
                        repo_name      TEXT PRIMARY KEY,
                        state          JSONB NOT NULL DEFAULT '{}',
                        stable_version TEXT,
                        updated_at     TIMESTAMPTZ DEFAULT now()
                    )
                """)
            logger.debug("Schema ensured")
        except Exception as e:
            logger.error(f"Schema creation failed: {e}")

    def _reconnect(self) -> None:
        import psycopg2

        try:
            if self._conn and not self._conn.closed:
                self._conn.close()
        except Exception:
            pass
        try:
            self._conn = psycopg2.connect(self._dsn)
            self._conn.autocommit = True
            logger.info("Reconnected to Postgres")
        except psycopg2.Error as e:
            logger.error(f"Reconnect failed: {e}")
            self._conn = None

    def _execute(self, query: str, params: tuple = ()) -> Optional[Any]:
        if not self._conn:
            self._reconnect()
            if not self._conn:
                return None
        try:
            with self._conn.cursor() as cur:
                cur.execute(query, params)
                if cur.description:
                    return cur.fetchall()
                return []
        except Exception as e:
            logger.error(f"Query failed: {e} — attempting reconnect")
            self._reconnect()
            return None

    # ── StateStore interface ───────────────────────────────────────

    def get_repo_state(self, repo_name: str) -> Optional[Dict[str, Any]]:
        rows = self._execute(
            "SELECT state FROM coralforge_repos WHERE repo_name = %s",
            (repo_name,),
        )
        if rows:
            return rows[0][0]
        return None

    def set_repo_state(self, repo_name: str, state: Dict[str, Any]) -> bool:
        result = self._execute(
            """INSERT INTO coralforge_repos (repo_name, state, updated_at)
               VALUES (%s, %s::jsonb, now())
               ON CONFLICT (repo_name)
               DO UPDATE SET state = %s::jsonb, updated_at = now()""",
            (repo_name, json.dumps(state), json.dumps(state)),
        )
        return result is not None

    def list_repos(self) -> List[str]:
        rows = self._execute(
            "SELECT repo_name FROM coralforge_repos ORDER BY repo_name"
        )
        return [r[0] for r in rows] if rows else []

    def get_stable(self, repo_name: str) -> Optional[str]:
        rows = self._execute(
            "SELECT stable_version FROM coralforge_repos WHERE repo_name = %s",
            (repo_name,),
        )
        if rows:
            return rows[0][0]
        return None

    def set_stable(self, repo_name: str, version: str) -> bool:
        result = self._execute(
            """INSERT INTO coralforge_repos (repo_name, state, stable_version, updated_at)
               VALUES (%s, '{}'::jsonb, %s, now())
               ON CONFLICT (repo_name)
               DO UPDATE SET stable_version = %s, updated_at = now()""",
            (repo_name, version, version),
        )
        return result is not None


class InMemoryStateStore(StateStore):
    """Fallback store — keeps state in memory.

    Useful for development and testing where no Postgres is available.
    State is lost on restart.
    """

    def __init__(self):
        self._repos: Dict[str, Dict[str, Any]] = {}
        self._stables: Dict[str, str] = {}

    def get_repo_state(self, repo_name: str) -> Optional[Dict[str, Any]]:
        return self._repos.get(repo_name)

    def set_repo_state(self, repo_name: str, state: Dict[str, Any]) -> bool:
        self._repos[repo_name] = state
        return True

    def list_repos(self) -> List[str]:
        return list(self._repos.keys())

    def get_stable(self, repo_name: str) -> Optional[str]:
        return self._stables.get(repo_name)

    def set_stable(self, repo_name: str, version: str) -> bool:
        self._stables[repo_name] = version
        return True