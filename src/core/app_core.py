"""Core orchestration layer for coralforge.

Owns the background poll loop, the per-repo state machine instances,
and exposes a clean API for transport layers (HTTP, gRPC, etc.) to call.
"""

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from src.data.brinecrypt_connector import BrinecryptConnector
from src.data.data_connector import InMemoryStateStore, PostgresStateStore, StateStore
from src.data.github_connector import GitHubConnector
from src.state.state import (
    ReleaseMachine,
)

logger = logging.getLogger("coralforge.core")

_POLL_INTERVAL = 30  # seconds between evaluation cycles


class AppCore:
    """Central orchestrator for the coralforge release lifecycle.

    Initializes per-repo state machines, runs a background poll loop,
    and provides synchronous methods for the API layer.
    """

    def __init__(self, config: Any):
        self.config = config
        self._machines: Dict[str, ReleaseMachine] = {}
        self._poll_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._store: Optional[StateStore] = None
        self._bc: Optional[BrinecryptConnector] = None
        self._initialized = False

    def initialize(self) -> None:
        """Set up all state machines and start the poll loop.

        Must be called once before any API method.
        """
        if self._initialized:
            return

        # ── Brinecrypt connector ──────────────────────────────────
        self._bc = BrinecryptConnector(self.config.brinecrypt_url)

        # ── State store ───────────────────────────────────────────
        if self.config.pg_dsn:
            try:
                self._store = PostgresStateStore(self.config.pg_dsn)
                logger.info("Using Postgres state store")
            except Exception as e:
                logger.warning("Postgres unavailable (%s), fallback to in-memory", e)
                self._store = InMemoryStateStore()
        else:
            self._store = InMemoryStateStore()
            logger.info("No Postgres DSN configured, using in-memory state store")

        # ── Per-repo state machines ──────────────────────────────
        for repo_config in self.config.repos:
            friendly = repo_config.get("friendly-name", "")
            if not friendly:
                continue

            gh = GitHubConnector(
                owner=repo_config.get("owner", ""),
                repo=repo_config.get("repo", ""),
                token=repo_config.get("token", ""),
            )

            machine = ReleaseMachine(
                repo_name=friendly,
                gh=gh,
                bc=self._bc,
                store=self._store,
            )
            self._machines[friendly] = machine
            logger.info("Initialized state machine for repo '%s' (state: %s)",
                        friendly, machine.current_state)

        # ── Start background poll ─────────────────────────────────
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="core-poll",
        )
        self._poll_thread.start()
        logger.info("Poll loop started (interval=%ds)", _POLL_INTERVAL)

        self._initialized = True

    def shutdown(self) -> None:
        """Stop the poll loop gracefully."""
        self._stop_event.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)
        logger.info("Core shut down")

    # ── Background poll loop ──────────────────────────────────────

    def _poll_loop(self) -> None:
        """Periodically evaluate all state machines."""
        while not self._stop_event.is_set():
            for name, machine in self._machines.items():
                try:
                    machine.evaluate()
                except Exception as e:
                    logger.error("[%s] Poll evaluation failed: %s", name, e)
            self._stop_event.wait(_POLL_INTERVAL)

    # ── API methods ───────────────────────────────────────────────

    def list_repos(self) -> List[str]:
        """Return all configured repo friendly-names."""
        return self.config.get_repo_names()

    def get_status(self, repo_name: Optional[str] = None
                   ) -> List[Dict[str, Any]]:
        """Return status for one or all repos.

        Returns a list of dicts, each with:
          repo, status, version, failed, and per-repo detail.
        """
        if repo_name:
            machines = {repo_name: self._machines.get(repo_name)}
        else:
            machines = self._machines

        results = []
        for name, machine in machines.items():
            if machine is None:
                continue
            state = {
                "repo": name,
                "status": machine.current_state,
                "version": machine.version,
                "failed": machine.failed,
            }
            results.append(state)

        return results

    def get_stable(self, repo_name: Optional[str] = None
                   ) -> List[Dict[str, str]]:
        """Return stable version for one or all repos."""
        if repo_name:
            repos_to_check = [repo_name]
        else:
            repos_to_check = self._store.list_repos() or []

        results = []
        for name in repos_to_check:
            stable = self._store.get_stable(name)
            if stable:
                results.append({"repo": name, "stable": stable})
            elif repo_name:
                # If specifically asked, include even if no stable
                results.append({"repo": name, "stable": None})

        return results

    def trigger_release(self, repo_name: str, bump: str = "patch"
                        ) -> Tuple[bool, str]:
        """Trigger a new release for a repo.

        Returns (success, message).
        """
        if bump not in ("patch", "minor", "major"):
            return False, f"Invalid bump type: {bump} (use patch|minor|major)"

        machine = self._machines.get(repo_name)
        if machine is None:
            return False, f"Unknown repo: {repo_name}"

        ok = machine.trigger_release(bump)
        if ok:
            return True, f"Release v{machine.version} ({bump}) started"
        return False, (f"Cannot trigger release in state "
                       f"'{machine.current_state}'")

    def promote_release(self, repo_name: str) -> Tuple[bool, str]:
        """Promote a release from e2e-test/integration-test to releasing."""
        machine = self._machines.get(repo_name)
        if machine is None:
            return False, f"Unknown repo: {repo_name}"

        ok = machine.promote_to_release()
        if ok:
            return True, f"Promoted to releasing (v{machine.version})"
        return (False, f"Cannot promote in state "
                f"'{machine.current_state}'")

    def merge_release(self, repo_name: str) -> Tuple[bool, str]:
        """Merge release branch into main."""
        machine = self._machines.get(repo_name)
        if machine is None:
            return False, f"Unknown repo: {repo_name}"

        ok = machine.merge_and_complete()
        if ok:
            return True, f"Merged v{machine.version} into main"
        return (False, f"Cannot merge in state "
                f"'{machine.current_state}'")

    def set_stable(self, repo_name: str, version: str) -> Tuple[bool, str]:
        """Mark a version as stable."""
        machine = self._machines.get(repo_name)
        if machine is None:
            return False, f"Unknown repo: {repo_name}"

        ok = machine.mark_stable(version)
        if ok:
            return True, f"v{version} marked as stable for {repo_name}"
        return False, f"Could not mark v{version} as stable (tag not found?)"

    def stop_release(self, repo_name: str, version: str) -> Tuple[bool, str]:
        """Stop a specific version build."""
        machine = self._machines.get(repo_name)
        if machine is None:
            return False, f"Unknown repo: {repo_name}"

        ok = machine.stop(version)
        if ok:
            return True, f"Release v{version} stopped"
        return (False, f"Cannot stop v{version} in state "
                f"'{machine.current_state}'")

    def health(self) -> Dict[str, Any]:
        """Health check — returns basic status of the core."""
        return {
            "status": "ok",
            "repos": len(self._machines),
            "bc_connected": self._bc.health() if self._bc else False,
            "store_type": type(self._store).__name__ if self._store else "none",
        }