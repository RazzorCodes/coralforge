"""
Configuration singleton for the coralforge app.

Loads from (in precedence order):
  1. Environment variables
  2. Brinecrypt secrets (DB creds, tokens)
  3. Defaults baked into the code

Config sources are resolved at startup via AppConfig.load().
Multi-repo config is passed as a dict; the simplest form is a single
repo from GITHUB_REPO / GITHUB_TOKEN env vars.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("coralforge.config")

_DEFAULT_BC_URL = "http://brinecrypt:8080"
_DEFAULT_REPO = "RazzorCodes/brinecrypt"


class AppConfig:
    """Singleton holding all configuration for the app.

    Call AppConfig.load() once at startup. Access attributes directly
    on the singleton instance.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_loaded") and self._loaded:
            return

        self.brinecrypt_url: str = os.getenv("BRINECRYPT_URL", _DEFAULT_BC_URL)
        self.pg_dsn: Optional[str] = None

        # List of repo config dicts, each with keys:
        #   friendly-name, owner, repo, token
        self.repos: List[Dict[str, Any]] = []

        # Env var fallbacks for single-repo mode
        self.github_repo: str = os.getenv("GITHUB_REPO", _DEFAULT_REPO)
        self.github_token: str = os.getenv("GITHUB_TOKEN", "")
        self._loaded = False

    def load(self, bc_connector: Optional[Any] = None,
             repo_configs: Optional[List[Dict[str, Any]]] = None) -> "AppConfig":
        """Resolve all configuration sources. Idempotent.

        Args:
            bc_connector: BrinecryptConnector for resolving secrets.
            repo_configs: Optional list of repo config dicts. If omitted,
                          uses env var fallback (GITHUB_REPO, GITHUB_TOKEN).
        """
        if self._loaded:
            return self

        # 1. Env overrides
        self.brinecrypt_url = os.getenv("BRINECRYPT_URL", self.brinecrypt_url)
        repo_str = os.getenv("GITHUB_REPO")
        token_str = os.getenv("GITHUB_TOKEN")
        if repo_str:
            self.github_repo = repo_str
        if token_str:
            self.github_token = token_str

        # 2. Repo configs
        if repo_configs:
            self.repos = list(repo_configs)
        else:
            self.repos = [self._env_fallback_repo()]

        # 3. Resolve secret references via brinecrypt
        self._resolve_secrets(bc_connector)

        self._loaded = True
        logger.info("AppConfig loaded — %d repo(s)", len(self.repos))
        return self

    def _env_fallback_repo(self) -> Dict[str, Any]:
        parts = self.github_repo.split("/")
        return {
            "friendly-name": self.github_repo.replace("/", "-"),
            "owner": parts[0] if len(parts) > 1 else "",
            "repo": parts[1] if len(parts) > 1 else self.github_repo,
            "token": self.github_token,
            "token_source": "env",
        }

    def _resolve_secrets(self, bc_connector: Optional[Any]) -> None:
        if bc_connector is None:
            return

        for repo in self.repos:
            for key in ("owner", "token", "repo"):
                entry = repo.get(key)
                if isinstance(entry, dict) and entry.get("type") == "brinecrypt-secret":
                    ns = entry.get("namespace", "coralforge")
                    secret_name = entry.get("name", "")
                    secret_key = entry.get("key", "value")
                    resolved = bc_connector.read_resource_raw(ns, secret_name)
                    if resolved:
                        try:
                            data = json.loads(resolved)
                            if isinstance(data, dict):
                                repo[key] = data.get(secret_key, resolved)
                            else:
                                repo[key] = resolved
                        except (json.JSONDecodeError, TypeError):
                            repo[key] = resolved

            # Resolve Postgres DSN from brinecrypt if not already set
            if self.pg_dsn is None:
                db_user = bc_connector.read_resource_raw("coralforge", "db-user")
                db_pass = bc_connector.read_resource_raw("coralforge", "db-pass")
                if db_user and db_pass:
                    host = os.getenv("PGHOST", "postgres")
                    port = os.getenv("PGPORT", "5432")
                    dbname = os.getenv("PGDATABASE", "coralforge")
                    self.pg_dsn = f"postgresql://{db_user}:{db_pass}@{host}:{port}/{dbname}"

    def get_repo_config(self, friendly_name: str) -> Optional[Dict[str, Any]]:
        for repo in self.repos:
            if repo.get("friendly-name") == friendly_name:
                return repo
        return None

    def get_repo_names(self) -> List[str]:
        return [r.get("friendly-name", "") for r in self.repos if r.get("friendly-name")]