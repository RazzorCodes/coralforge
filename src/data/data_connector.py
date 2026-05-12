"""Persistence layer for normalized Coralforge orchestration state."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from src.orchestration.models import AuditEntry, CiRun, LogSnapshot

logger = logging.getLogger("coralforge.data")


class StateStore(ABC):
    """Interface for persisting Coralforge run state, logs, and audit history."""

    @abstractmethod
    def get_repo_state(self, repo_name: str) -> Optional[Dict[str, Any]]:
        ...

    @abstractmethod
    def set_repo_state(self, repo_name: str, state: Dict[str, Any]) -> bool:
        ...

    @abstractmethod
    def list_repos(self) -> List[str]:
        ...

    @abstractmethod
    def get_stable(self, repo_name: str) -> Optional[str]:
        ...

    @abstractmethod
    def set_stable(self, repo_name: str, version: str) -> bool:
        ...

    @abstractmethod
    def upsert_run(self, run: CiRun) -> bool:
        ...

    @abstractmethod
    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        ...

    @abstractmethod
    def list_runs(
        self,
        repo_name: Optional[str] = None,
        run_type: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def save_log_snapshot(self, snapshot: LogSnapshot) -> bool:
        ...

    @abstractmethod
    def get_log_snapshot(self, run_id: str) -> Optional[Dict[str, Any]]:
        ...

    @abstractmethod
    def append_audit_entry(self, entry: AuditEntry) -> bool:
        ...

    @abstractmethod
    def list_audit_entries(
        self,
        repo_name: Optional[str] = None,
        run_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        ...


class PostgresStateStore(StateStore):
    def __init__(self, dsn: str, auto_create: bool = True):
        import psycopg2

        self._dsn = dsn
        self._conn: Optional[psycopg2.extensions.connection] = None
        self._connect()
        if auto_create:
            self._ensure_schema()

    def _connect(self) -> None:
        import psycopg2

        self._conn = psycopg2.connect(self._dsn)
        self._conn.autocommit = True
        logger.info("Connected to Postgres state store")

    def _ensure_schema(self) -> None:
        if not self._conn:
            return
        with self._conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS coralforge_repos (
                    repo_name      TEXT PRIMARY KEY,
                    state          JSONB NOT NULL DEFAULT '{}',
                    stable_version TEXT,
                    updated_at     TIMESTAMPTZ DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS coralforge_runs (
                    run_id         TEXT PRIMARY KEY,
                    repo_name      TEXT NOT NULL,
                    run_type       TEXT NOT NULL,
                    status         TEXT NOT NULL,
                    payload        JSONB NOT NULL,
                    created_at     TIMESTAMPTZ DEFAULT now(),
                    updated_at     TIMESTAMPTZ DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS coralforge_runs_repo_created_idx
                    ON coralforge_runs (repo_name, created_at DESC);
                CREATE TABLE IF NOT EXISTS coralforge_log_snapshots (
                    run_id         TEXT PRIMARY KEY,
                    payload        JSONB NOT NULL,
                    updated_at     TIMESTAMPTZ DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS coralforge_audit_entries (
                    id             BIGSERIAL PRIMARY KEY,
                    repo_name      TEXT NOT NULL,
                    run_id         TEXT,
                    event_type     TEXT NOT NULL,
                    payload        JSONB NOT NULL,
                    created_at     TIMESTAMPTZ DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS coralforge_audit_repo_created_idx
                    ON coralforge_audit_entries (repo_name, created_at DESC);
                """
            )

    def _reconnect(self) -> None:
        try:
            if self._conn and not self._conn.closed:
                self._conn.close()
        except Exception:
            pass
        self._connect()

    def _execute(self, query: str, params: tuple = ()) -> Optional[Any]:
        if not self._conn:
            self._reconnect()
        try:
            with self._conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchall() if cur.description else []
        except Exception as exc:
            logger.error("Query failed: %s", exc)
            self._reconnect()
            return None

    def get_repo_state(self, repo_name: str) -> Optional[Dict[str, Any]]:
        rows = self._execute("SELECT state FROM coralforge_repos WHERE repo_name = %s", (repo_name,))
        return rows[0][0] if rows else None

    def set_repo_state(self, repo_name: str, state: Dict[str, Any]) -> bool:
        rows = self._execute(
            """
            INSERT INTO coralforge_repos (repo_name, state, updated_at)
            VALUES (%s, %s::jsonb, now())
            ON CONFLICT (repo_name)
            DO UPDATE SET state = EXCLUDED.state, updated_at = now()
            """,
            (repo_name, json.dumps(state)),
        )
        return rows is not None

    def list_repos(self) -> List[str]:
        rows = self._execute("SELECT repo_name FROM coralforge_repos ORDER BY repo_name")
        return [row[0] for row in rows] if rows else []

    def get_stable(self, repo_name: str) -> Optional[str]:
        rows = self._execute("SELECT stable_version FROM coralforge_repos WHERE repo_name = %s", (repo_name,))
        return rows[0][0] if rows else None

    def set_stable(self, repo_name: str, version: str) -> bool:
        rows = self._execute(
            """
            INSERT INTO coralforge_repos (repo_name, state, stable_version, updated_at)
            VALUES (%s, '{}'::jsonb, %s, now())
            ON CONFLICT (repo_name)
            DO UPDATE SET stable_version = EXCLUDED.stable_version, updated_at = now()
            """,
            (repo_name, version),
        )
        return rows is not None

    def upsert_run(self, run: CiRun) -> bool:
        payload = run.to_dict()
        rows = self._execute(
            """
            INSERT INTO coralforge_runs (run_id, repo_name, run_type, status, payload, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s::jsonb, now(), now())
            ON CONFLICT (run_id)
            DO UPDATE SET
                repo_name = EXCLUDED.repo_name,
                run_type = EXCLUDED.run_type,
                status = EXCLUDED.status,
                payload = EXCLUDED.payload,
                updated_at = now()
            """,
            (run.run_id, run.repo, run.run_type, run.status, json.dumps(payload)),
        )
        return rows is not None

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        rows = self._execute("SELECT payload FROM coralforge_runs WHERE run_id = %s", (run_id,))
        return rows[0][0] if rows else None

    def list_runs(
        self,
        repo_name: Optional[str] = None,
        run_type: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        query = "SELECT payload FROM coralforge_runs"
        clauses = []
        params: List[Any] = []
        if repo_name:
            clauses.append("repo_name = %s")
            params.append(repo_name)
        if run_type:
            clauses.append("run_type = %s")
            params.append(run_type)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        rows = self._execute(query, tuple(params))
        return [row[0] for row in rows] if rows else []

    def save_log_snapshot(self, snapshot: LogSnapshot) -> bool:
        rows = self._execute(
            """
            INSERT INTO coralforge_log_snapshots (run_id, payload, updated_at)
            VALUES (%s, %s::jsonb, now())
            ON CONFLICT (run_id)
            DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()
            """,
            (snapshot.run_id, json.dumps(snapshot.to_dict())),
        )
        return rows is not None

    def get_log_snapshot(self, run_id: str) -> Optional[Dict[str, Any]]:
        rows = self._execute("SELECT payload FROM coralforge_log_snapshots WHERE run_id = %s", (run_id,))
        return rows[0][0] if rows else None

    def append_audit_entry(self, entry: AuditEntry) -> bool:
        rows = self._execute(
            """
            INSERT INTO coralforge_audit_entries (repo_name, run_id, event_type, payload, created_at)
            VALUES (%s, %s, %s, %s::jsonb, now())
            """,
            (entry.repo, entry.run_id, entry.event_type, json.dumps(entry.to_dict())),
        )
        return rows is not None

    def list_audit_entries(
        self,
        repo_name: Optional[str] = None,
        run_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        query = "SELECT payload FROM coralforge_audit_entries"
        clauses = []
        params: List[Any] = []
        if repo_name:
            clauses.append("repo_name = %s")
            params.append(repo_name)
        if run_id:
            clauses.append("run_id = %s")
            params.append(run_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        rows = self._execute(query, tuple(params))
        return [row[0] for row in rows] if rows else []


class InMemoryStateStore(StateStore):
    def __init__(self):
        self._repo_states: Dict[str, Dict[str, Any]] = {}
        self._stables: Dict[str, str] = {}
        self._runs: Dict[str, Dict[str, Any]] = {}
        self._logs: Dict[str, Dict[str, Any]] = {}
        self._audit: List[Dict[str, Any]] = []

    def get_repo_state(self, repo_name: str) -> Optional[Dict[str, Any]]:
        return self._repo_states.get(repo_name)

    def set_repo_state(self, repo_name: str, state: Dict[str, Any]) -> bool:
        self._repo_states[repo_name] = dict(state)
        return True

    def list_repos(self) -> List[str]:
        repo_names = set(self._repo_states.keys())
        repo_names.update(run["repo"] for run in self._runs.values())
        return sorted(repo_names)

    def get_stable(self, repo_name: str) -> Optional[str]:
        return self._stables.get(repo_name)

    def set_stable(self, repo_name: str, version: str) -> bool:
        self._stables[repo_name] = version
        return True

    def upsert_run(self, run: CiRun) -> bool:
        self._runs[run.run_id] = run.to_dict()
        return True

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self._runs.get(run_id)

    def list_runs(
        self,
        repo_name: Optional[str] = None,
        run_type: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        runs = list(self._runs.values())
        if repo_name:
            runs = [run for run in runs if run.get("repo") == repo_name]
        if run_type:
            runs = [run for run in runs if run.get("run_type") == run_type]
        runs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return runs[:limit]

    def save_log_snapshot(self, snapshot: LogSnapshot) -> bool:
        self._logs[snapshot.run_id] = snapshot.to_dict()
        return True

    def get_log_snapshot(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self._logs.get(run_id)

    def append_audit_entry(self, entry: AuditEntry) -> bool:
        self._audit.append(entry.to_dict())
        self._audit.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return True

    def list_audit_entries(
        self,
        repo_name: Optional[str] = None,
        run_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        entries = list(self._audit)
        if repo_name:
            entries = [entry for entry in entries if entry.get("repo") == repo_name]
        if run_id:
            entries = [entry for entry in entries if entry.get("run_id") == run_id]
        return entries[:limit]
