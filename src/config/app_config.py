"""Configuration loading for the Coralforge multi-provider orchestrator."""

from __future__ import annotations

import base64
import json
import logging
import os
from glob import glob
from typing import Any, Dict, Iterable, List, Optional

import requests as _http
import yaml

from src.orchestration.models import (
    ProviderBinding,
    RepoDefinition,
    RunDefinition,
    SecretReference,
    StageDefinition,
)

logger = logging.getLogger("coralforge.config")

_DEFAULT_BC_URL = "http://brinecrypt:8080"
_DEFAULT_REPO = "RazzorCodes/brinecrypt"
_CONFIG_CANDIDATES = (
    ".coralforge.yml",
    ".coralforge.yaml",
    "coralforge.yml",
    "coralforge.yaml",
)


def _as_path_list(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, list):
                return [str(item) for item in loaded if str(item).strip()]
        except json.JSONDecodeError:
            pass
    return [part for part in raw.split(os.pathsep) if part]


def _parse_secret_ref(value: Any) -> Optional[SecretReference]:
    if isinstance(value, dict) and isinstance(value.get("secretRef"), dict):
        block = value["secretRef"]
        return SecretReference(
            namespace=block.get("namespace", "coralforge"),
            name=block.get("name", ""),
            key=block.get("key", "value"),
        )
    if isinstance(value, dict) and {"namespace", "name"} <= set(value.keys()):
        return SecretReference(
            namespace=value.get("namespace", "coralforge"),
            name=value.get("name", ""),
            key=value.get("key", "value"),
        )
    return None


def _parse_stage_definition(payload: Dict[str, Any]) -> StageDefinition:
    return StageDefinition(
        name=payload["name"],
        provider=payload["provider"],
        mode=payload.get("mode", "automatic"),
        target=dict(payload.get("target") or {}),
        runner=payload.get("runner"),
        depends_on=list(payload.get("depends_on") or []),
        trigger=dict(payload.get("trigger") or {}),
    )


def _parse_run_definition(name: str, payload: Dict[str, Any]) -> RunDefinition:
    stages = [_parse_stage_definition(item) for item in payload.get("stages") or []]
    return RunDefinition(
        name=name,
        default_provider=payload["default_provider"],
        provider_target=dict(payload.get("provider_target") or {}),
        trigger=dict(payload.get("trigger") or {}),
        stages=stages,
    )


def _parse_provider_binding(name: str, payload: Dict[str, Any]) -> ProviderBinding:
    config = dict(payload)
    secrets: Dict[str, SecretReference] = {}
    auth = payload.get("auth") or {}
    for key, value in auth.items():
        secret_ref = _parse_secret_ref(value)
        if secret_ref is not None:
            secrets[key] = secret_ref
            config[key] = None
        else:
            config[key] = value

    repo_block = payload.get("repo") or {}
    if isinstance(repo_block, dict):
        if "owner" in repo_block:
            config["owner"] = repo_block["owner"]
        if "repo" in repo_block:
            config["repo"] = repo_block["repo"]

    config.pop("auth", None)
    return ProviderBinding(name=name, kind=payload["kind"], config=config, secrets=secrets)


def _find_repo_config_files(explicit_paths: Iterable[str], roots: Iterable[str]) -> List[str]:
    found: List[str] = []
    for path in explicit_paths:
        if os.path.isfile(path):
            found.append(os.path.abspath(path))

    for root in roots:
        for candidate in _CONFIG_CANDIDATES:
            for path in glob(os.path.join(root, "**", candidate), recursive=True):
                if os.path.isfile(path):
                    found.append(os.path.abspath(path))

    unique: List[str] = []
    seen = set()
    for path in found:
        if path not in seen:
            unique.append(path)
            seen.add(path)
    return unique


class AppConfig:
    """Application configuration with repo registry and Brinecrypt secret refs."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_loaded") and self._loaded:
            return

        self.brinecrypt_url: str = os.getenv("BRINECRYPT_URL", _DEFAULT_BC_URL)
        self.pg_dsn: Optional[str] = os.getenv("CORALFORGE_PG_DSN")
        self.poll_interval_seconds: int = int(os.getenv("CORALFORGE_POLL_INTERVAL", "30"))
        self.repo_config_paths: List[str] = []
        self.repos: List[RepoDefinition] = []
        self._loaded = False

        self.github_repo: str = os.getenv("GITHUB_REPO", _DEFAULT_REPO)
        self.github_token: str = os.getenv("GITHUB_TOKEN", "")

    def load(
        self,
        bc_connector: Optional[Any] = None,
        repo_config_paths: Optional[List[str]] = None,
    ) -> "AppConfig":
        if self._loaded:
            return self

        repo_list_path = os.getenv("CORALFORGE_REPO_LIST")
        if repo_list_path and os.path.isfile(repo_list_path):
            # Primary path: fetch each repo's .coralforge.yml from GitHub API
            self.repos = self._load_repos_from_github_list(repo_list_path, bc_connector)
        else:
            # Legacy/dev path: load from local file paths or env fallback
            explicit_paths = list(repo_config_paths or [])
            explicit_paths.extend(_as_path_list(os.getenv("CORALFORGE_REPO_CONFIGS")))
            roots = _as_path_list(os.getenv("CORALFORGE_REPO_ROOTS"))

            self.repo_config_paths = _find_repo_config_files(explicit_paths, roots)
            if self.repo_config_paths:
                self.repos = [self._load_repo_config(path) for path in self.repo_config_paths]
            else:
                self.repos = [self._env_fallback_repo()]

        self._resolve_secrets(bc_connector)
        self._resolve_postgres_dsn(bc_connector)
        self._loaded = True
        logger.info("AppConfig loaded with %d repo(s)", len(self.repos))
        return self

    def _env_fallback_repo(self) -> RepoDefinition:
        owner, repo_name = "", self.github_repo
        if "/" in self.github_repo:
            owner, repo_name = self.github_repo.split("/", 1)

        binding = ProviderBinding(
            name="github",
            kind="github-actions",
            config={
                "owner": owner,
                "repo": repo_name,
                "token": self.github_token,
                "simulate": not bool(self.github_token),
            },
        )
        run_definition = RunDefinition(
            name="release",
            default_provider="github",
            provider_target={"workflow": "release.yml"},
            stages=[
                StageDefinition(name="build", provider="github", target={"workflow": "release-branch.yml"}),
                StageDefinition(name="release", provider="github", target={"workflow": "release.yml"}, mode="manual"),
            ],
        )
        return RepoDefinition(
            name=self.github_repo.replace("/", "-"),
            owner=owner,
            repo=repo_name,
            workspace_path=os.getenv("CORALFORGE_DEFAULT_WORKSPACE", os.getcwd()),
            config_path="<env>",
            config_version=1,
            providers={"github": binding},
            run_types={"release": run_definition},
            metadata={"legacy_env_fallback": True, "default_ref": "main"},
        )

    def _parse_repo_payload(
        self,
        payload: Dict[str, Any],
        source_label: str,
        workspace_fallback: str = "",
    ) -> RepoDefinition:
        repo_block = payload.get("repo") or {}
        workspace_path = repo_block.get("workspace_path") or workspace_fallback
        providers = {
            name: _parse_provider_binding(name, provider_payload)
            for name, provider_payload in (payload.get("providers") or {}).items()
        }
        run_types = {
            name: _parse_run_definition(name, run_payload)
            for name, run_payload in (payload.get("run_types") or {}).items()
        }
        secret_refs = {
            secret_name: ref
            for secret_name, ref in (
                (sn, _parse_secret_ref(sp))
                for sn, sp in (payload.get("secrets") or {}).items()
            )
            if ref is not None
        }
        name_fallback = os.path.basename(workspace_fallback) if workspace_fallback else "unknown"
        repo = RepoDefinition(
            name=repo_block.get("name") or repo_block.get("friendly_name") or repo_block.get("repo") or name_fallback,
            owner=repo_block.get("owner", ""),
            repo=repo_block.get("repo", ""),
            workspace_path=os.path.abspath(workspace_path) if workspace_path else "",
            config_path=source_label,
            config_version=int(payload.get("version", 1)),
            providers=providers,
            run_types=run_types,
            secret_references=secret_refs,
            metadata=dict(payload.get("metadata") or {}),
        )
        self._validate_repo_definition(repo)
        return repo

    def _load_repo_config(self, path: str) -> RepoDefinition:
        with open(path, encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
        repo_dir = os.path.dirname(path)
        return self._parse_repo_payload(payload, source_label=os.path.abspath(path), workspace_fallback=repo_dir)

    def _fetch_coralforge_yaml_from_github(
        self, owner: str, repo: str, token: str, endpoint: str
    ) -> Optional[str]:
        session = _http.Session()
        session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        if token:
            session.headers["Authorization"] = f"Bearer {token}"

        for candidate in _CONFIG_CANDIDATES:
            try:
                r = session.get(
                    f"{endpoint}/repos/{owner}/{repo}/contents/{candidate}",
                    timeout=15,
                )
            except Exception as exc:
                logger.warning("GitHub fetch error for %s/%s/%s: %s", owner, repo, candidate, exc)
                continue
            if r.ok:
                try:
                    raw_b64 = r.json().get("content", "").replace("\n", "")
                    return base64.b64decode(raw_b64).decode("utf-8", errors="replace")
                except Exception as exc:
                    logger.warning("Decode error for %s/%s/%s: %s", owner, repo, candidate, exc)
        return None

    def _load_repos_from_github_list(
        self,
        list_path: str,
        bc_connector: Optional[Any],
    ) -> List[RepoDefinition]:
        with open(list_path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}

        repos: List[RepoDefinition] = []
        for entry in raw.get("repos") or []:
            owner = entry.get("owner", "")
            repo_name = entry.get("repo", "")
            endpoint = entry.get("endpoint", "https://api.github.com").rstrip("/")

            token = entry.get("token", "")
            if not token and "tokenSecretRef" in entry and bc_connector:
                ref = _parse_secret_ref({"secretRef": entry["tokenSecretRef"]})
                if ref:
                    token = self._resolve_secret(bc_connector, ref) or ""

            yaml_content = self._fetch_coralforge_yaml_from_github(owner, repo_name, token, endpoint)
            if yaml_content is None:
                logger.error("Failed to fetch .coralforge.yml for %s/%s", owner, repo_name)
                stub = RepoDefinition(
                    name=f"{owner}-{repo_name}",
                    owner=owner,
                    repo=repo_name,
                    workspace_path="",
                    config_path=f"github:{owner}/{repo_name}",
                    config_version=0,
                    providers={},
                    run_types={},
                )
                stub.validation_errors.append(
                    f"could not fetch .coralforge.yml from github:{owner}/{repo_name}"
                )
                repos.append(stub)
                continue

            payload = yaml.safe_load(yaml_content) or {}
            repo_def = self._parse_repo_payload(
                payload,
                source_label=f"github:{owner}/{repo_name}/.coralforge.yml",
            )
            repos.append(repo_def)
            logger.info("Loaded repo config for %s/%s from GitHub", owner, repo_name)

        return repos

    def _validate_repo_definition(self, repo: RepoDefinition) -> None:
        if not repo.providers:
            repo.validation_errors.append("repo config has no providers")
        if not repo.run_types:
            repo.validation_errors.append("repo config has no run_types")
        if not os.path.isdir(repo.workspace_path):
            logger.debug("workspace_path does not exist (expected for API-driven repos): %s", repo.workspace_path)

        for run_name, run_definition in repo.run_types.items():
            if run_definition.default_provider not in repo.providers:
                repo.validation_errors.append(
                    f"run type '{run_name}' references unknown provider '{run_definition.default_provider}'"
                )
            if not run_definition.stages:
                repo.validation_errors.append(f"run type '{run_name}' has no stages")
            for stage in run_definition.stages:
                if stage.provider not in repo.providers:
                    repo.validation_errors.append(
                        f"stage '{run_name}:{stage.name}' references unknown provider '{stage.provider}'"
                    )

    def _resolve_secret(self, bc_connector: Any, ref: SecretReference) -> Optional[str]:
        raw = bc_connector.read_resource_raw(ref.namespace, ref.name)
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
        if isinstance(payload, dict):
            return payload.get(ref.key) or payload.get("value") or raw
        return raw

    def _resolve_secrets(self, bc_connector: Optional[Any]) -> None:
        if bc_connector is None:
            return

        for repo in self.repos:
            for binding in repo.providers.values():
                for key, ref in binding.secrets.items():
                    resolved = self._resolve_secret(bc_connector, ref)
                    if resolved is None:
                        repo.validation_errors.append(
                            f"missing secret '{ref.namespace}/{ref.name}' for provider '{binding.name}.{key}'"
                        )
                    else:
                        binding.config[key] = resolved

            for logical_name, ref in repo.secret_references.items():
                resolved = self._resolve_secret(bc_connector, ref)
                if resolved is None:
                    repo.validation_errors.append(
                        f"missing repo secret '{logical_name}' from '{ref.namespace}/{ref.name}'"
                    )
                else:
                    repo.metadata.setdefault("resolved_secrets", {})[logical_name] = resolved

    def _resolve_postgres_dsn(self, bc_connector: Optional[Any]) -> None:
        if self.pg_dsn is not None or bc_connector is None:
            return

        secret_ref = _parse_secret_ref(
            yaml.safe_load(os.getenv("CORALFORGE_PG_DSN_SECRET", "")) if os.getenv("CORALFORGE_PG_DSN_SECRET") else None
        )
        if secret_ref is not None:
            self.pg_dsn = self._resolve_secret(bc_connector, secret_ref)
            return

        db_user = bc_connector.read_resource_raw("coralforge", "db-user")
        db_pass = bc_connector.read_resource_raw("coralforge", "db-pass")
        if db_user and db_pass:
            host = os.getenv("PGHOST", "postgres")
            port = os.getenv("PGPORT", "5432")
            dbname = os.getenv("PGDATABASE", "coralforge")
            self.pg_dsn = f"postgresql://{db_user}:{db_pass}@{host}:{port}/{dbname}"

    def get_repo(self, repo_name: str) -> Optional[RepoDefinition]:
        for repo in self.repos:
            if repo.name == repo_name:
                return repo
        return None

    def get_repo_names(self) -> List[str]:
        return [repo.name for repo in self.repos]
