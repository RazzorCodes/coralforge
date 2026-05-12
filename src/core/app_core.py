"""Core orchestration wrapper for Coralforge."""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from src.data.brinecrypt_connector import BrinecryptConnector
from src.data.data_connector import InMemoryStateStore, PostgresStateStore, StateStore
from src.orchestration.service import OrchestrationService

logger = logging.getLogger("coralforge.core")


class AppCore:
    """Application core that wires config, persistence, and orchestration service."""

    def __init__(self, config: Any):
        self.config = config
        self._store: Optional[StateStore] = None
        self._bc: Optional[BrinecryptConnector] = None
        self._service: Optional[OrchestrationService] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return

        self._bc = BrinecryptConnector(self.config.brinecrypt_url)
        self.config._resolve_secrets(self._bc)
        self.config._resolve_postgres_dsn(self._bc)

        if self.config.pg_dsn:
            try:
                self._store = PostgresStateStore(self.config.pg_dsn)
                logger.info("Using Postgres state store")
            except Exception as exc:
                logger.warning("Postgres unavailable (%s), falling back to in-memory", exc)
                self._store = InMemoryStateStore()
        else:
            self._store = InMemoryStateStore()
            logger.info("No Postgres DSN configured; using in-memory state store")

        self._service = OrchestrationService(self.config, self._store)
        self._service.validate_repositories()
        self._start_poll_loop()
        self._initialized = True

    def _start_poll_loop(self) -> None:
        interval = max(int(getattr(self.config, "poll_interval_seconds", 30)), 5)

        def poll_loop() -> None:
            while not self._stop_event.is_set():
                try:
                    if self._service is not None:
                        self._service.refresh_active_runs()
                except Exception as exc:
                    logger.warning("Background refresh failed: %s", exc)
                self._stop_event.wait(interval)

        self._poll_thread = threading.Thread(target=poll_loop, daemon=True, name="coralforge-poll")
        self._poll_thread.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)

    @property
    def service(self) -> OrchestrationService:
        if self._service is None:
            raise RuntimeError("core not initialized")
        return self._service

    def list_repos(self) -> List[str]:
        return self.service.list_repo_names()

    def list_repo_definitions(self) -> List[Dict[str, Any]]:
        return self.service.list_repos()

    def get_status(self, repo_name: Optional[str] = None) -> List[Dict[str, Any]]:
        runs = self.service.list_runs(repo_name=repo_name, limit=50)
        latest_by_repo: Dict[str, Dict[str, Any]] = {}
        for run in runs:
            latest_by_repo.setdefault(run["repo"], run)

        results = []
        repo_names = [repo_name] if repo_name else self.service.list_repo_names()
        for name in repo_names:
            run = latest_by_repo.get(name)
            results.append(
                {
                    "repo": name,
                    "status": run.get("status", "unknown") if run else "unknown",
                    "version": run.get("version") if run else None,
                    "failed": (run.get("status") == "failed") if run else False,
                    "current_stage": run.get("current_stage") if run else None,
                    "provider": run.get("provider") if run else None,
                }
            )
        return results

    def get_stable(self, repo_name: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.service.get_stable(repo_name)

    def get_all_versions(self, repo_name: Optional[str] = None) -> List[Dict[str, Any]]:
        runs = self.service.list_runs(repo_name=repo_name, limit=200)
        return [
            {
                "repo": run["repo"],
                "version": run.get("version"),
                "status": run.get("status"),
                "failed": run.get("status") == "failed",
                "stable": self._store.get_stable(run["repo"]) == run.get("version") if self._store else False,
                "tag": run.get("version"),
                "branch": run.get("ref"),
                "is_current": index == 0,
                "provider": run.get("provider"),
                "run_type": run.get("run_type"),
            }
            for index, run in enumerate(runs)
        ]

    def list_runs(
        self,
        repo_name: Optional[str] = None,
        run_type: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        return self.service.list_runs(repo_name=repo_name, run_type=run_type, limit=limit)

    def trigger_run(
        self,
        repo_name: str,
        run_type: str,
        ref: Optional[str] = None,
        actor: str = "api",
        provider_name: Optional[str] = None,
        inputs: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self.service.trigger_run(
            repo_name=repo_name,
            run_type=run_type,
            ref=ref,
            actor=actor,
            provider_name=provider_name,
            inputs=inputs,
        ).to_dict()

    def get_run(self, run_id: str, refresh: bool = False) -> Optional[Dict[str, Any]]:
        return self.service.get_run(run_id, refresh=refresh)

    def get_logs(self, run_id: str, refresh: bool = False) -> Optional[Dict[str, Any]]:
        return self.service.get_logs(run_id, refresh=refresh)

    def get_provider_metadata(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self.service.get_provider_metadata(run_id)

    def trigger_release(self, repo_name: str, bump: str = "patch") -> Tuple[bool, str]:
        try:
            run = self.trigger_run(
                repo_name=repo_name,
                run_type="release",
                actor="legacy-api",
                inputs={"bump": bump},
            )
        except Exception as exc:
            return False, str(exc)
        return True, f"Release run queued ({run['run_id']})"

    def promote_release(self, repo_name: str) -> Tuple[bool, str]:
        run = self.service.latest_run_for_repo(repo_name, run_type="release")
        if run is None:
            return False, f"No release run found for '{repo_name}'"
        return False, f"Manual promotion is not yet implemented for normalized run {run.run_id}"

    def merge_release(self, repo_name: str) -> Tuple[bool, str]:
        run = self.service.latest_run_for_repo(repo_name, run_type="release")
        if run is None:
            return False, f"No release run found for '{repo_name}'"
        return False, f"Merge is provider-specific and not yet implemented for normalized run {run.run_id}"

    def set_stable(self, repo_name: str, version: str) -> Tuple[bool, str]:
        ok = self.service.mark_stable(repo_name, version)
        return (True, f"v{version} marked as stable") if ok else (False, "could not mark stable")

    def stop_release(self, repo_name: str, version: str) -> Tuple[bool, str]:
        return False, f"Stopping release '{repo_name}:{version}' is not yet implemented via the normalized API"

    def health(self) -> Dict[str, Any]:
        service_health = self.service.health()
        service_health["bc_connected"] = self._bc.health() if self._bc else False
        return service_health
