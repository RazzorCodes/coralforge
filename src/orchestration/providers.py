"""Provider interfaces and concrete GitHub/Drone implementations."""

from __future__ import annotations

from datetime import datetime, timezone
import io
import logging
import os
import re
import zipfile
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests
import yaml

from src.orchestration.models import (
    CiStage,
    CiStep,
    ProviderBinding,
    ProviderRunSnapshot,
    RepoDefinition,
    RunDefinition,
)

logger = logging.getLogger("coralforge.providers")


def _normalize_provider_timestamp(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z") or "T" in text:
            return text
        try:
            value = int(text)
        except ValueError:
            try:
                value = float(text)
            except ValueError:
                return text
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds <= 0:
            return None
        if seconds > 1_000_000_000_000:
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _workflow_on_block(data: Dict[str, Any]) -> Any:
    return data.get("on", data.get(True))


def _parse_on_block(on_block: Any) -> tuple:
    """Return (branches, tag_patterns, dispatchable) from a GitHub Actions 'on' block."""
    branches: List[str] = []
    tag_patterns: List[str] = []
    dispatchable = False
    if isinstance(on_block, str):
        dispatchable = on_block == "workflow_dispatch"
    elif isinstance(on_block, list):
        dispatchable = "workflow_dispatch" in on_block
    elif isinstance(on_block, dict):
        dispatchable = "workflow_dispatch" in on_block
        push = on_block.get("push")
        if isinstance(push, dict):
            branches = list(push.get("branches") or [])
            tag_patterns = list(push.get("tags") or [])
    return branches, tag_patterns, dispatchable


def _bump_version(version: str, bump: str = "patch") -> str:
    major, minor, patch_v = (int(p) for p in version.split("."))
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch_v + 1}"


def _parse_drone_documents(documents: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Build Drone pipeline definitions dict from a list of parsed YAML documents."""
    definitions: Dict[str, Dict[str, Any]] = {}
    for index, document in enumerate(documents):
        name = document.get("name") or f"pipeline-{index + 1}"
        steps = [
            step.get("name", f"step-{position + 1}")
            for position, step in enumerate(document.get("steps") or [])
            if isinstance(step, dict)
        ]
        trigger = document.get("trigger") or {}
        branches: List[str] = []
        if isinstance(trigger, dict):
            branch_block = trigger.get("branch")
            if isinstance(branch_block, dict):
                branches = list(branch_block.get("include") or [])
            elif isinstance(branch_block, list):
                branches = list(branch_block)

        definitions[name] = {
            "name": name,
            "kind": document.get("kind", "pipeline"),
            "type": document.get("type", "docker"),
            "steps": steps,
            "branches": branches,
            "trigger": trigger,
        }
    return definitions


def read_github_workflow_definitions(repo_path: str) -> Dict[str, Dict[str, Any]]:
    """Read GitHub Actions workflow definitions from a local checkout (tests only)."""
    workflows_dir = os.path.join(repo_path, ".github", "workflows")
    definitions: Dict[str, Dict[str, Any]] = {}
    if not os.path.isdir(workflows_dir):
        return definitions

    for filename in sorted(os.listdir(workflows_dir)):
        if not filename.endswith((".yml", ".yaml")):
            continue

        full_path = os.path.join(workflows_dir, filename)
        with open(full_path, encoding="utf-8") as handle:
            content = yaml.safe_load(handle) or {}

        on_block = _workflow_on_block(content)
        jobs = list((content.get("jobs") or {}).keys())
        branches, tag_patterns, dispatchable = _parse_on_block(on_block)

        definitions[filename] = {
            "name": content.get("name", filename),
            "file": filename,
            "path": full_path,
            "jobs": jobs,
            "dispatchable": dispatchable,
            "branches": branches,
            "tag_patterns": tag_patterns,
            "triggers": on_block,
        }

    return definitions


def read_drone_definitions(repo_path: str) -> Dict[str, Dict[str, Any]]:
    """Read Drone pipeline definitions from a local checkout (tests only)."""
    candidates = [
        ".drone.yml",
        ".drone.yaml",
        os.path.join(".drone", "pipeline.yml"),
        os.path.join(".drone", "pipeline.yaml"),
    ]
    documents: List[Dict[str, Any]] = []

    for rel_path in candidates:
        full_path = os.path.join(repo_path, rel_path)
        if not os.path.exists(full_path):
            continue
        with open(full_path, encoding="utf-8") as handle:
            loaded = list(yaml.safe_load_all(handle))
        for document in loaded:
            if isinstance(document, dict):
                documents.append(document)

    return _parse_drone_documents(documents)


class RepoSourceConnector(ABC):
    @abstractmethod
    def get_repo_metadata(self) -> Dict[str, Any]:
        ...

    @abstractmethod
    def list_branches(self) -> List[str]:
        ...

    @abstractmethod
    def list_tags(self) -> List[str]:
        ...


class CiProviderConnector(ABC):
    def __init__(self, binding: ProviderBinding):
        self.binding = binding

    @property
    def kind(self) -> str:
        return self.binding.kind

    @abstractmethod
    def discover_definitions(self, repo: RepoDefinition) -> Dict[str, Dict[str, Any]]:
        ...

    @abstractmethod
    def trigger_run(
        self,
        repo: RepoDefinition,
        run_definition: RunDefinition,
        ref: Optional[str],
        inputs: Optional[Dict[str, Any]] = None,
    ) -> ProviderRunSnapshot:
        ...

    @abstractmethod
    def get_run(self, repo: RepoDefinition, provider_run_id: str) -> ProviderRunSnapshot:
        ...

    @abstractmethod
    def get_logs(self, repo: RepoDefinition, provider_run_id: str) -> Dict[str, str]:
        ...

    def list_recent_runs(self, repo: RepoDefinition, limit: int = 20) -> List[ProviderRunSnapshot]:
        return []

    def find_build_by_ref(self, repo: RepoDefinition, ref: str) -> Optional[str]:
        """Return a provider_run_id for a build triggered by the given ref (e.g. a tag name), or None."""
        return None


class ReleaseProviderConnector(CiProviderConnector):
    """Optional provider interface for tagging/release flows."""


class GitHubRepoConnector(RepoSourceConnector):
    def __init__(self, owner: str, repo: str, token: str, endpoint: str = "https://api.github.com"):
        self.owner = owner
        self.repo = repo
        self.base_url = f"{endpoint.rstrip('/')}/repos/{owner}/{repo}"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[requests.Response]:
        response = self.session.get(f"{self.base_url}{path}", params=params, timeout=15)
        response.raise_for_status()
        return response

    def get_repo_metadata(self) -> Dict[str, Any]:
        response = self._get("")
        return response.json() if response is not None else {}

    def list_branches(self) -> List[str]:
        response = self._get("/branches", params={"per_page": 100})
        return [item.get("name", "") for item in response.json()] if response is not None else []

    def list_tags(self) -> List[str]:
        response = self._get("/tags", params={"per_page": 100})
        return [item.get("name", "") for item in response.json()] if response is not None else []


class GitHubActionsConnector(CiProviderConnector):
    def __init__(self, binding: ProviderBinding):
        super().__init__(binding)
        endpoint = binding.config.get("endpoint", "https://api.github.com").rstrip("/")
        owner = binding.config.get("owner", "")
        repo = binding.config.get("repo", "")
        self.base_url = f"{endpoint}/repos/{owner}/{repo}"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        token = binding.config.get("token", "")
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def discover_definitions(self, repo: RepoDefinition) -> Dict[str, Dict[str, Any]]:
        try:
            response = self.session.get(
                f"{self.base_url}/contents/.github/workflows",
                timeout=15,
            )
        except Exception as exc:
            logger.warning("GitHub workflow listing failed: %s", exc)
            return {}
        if not response.ok:
            logger.warning("GitHub workflow listing returned %s", response.status_code)
            return {}

        definitions: Dict[str, Dict[str, Any]] = {}
        for item in response.json():
            name = item.get("name", "")
            if not name.endswith((".yml", ".yaml")):
                continue
            download_url = item.get("download_url")
            if not download_url:
                continue
            try:
                fc = self.session.get(download_url, timeout=15)
            except Exception as exc:
                logger.warning("Failed to fetch workflow %s: %s", name, exc)
                continue
            if not fc.ok:
                continue
            content = yaml.safe_load(fc.text) or {}
            on_block = _workflow_on_block(content)
            jobs = list((content.get("jobs") or {}).keys())
            branches, tag_patterns, dispatchable = _parse_on_block(on_block)
            definitions[name] = {
                "name": content.get("name", name),
                "file": name,
                "jobs": jobs,
                "dispatchable": dispatchable,
                "branches": branches,
                "tag_patterns": tag_patterns,
                "triggers": on_block,
            }
        return definitions

    def _normalize_status(self, status: str, conclusion: Optional[str]) -> str:
        if status in {"queued", "requested", "waiting", "pending"}:
            return "queued"
        if status in {"in_progress", "running"}:
            return "running"
        if conclusion == "success":
            return "passed"
        if conclusion in {"failure", "timed_out", "action_required"}:
            return "failed"
        if conclusion == "cancelled":
            return "cancelled"
        return "queued" if status else "unknown"

    def _jobs_for_run(self, provider_run_id: str) -> List[Dict[str, Any]]:
        response = self.session.get(
            f"{self.base_url}/actions/runs/{provider_run_id}/jobs",
            params={"per_page": 100},
            timeout=15,
        )
        response.raise_for_status()
        return response.json().get("jobs", [])

    # ── GitHub tag-trigger helpers ────────────────────────────────────────────

    def _get_branch_sha(self, branch: str) -> str:
        r = self.session.get(f"{self.base_url}/commits/{branch}", timeout=15)
        r.raise_for_status()
        return r.json()["sha"]

    def _get_tag_sha(self, tag_name: str) -> str:
        r = self.session.get(f"{self.base_url}/git/refs/tags/{tag_name}", timeout=15)
        r.raise_for_status()
        ref_obj = r.json().get("object", {})
        sha = ref_obj.get("sha", "")
        if ref_obj.get("type") == "tag":
            r2 = self.session.get(f"{self.base_url}/git/tags/{sha}", timeout=15)
            r2.raise_for_status()
            sha = r2.json().get("object", {}).get("sha", sha)
        return sha

    def _latest_release_version(self) -> Optional[str]:
        r = self.session.get(f"{self.base_url}/releases", params={"per_page": 50}, timeout=15)
        r.raise_for_status()
        versions = []
        for rel in r.json():
            if rel.get("draft") or rel.get("prerelease"):
                continue
            tag = rel.get("tag_name", "")
            if not tag.startswith("v"):
                continue
            parts = tag[1:].split(".")
            if len(parts) == 3 and all(p.isdigit() for p in parts):
                versions.append(tuple(int(p) for p in parts))
        if not versions:
            return None
        m = max(versions)
        return f"{m[0]}.{m[1]}.{m[2]}"

    def _create_tag_ref(self, tag_name: str, sha: str) -> None:
        ref = f"refs/tags/{tag_name}"
        r = self.session.post(f"{self.base_url}/git/refs", json={"ref": ref, "sha": sha}, timeout=15)
        if r.status_code == 422:
            r = self.session.patch(
                f"{self.base_url}/git/refs/tags/{tag_name}",
                json={"sha": sha, "force": True},
                timeout=15,
            )
        r.raise_for_status()

    def _trigger_via_tag(
        self,
        workflow: str,
        workflow_def: Dict[str, Any],
        ref_name: str,
        inputs: Dict[str, Any],
    ) -> ProviderRunSnapshot:
        tag_patterns = workflow_def.get("tag_patterns", [])
        tag_prefix: Optional[str] = None
        for pattern in tag_patterns:
            if isinstance(pattern, str) and pattern.endswith("*"):
                tag_prefix = pattern[:-1]
                break
        if not tag_prefix:
            raise ValueError(f"Cannot determine tag prefix for workflow '{workflow}'")

        if tag_prefix.startswith("stable"):
            stable_version = inputs.get("version") or self._latest_release_version()
            if not stable_version:
                raise RuntimeError(f"No published releases found; cannot create stable tag")
            sha = self._get_tag_sha(f"v{stable_version}")
            tag_name = f"{tag_prefix}{stable_version}"
            self._create_tag_ref(tag_name, sha)
            logger.info("Created tag %s at %s for workflow %s", tag_name, sha[:12], workflow)
            return ProviderRunSnapshot(
                status="queued",
                provider_status="tagged",
                ref=f"v{stable_version}",
                sha=sha,
                metadata={"workflow": workflow, "tag": tag_name, "version": stable_version},
            )

        # build-* and release-* both bump from latest release
        latest = self._latest_release_version()
        if inputs.get("version"):
            version = inputs["version"]
        elif latest:
            version = _bump_version(latest, inputs.get("bump", "patch"))
        else:
            version = "0.1.0"
        sha = self._get_branch_sha(ref_name)
        tag_name = f"{tag_prefix}{version}"
        self._create_tag_ref(tag_name, sha)
        logger.info("Created tag %s at %s for workflow %s", tag_name, sha[:12], workflow)
        return ProviderRunSnapshot(
            status="queued",
            provider_status="tagged",
            ref=ref_name,
            sha=sha,
            metadata={"workflow": workflow, "tag": tag_name, "version": version},
        )

    def trigger_run(
        self,
        repo: RepoDefinition,
        run_definition: RunDefinition,
        ref: Optional[str],
        inputs: Optional[Dict[str, Any]] = None,
    ) -> ProviderRunSnapshot:
        workflow = run_definition.provider_target.get("workflow")
        ref_name = ref or repo.metadata.get("default_ref", "main")
        if not workflow:
            raise ValueError(f"run type '{run_definition.name}' missing provider_target.workflow")

        if self.binding.config.get("simulate"):
            return ProviderRunSnapshot(
                status="queued",
                provider_status="simulated",
                provider_run_id=f"sim-{run_definition.name}-{ref_name}",
                ref=ref_name,
                metadata={"workflow": workflow, "simulated": True, "inputs": inputs or {}},
            )

        definitions = self.discover_definitions(repo)
        workflow_def = definitions.get(workflow, {})

        if workflow_def.get("tag_patterns"):
            return self._trigger_via_tag(workflow, workflow_def, ref_name, inputs or {})
        raise ValueError(
            f"workflow '{workflow}' is not tag-triggerable; configure 'on.push.tags' in the workflow so Coralforge can trigger via Git tag"
        )

    def get_run(self, repo: RepoDefinition, provider_run_id: str) -> ProviderRunSnapshot:
        if provider_run_id.startswith("sim-"):
            return ProviderRunSnapshot(
                status="passed",
                provider_status="success",
                provider_run_id=provider_run_id,
                ref=repo.metadata.get("default_ref", "main"),
                metadata={"simulated": True},
            )

        response = self.session.get(
            f"{self.base_url}/actions/runs/{provider_run_id}",
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        jobs = self._jobs_for_run(provider_run_id)

        stages = []
        for job in jobs:
            steps = [
                CiStep(
                    name=step.get("name", ""),
                    status=self._normalize_status(step.get("status", ""), step.get("conclusion")),
                    started_at=step.get("started_at"),
                    finished_at=step.get("completed_at"),
                    provider_step=str(step.get("number", "")),
                )
                for step in job.get("steps") or []
            ]
            stages.append(
                CiStage(
                    name=job.get("name", ""),
                    provider=self.binding.name,
                    status=self._normalize_status(job.get("status", ""), job.get("conclusion")),
                    started_at=job.get("started_at"),
                    finished_at=job.get("completed_at"),
                    provider_target={"job_id": job.get("id")},
                    steps=steps,
                )
            )

        return ProviderRunSnapshot(
            status=self._normalize_status(payload.get("status", ""), payload.get("conclusion")),
            provider_status=payload.get("conclusion") or payload.get("status", "unknown"),
            provider_run_id=str(payload.get("id", provider_run_id)),
            url=payload.get("html_url"),
            sha=payload.get("head_sha"),
            ref=payload.get("head_branch"),
            stages=stages,
            metadata={
                "event": payload.get("event"),
                "workflow_id": payload.get("workflow_id"),
                "run_number": payload.get("run_number"),
            },
        )

    def get_logs(self, repo: RepoDefinition, provider_run_id: str) -> Dict[str, str]:
        if provider_run_id.startswith("sim-"):
            return {"simulated": "Simulated GitHub Actions run; no remote logs were fetched."}

        try:
            response = self.session.get(
                f"{self.base_url}/actions/runs/{provider_run_id}/logs",
                timeout=30,
            )
            response.raise_for_status()
            archive = zipfile.ZipFile(io.BytesIO(response.content))
            logs: Dict[str, str] = {}
            for name in archive.namelist():
                logs[name] = archive.read(name).decode("utf-8", errors="replace")
            if logs:
                return logs
        except Exception as exc:
            logger.warning("GitHub log archive fetch failed for run %s: %s", provider_run_id, exc)

        jobs = self._jobs_for_run(provider_run_id)
        return {
            job.get("name", f"job-{index + 1}"): "\n".join(
                f"{step.get('name', '')}: {step.get('conclusion') or step.get('status', 'unknown')}"
                for step in job.get("steps") or []
            )
            for index, job in enumerate(jobs)
        }

    def list_recent_runs(self, repo: RepoDefinition, limit: int = 20) -> List[ProviderRunSnapshot]:
        try:
            response = self.session.get(
                f"{self.base_url}/actions/runs",
                params={"per_page": min(limit, 100)},
                timeout=15,
            )
            response.raise_for_status()
        except Exception as exc:
            logger.warning("GHA list runs failed for %s: %s", repo.name, exc)
            return []

        snapshots: List[ProviderRunSnapshot] = []
        for run in response.json().get("workflow_runs", []):
            provider_run_id = str(run.get("id", "")).strip()
            if not provider_run_id:
                continue
            workflow_path = run.get("path", "")
            workflow_file = workflow_path.split("/")[-1] if workflow_path else str(run.get("workflow_id", ""))
            status = run.get("status", "")
            conclusion = run.get("conclusion")
            finished_at = run.get("updated_at") if status == "completed" else None
            snapshots.append(
                ProviderRunSnapshot(
                    status=self._normalize_status(status, conclusion),
                    provider_status=conclusion or status or "unknown",
                    provider_run_id=provider_run_id,
                    url=run.get("html_url"),
                    sha=run.get("head_sha"),
                    ref=run.get("head_branch"),
                    created_at=run.get("created_at"),
                    started_at=run.get("run_started_at"),
                    finished_at=finished_at,
                    metadata={
                        "event": run.get("event"),
                        "workflow": workflow_file,
                        "workflow_id": run.get("workflow_id"),
                        "run_number": run.get("run_number"),
                    },
                )
            )
        return snapshots


class DroneConnector(CiProviderConnector):
    def __init__(self, binding: ProviderBinding):
        super().__init__(binding)
        endpoint = binding.config.get("endpoint", "").rstrip("/")
        if not endpoint and not binding.config.get("simulate"):
            raise ValueError("drone provider requires endpoint or simulate=true")
        self.endpoint = endpoint
        self.session = requests.Session()
        token = binding.config.get("token", "")
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self._definitions_cache: Optional[Dict[str, Dict[str, Any]]] = None

    def discover_definitions(self, repo: RepoDefinition) -> Dict[str, Dict[str, Any]]:
        if self._definitions_cache is not None:
            return self._definitions_cache
        result = self._fetch_definitions(repo)
        self._definitions_cache = result
        return result

    def _fetch_definitions(self, repo: RepoDefinition) -> Dict[str, Dict[str, Any]]:
        github_token = self.binding.config.get("github_token")
        owner = self.binding.config.get("owner", repo.owner)
        repo_name = self.binding.config.get("repo", repo.repo)

        if github_token:
            import base64
            gh_endpoint = self.binding.config.get("github_endpoint", "https://api.github.com").rstrip("/")
            sess = requests.Session()
            sess.headers.update({
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            })
            try:
                r = sess.get(
                    f"{gh_endpoint}/repos/{owner}/{repo_name}/contents/.drone.yml",
                    timeout=15,
                )
            except Exception as exc:
                logger.warning("Failed to fetch .drone.yml from GitHub: %s", exc)
                return {}
            if not r.ok:
                logger.warning(".drone.yml not found via GitHub API (%s)", r.status_code)
                return {}
            try:
                raw = base64.b64decode(r.json()["content"]).decode("utf-8", errors="replace")
                documents = [doc for doc in yaml.safe_load_all(raw) if isinstance(doc, dict)]
                return _parse_drone_documents(documents)
            except Exception as exc:
                logger.warning("Failed to parse .drone.yml: %s", exc)
                return {}

        if self.binding.config.get("simulate"):
            return {}

        try:
            self.session.get(
                f"{self.endpoint}/api/repos/{owner}/{repo_name}",
                timeout=10,
            ).raise_for_status()
        except Exception as exc:
            raise RuntimeError(f"Drone repo unreachable for {owner}/{repo_name}: {exc}") from exc
        return {}

    # ── GitHub tag-trigger helpers ────────────────────────────────────────────

    def _gh_base(self) -> str:
        return self.binding.config.get("github_endpoint", "https://api.github.com").rstrip("/")

    def _github_session(self) -> Optional[requests.Session]:
        token = self.binding.config.get("github_token")
        if not token:
            return None
        sess = requests.Session()
        sess.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        return sess

    def _get_branch_sha(self, sess: requests.Session, owner: str, repo_name: str, branch: str) -> str:
        r = sess.get(f"{self._gh_base()}/repos/{owner}/{repo_name}/commits/{branch}", timeout=15)
        r.raise_for_status()
        return r.json()["sha"]

    def _get_tag_sha(self, sess: requests.Session, owner: str, repo_name: str, tag_name: str) -> str:
        r = sess.get(f"{self._gh_base()}/repos/{owner}/{repo_name}/git/refs/tags/{tag_name}", timeout=15)
        r.raise_for_status()
        ref_obj = r.json().get("object", {})
        sha = ref_obj.get("sha", "")
        if ref_obj.get("type") == "tag":
            r2 = sess.get(f"{self._gh_base()}/repos/{owner}/{repo_name}/git/tags/{sha}", timeout=15)
            r2.raise_for_status()
            sha = r2.json().get("object", {}).get("sha", sha)
        return sha

    def _latest_release_version(self, sess: requests.Session, owner: str, repo_name: str) -> Optional[str]:
        r = sess.get(
            f"{self._gh_base()}/repos/{owner}/{repo_name}/releases",
            params={"per_page": 50},
            timeout=15,
        )
        r.raise_for_status()
        versions = []
        for rel in r.json():
            if rel.get("draft") or rel.get("prerelease"):
                continue
            tag = rel.get("tag_name", "")
            if not tag.startswith("v"):
                continue
            parts = tag[1:].split(".")
            if len(parts) == 3 and all(p.isdigit() for p in parts):
                versions.append(tuple(int(p) for p in parts))
        if not versions:
            return None
        m = max(versions)
        return f"{m[0]}.{m[1]}.{m[2]}"

    @staticmethod
    def _bump_version(version: str, bump: str = "patch") -> str:
        return _bump_version(version, bump)

    def _create_tag_ref(self, sess: requests.Session, owner: str, repo_name: str, tag_name: str, sha: str) -> None:
        ref = f"refs/tags/{tag_name}"
        r = sess.post(
            f"{self._gh_base()}/repos/{owner}/{repo_name}/git/refs",
            json={"ref": ref, "sha": sha},
            timeout=15,
        )
        if r.status_code == 422:
            r = sess.patch(
                f"{self._gh_base()}/repos/{owner}/{repo_name}/git/refs/tags/{tag_name}",
                json={"sha": sha, "force": True},
                timeout=15,
            )
        r.raise_for_status()

    def _trigger_via_tag(
        self,
        sess: requests.Session,
        owner: str,
        repo_name: str,
        pipeline: str,
        pipeline_def: Dict[str, Any],
        ref_name: str,
        inputs: Dict[str, Any],
        tag_prefix_override: Optional[str] = None,
    ) -> ProviderRunSnapshot:
        trigger = pipeline_def.get("trigger") or {}
        ref_block = trigger.get("ref") or {}
        ref_includes = ref_block.get("include", []) if isinstance(ref_block, dict) else []

        tag_prefix: Optional[str] = tag_prefix_override
        if not tag_prefix:
            for pattern in ref_includes:
                if isinstance(pattern, str) and pattern.startswith("refs/tags/") and pattern.endswith("*"):
                    tag_prefix = pattern[len("refs/tags/"):-1]
                    break

        if not tag_prefix:
            raise ValueError(f"Cannot determine tag prefix for pipeline '{pipeline}'")

        if tag_prefix.startswith("release"):
            latest = self._latest_release_version(sess, owner, repo_name)
            if inputs.get("version"):
                new_version = inputs["version"]
            elif latest:
                new_version = self._bump_version(latest, inputs.get("bump", "patch"))
            else:
                new_version = "0.1.0"
            sha = self._get_branch_sha(sess, owner, repo_name, ref_name)
            tag_name = f"{tag_prefix}{new_version}"
            self._create_tag_ref(sess, owner, repo_name, tag_name, sha)
            logger.info("Created tag %s at %s for %s/%s", tag_name, sha[:12], owner, repo_name)
            return ProviderRunSnapshot(
                status="queued",
                provider_status="tagged",
                ref=ref_name,
                sha=sha,
                metadata={"pipeline": pipeline, "tag": tag_name, "version": new_version},
            )

        if tag_prefix.startswith("stable"):
            stable_version = inputs.get("version") or self._latest_release_version(sess, owner, repo_name)
            if not stable_version:
                raise RuntimeError(f"No published releases found in {owner}/{repo_name}")
            sha = self._get_tag_sha(sess, owner, repo_name, f"v{stable_version}")
            tag_name = f"{tag_prefix}{stable_version}"
            self._create_tag_ref(sess, owner, repo_name, tag_name, sha)
            logger.info("Created tag %s at %s for %s/%s", tag_name, sha[:12], owner, repo_name)
            return ProviderRunSnapshot(
                status="queued",
                provider_status="tagged",
                ref=f"v{stable_version}",
                sha=sha,
                metadata={"pipeline": pipeline, "tag": tag_name, "version": stable_version},
            )

        raise ValueError(f"Unrecognized tag prefix '{tag_prefix}' for pipeline '{pipeline}'")

    def _normalize_status(self, status: str) -> str:
        if status in {"pending", "created"}:
            return "queued"
        if status == "blocked":
            return "blocked"
        if status in {"running", "started"}:
            return "running"
        if status == "success":
            return "passed"
        if status in {"failure", "error", "killed"}:
            return "failed"
        if status in {"cancelled", "canceled"}:
            return "cancelled"
        return "unknown"

    def _coerce_build_payload(self, payload: Any) -> Optional[Dict[str, Any]]:
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    return item
        return None

    def _find_recent_triggered_build(
        self,
        owner: str,
        repo_name: str,
        ref_name: str,
    ) -> Optional[Dict[str, Any]]:
        response = self.session.get(
            f"{self.endpoint}/api/repos/{owner}/{repo_name}/builds",
            params={"page": 1, "per_page": 20},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return None

        matched: List[Dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            target = item.get("target")
            if target not in {ref_name, f"refs/heads/{ref_name}"}:
                continue
            matched.append(item)

        # Prefer a freshly enqueued/running build when available.
        for item in matched:
            if self._normalize_status(item.get("status", "")) in {"queued", "running", "blocked"}:
                return item
        return matched[0] if matched else None

    def find_build_by_ref(self, repo: RepoDefinition, ref: str) -> Optional[str]:
        if self.binding.config.get("simulate") or not self.endpoint:
            return None
        owner = self.binding.config.get("owner", repo.owner)
        repo_name = self.binding.config.get("repo", repo.repo)
        try:
            response = self.session.get(
                f"{self.endpoint}/api/repos/{owner}/{repo_name}/builds",
                params={"page": 1, "per_page": 25},
                timeout=15,
            )
            response.raise_for_status()
        except Exception as exc:
            logger.warning("find_build_by_ref failed for tag %s: %s", ref, exc)
            return None
        payload = response.json()
        if not isinstance(payload, list):
            return None
        for item in payload:
            if not isinstance(item, dict):
                continue
            target = item.get("target", "")
            if target in {ref, f"refs/tags/{ref}"}:
                number = item.get("number")
                if number is not None:
                    return str(number)
        return None

    def trigger_run(
        self,
        repo: RepoDefinition,
        run_definition: RunDefinition,
        ref: Optional[str],
        inputs: Optional[Dict[str, Any]] = None,
    ) -> ProviderRunSnapshot:
        pipeline = run_definition.provider_target.get("pipeline")
        ref_name = ref or repo.metadata.get("default_ref", "main")
        if not pipeline:
            raise ValueError(f"run type '{run_definition.name}' missing provider_target.pipeline")

        if self.binding.config.get("simulate"):
            return ProviderRunSnapshot(
                status="queued",
                provider_status="simulated",
                provider_run_id=f"sim-{run_definition.name}-{pipeline}-{ref_name}",
                ref=ref_name,
                metadata={"pipeline": pipeline, "simulated": True, "inputs": inputs or {}},
            )

        owner = self.binding.config.get("owner", repo.owner)
        repo_name = self.binding.config.get("repo", repo.repo)

        # Create GitHub tag and trigger Drone directly for tag-ref pipelines
        definitions = self.discover_definitions(repo)
        pipeline_def = definitions.get(pipeline, {})
        trigger = pipeline_def.get("trigger") or {}
        event_block = trigger.get("event") or {}
        trigger_events = (
            event_block.get("include", []) if isinstance(event_block, dict)
            else event_block if isinstance(event_block, list)
            else []
        )
        ref_block = trigger.get("ref") or {}
        ref_includes = ref_block.get("include", []) if isinstance(ref_block, dict) else []
        has_tag_ref = (
            "tag" in trigger_events
            or any(isinstance(p, str) and "refs/tags/" in p for p in ref_includes)
            or bool(run_definition.provider_target.get("tag_prefix"))
        )
        if has_tag_ref:
            sess = self._github_session()
            if not sess:
                raise ValueError(f"github_token required to create tag for pipeline '{pipeline}'")
            tag_prefix_override = run_definition.provider_target.get("tag_prefix") or None
            tag_snapshot = self._trigger_via_tag(sess, owner, repo_name, pipeline, pipeline_def, ref_name, inputs or {}, tag_prefix_override)
            tag_name = tag_snapshot.metadata.get("tag")
            if tag_name:
                drone_ref = f"refs/tags/{tag_name}"
                response = self.session.post(
                    f"{self.endpoint}/api/repos/{owner}/{repo_name}/builds",
                    params={"branch": drone_ref},
                    timeout=15,
                )
                response.raise_for_status()
                payload: Optional[Dict[str, Any]]
                try:
                    payload = self._coerce_build_payload(response.json())
                except ValueError:
                    payload = None
                if payload is None:
                    payload = self._find_recent_triggered_build(owner, repo_name, drone_ref)
                if payload is not None:
                    tag_snapshot.provider_run_id = str(payload.get("number", ""))
                    tag_snapshot.status = self._normalize_status(payload.get("status", ""))
                    tag_snapshot.provider_status = payload.get("status", tag_snapshot.provider_status)
                    tag_snapshot.url = payload.get("link") or tag_snapshot.url
                    tag_snapshot.created_at = _normalize_provider_timestamp(payload.get("created"))
                    tag_snapshot.started_at = _normalize_provider_timestamp(payload.get("started"))
                else:
                    logger.warning("No Drone build found after tag creation for pipeline '%s'", pipeline)
            return tag_snapshot

        response = self.session.post(
            f"{self.endpoint}/api/repos/{owner}/{repo_name}/builds",
            params={"branch": ref_name},
            timeout=15,
        )
        response.raise_for_status()

        payload: Optional[Dict[str, Any]]
        try:
            payload = self._coerce_build_payload(response.json())
        except ValueError:
            payload = None
        if payload is None:
            logger.warning(
                "Drone trigger returned empty payload for %s/%s on ref '%s'; attempting recent-build fallback",
                owner,
                repo_name,
                ref_name,
            )
            payload = self._find_recent_triggered_build(owner, repo_name, ref_name)
        if payload is None:
            body = (response.text or "").strip()
            body_excerpt = body[:240] if body else "<empty>"
            raise RuntimeError(
                f"Drone trigger returned no build payload for {owner}/{repo_name} ref '{ref_name}' "
                f"(status={response.status_code}, body={body_excerpt})"
            )

        return ProviderRunSnapshot(
            status=self._normalize_status(payload.get("status", "")),
            provider_status=payload.get("status", "unknown"),
            provider_run_id=str(payload.get("number", "")),
            url=payload.get("link"),
            created_at=_normalize_provider_timestamp(payload.get("created")),
            started_at=_normalize_provider_timestamp(payload.get("started")),
            finished_at=_normalize_provider_timestamp(payload.get("stopped")),
            sha=payload.get("after"),
            ref=payload.get("target") or ref_name,
            metadata={"pipeline": pipeline, "event": payload.get("event")},
        )

    def get_run(self, repo: RepoDefinition, provider_run_id: str) -> ProviderRunSnapshot:
        if provider_run_id.startswith("sim-"):
            return ProviderRunSnapshot(
                status="passed",
                provider_status="success",
                provider_run_id=provider_run_id,
                ref=repo.metadata.get("default_ref", "main"),
                metadata={"simulated": True},
            )

        owner = self.binding.config.get("owner", repo.owner)
        repo_name = self.binding.config.get("repo", repo.repo)
        response = self.session.get(
            f"{self.endpoint}/api/repos/{owner}/{repo_name}/builds/{provider_run_id}",
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()

        stages = []
        for stage in payload.get("stages") or []:
            steps = [
                CiStep(
                    name=step.get("name", ""),
                    status=self._normalize_status(step.get("status", "")),
                    started_at=_normalize_provider_timestamp(step.get("started")),
                    finished_at=_normalize_provider_timestamp(step.get("stopped")),
                    provider_step=str(step.get("number", "")),
                )
                for step in stage.get("steps") or []
            ]
            stages.append(
                CiStage(
                    name=stage.get("name", ""),
                    provider=self.binding.name,
                    status=self._normalize_status(stage.get("status", "")),
                    started_at=_normalize_provider_timestamp(stage.get("started")),
                    finished_at=_normalize_provider_timestamp(stage.get("stopped")),
                    provider_target={"number": stage.get("number")},
                    steps=steps,
                )
            )

        return ProviderRunSnapshot(
            status=self._normalize_status(payload.get("status", "")),
            provider_status=payload.get("status", "unknown"),
            provider_run_id=str(payload.get("number", provider_run_id)),
            url=payload.get("link"),
            created_at=_normalize_provider_timestamp(payload.get("created")),
            started_at=_normalize_provider_timestamp(payload.get("started")),
            finished_at=_normalize_provider_timestamp(payload.get("stopped")),
            sha=payload.get("after"),
            ref=payload.get("target"),
            stages=stages,
            metadata={"event": payload.get("event"), "sender": payload.get("sender")},
        )

    def list_recent_runs(self, repo: RepoDefinition, limit: int = 20) -> List[ProviderRunSnapshot]:
        if self.binding.config.get("simulate"):
            return []

        owner = self.binding.config.get("owner", repo.owner)
        repo_name = self.binding.config.get("repo", repo.repo)
        response = self.session.get(
            f"{self.endpoint}/api/repos/{owner}/{repo_name}/builds",
            params={"page": 1, "per_page": limit},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            logger.warning("Drone list builds returned unexpected payload type: %s", type(payload).__name__)
            return []

        snapshots: List[ProviderRunSnapshot] = []
        for item in payload[:limit]:
            provider_run_id = str(item.get("number", "")).strip()
            if not provider_run_id:
                continue
            try:
                snapshot = self.get_run(repo, provider_run_id)
            except Exception as exc:
                logger.warning("Failed to fetch Drone run detail for build %s: %s", provider_run_id, exc)
                continue
            snapshot.status = self._normalize_status(item.get("status", snapshot.provider_status))
            snapshot.provider_status = item.get("status", snapshot.provider_status)
            snapshot.url = item.get("link") or snapshot.url
            snapshot.created_at = _normalize_provider_timestamp(item.get("created")) or snapshot.created_at
            snapshot.started_at = _normalize_provider_timestamp(item.get("started")) or snapshot.started_at
            snapshot.finished_at = _normalize_provider_timestamp(item.get("stopped")) or snapshot.finished_at
            snapshot.sha = item.get("after") or snapshot.sha
            snapshot.ref = item.get("target") or snapshot.ref
            snapshot.metadata.update(
                {
                    "event": item.get("event", snapshot.metadata.get("event")),
                    "sender": item.get("sender", snapshot.metadata.get("sender")),
                }
            )
            snapshots.append(snapshot)
        return snapshots

    def get_logs(self, repo: RepoDefinition, provider_run_id: str) -> Dict[str, str]:
        if provider_run_id.startswith("sim-"):
            return {"simulated": "Simulated Drone run; no remote logs were fetched."}

        owner = self.binding.config.get("owner", repo.owner)
        repo_name = self.binding.config.get("repo", repo.repo)
        run_snapshot = self.get_run(repo, provider_run_id)
        logs: Dict[str, str] = {}
        for stage in run_snapshot.stages:
            stage_number = stage.provider_target.get("number")
            if stage_number is None:
                continue
            response = self.session.get(
                f"{self.endpoint}/api/repos/{owner}/{repo_name}/builds/{provider_run_id}/logs/{stage_number}",
                timeout=30,
            )
            if response.ok:
                logs[stage.name] = response.text
        return logs


class JenkinsConnector(CiProviderConnector):
    def __init__(self, binding: ProviderBinding):
        super().__init__(binding)
        self.endpoint = str(binding.config.get("endpoint", "")).rstrip("/")
        self.default_job = str(binding.config.get("job", "")).strip()
        self._crumb_header: Dict[str, str] = {}
        self.session = requests.Session()
        user = str(binding.config.get("user", "")).strip()
        token = str(binding.config.get("token", "")).strip()
        if user and token:
            self.session.auth = (user, token)
        if not self.binding.config.get("simulate") and not self.endpoint:
            raise ValueError("jenkins provider requires endpoint or simulate=true")

    @staticmethod
    def _normalize_status(result: Optional[str], building: bool = False, blocked: bool = False) -> str:
        if blocked:
            return "blocked"
        if building:
            return "running"
        if result in {None, "", "NOT_BUILT"}:
            return "queued"
        result = str(result).upper()
        if result == "SUCCESS":
            return "passed"
        if result in {"FAILURE", "UNSTABLE"}:
            return "failed"
        if result in {"ABORTED", "CANCELLED"}:
            return "cancelled"
        return "unknown"

    @staticmethod
    def _job_path(job: str) -> str:
        parts = [part for part in str(job).split("/") if part]
        return "".join(f"/job/{quote(part, safe='')}" for part in parts)

    def _job_api_url(self, job: str, suffix: str = "") -> str:
        return f"{self.endpoint}{self._job_path(job)}{suffix}"

    def _ensure_crumb(self) -> None:
        if self._crumb_header:
            return
        try:
            response = self.session.get(f"{self.endpoint}/crumbIssuer/api/json", timeout=15)
        except Exception:
            return
        if not response.ok:
            return
        payload = response.json() or {}
        crumb_field = payload.get("crumbRequestField")
        crumb_value = payload.get("crumb")
        if crumb_field and crumb_value:
            self._crumb_header = {str(crumb_field): str(crumb_value)}

    @staticmethod
    def _extract_parameters(actions: Any) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if not isinstance(actions, list):
            return params
        for action in actions:
            if not isinstance(action, dict):
                continue
            for item in action.get("parameters") or []:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if name:
                    params[str(name)] = item.get("value")
        return params

    @staticmethod
    def _extract_sha(actions: Any) -> Optional[str]:
        if not isinstance(actions, list):
            return None
        for action in actions:
            if not isinstance(action, dict):
                continue
            builds = action.get("buildsByBranchName")
            if isinstance(builds, dict):
                for entry in builds.values():
                    if isinstance(entry, dict):
                        sha = entry.get("revision", {}).get("SHA1")
                        if sha:
                            return str(sha)
        return None

    def _resolve_job(self, run_definition: RunDefinition, stage_target: Optional[Dict[str, Any]] = None) -> str:
        if stage_target and stage_target.get("job"):
            return str(stage_target["job"]).strip()
        if run_definition.provider_target.get("job"):
            return str(run_definition.provider_target["job"]).strip()
        if self.default_job:
            return self.default_job
        raise ValueError(f"run type '{run_definition.name}' missing provider_target.job")

    @staticmethod
    def _resolve_version(run_type: str, ref_name: str, inputs: Dict[str, Any]) -> Optional[str]:
        explicit = str(inputs.get("version", "") or "").strip()
        if explicit:
            return explicit
        if run_type == "release":
            match = re.match(r"^release-(\d+\.\d+\.\d+)$", ref_name)
            if match:
                return match.group(1)
        if run_type == "stable":
            match = re.match(r"^stable-(\d+\.\d+\.\d+)$", ref_name)
            if match:
                return match.group(1)
        return None

    def _queue_snapshot(self, job: str, queue_id: str, ref_name: str, parameters: Dict[str, Any]) -> ProviderRunSnapshot:
        return ProviderRunSnapshot(
            status="queued",
            provider_status="queued",
            provider_run_id=f"queue:{queue_id}",
            ref=ref_name,
            metadata={
                "job": job,
                "queue_item": queue_id,
                "parameters": parameters,
            },
        )

    def _resolve_build_from_queue(self, job: str, queue_id: str) -> Optional[str]:
        response = self.session.get(
            f"{self.endpoint}/queue/item/{quote(queue_id, safe='')}/api/json",
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json() or {}
        executable = payload.get("executable") or {}
        number = executable.get("number")
        if number is None:
            return None
        return str(number)

    def _wfapi_stages(self, job: str, build_id: str) -> List[CiStage]:
        response = self.session.get(
            self._job_api_url(job, f"/{quote(str(build_id), safe='')}/wfapi/describe"),
            timeout=15,
        )
        if not response.ok:
            return []
        payload = response.json() or {}
        stages: List[CiStage] = []
        for stage in payload.get("stages") or []:
            if not isinstance(stage, dict):
                continue
            status = self._normalize_status(stage.get("status"), stage.get("status") == "IN_PROGRESS")
            stage_id = stage.get("id")
            metadata: Dict[str, Any] = {}
            if stage_id is not None:
                metadata["id"] = stage_id
            stages.append(
                CiStage(
                    name=str(stage.get("name", "")),
                    provider=self.binding.name,
                    status=status,
                    started_at=_normalize_provider_timestamp(stage.get("startTimeMillis")),
                    finished_at=_normalize_provider_timestamp(stage.get("endTimeMillis")),
                    duration_seconds=((stage.get("durationMillis") or 0) / 1000.0) if stage.get("durationMillis") else None,
                    metadata=metadata,
                )
            )
        return stages

    def discover_definitions(self, repo: RepoDefinition) -> Dict[str, Dict[str, Any]]:
        job = self.default_job
        if self.binding.config.get("simulate"):
            if not job:
                return {}
            return {job: {"name": job, "job": job, "stages": []}}
        if not job:
            return {}

        response = self.session.get(
            self._job_api_url(job, "/api/json"),
            params={"tree": "name,fullName,url,buildable,lastBuild[number]"},
            timeout=15,
        )
        if not response.ok:
            logger.warning("Jenkins job discovery failed for %s: %s", job, response.status_code)
            return {}
        payload = response.json() or {}
        last_build = (payload.get("lastBuild") or {}).get("number")
        stage_names: List[str] = []
        if last_build is not None:
            stage_names = [stage.name for stage in self._wfapi_stages(job, str(last_build))]
        return {
            job: {
                "name": payload.get("name", job),
                "job": payload.get("fullName", job),
                "url": payload.get("url"),
                "buildable": bool(payload.get("buildable", True)),
                "stages": stage_names,
            }
        }

    def trigger_run(
        self,
        repo: RepoDefinition,
        run_definition: RunDefinition,
        ref: Optional[str],
        inputs: Optional[Dict[str, Any]] = None,
    ) -> ProviderRunSnapshot:
        ref_name = ref or repo.metadata.get("default_ref", "main")
        data = dict(inputs or {})
        run_type = str(data.get("run_type", run_definition.name))
        job = self._resolve_job(run_definition)
        version = self._resolve_version(run_type, ref_name, data)
        parameters: Dict[str, Any] = {
            "RUN_TYPE": run_type,
            "REF_NAME": ref_name,
            "BUMP": str(data.get("bump", "patch")),
            "ACTOR": str(data.get("actor", "coralforge")),
            "DRY_RUN": str(data.get("dry_run", True)).lower(),
        }
        if version:
            parameters["VERSION"] = version
            parameters["version"] = version

        if self.binding.config.get("simulate"):
            return ProviderRunSnapshot(
                status="queued",
                provider_status="simulated",
                provider_run_id=f"sim-{run_type}-{job}-{ref_name}",
                ref=ref_name,
                metadata={"job": job, "simulated": True, "parameters": parameters},
            )

        self._ensure_crumb()
        response = self.session.post(
            self._job_api_url(job, "/buildWithParameters"),
            params=parameters,
            headers=self._crumb_header,
            timeout=15,
        )
        response.raise_for_status()
        location = (response.headers or {}).get("Location", "")
        queue_id = location.rstrip("/").split("/")[-1] if location else "unknown"
        return self._queue_snapshot(job, queue_id, ref_name, parameters)

    def get_run(self, repo: RepoDefinition, provider_run_id: str) -> ProviderRunSnapshot:
        run_definition = repo.run_types.get("ci")
        if provider_run_id.startswith("sim-"):
            return ProviderRunSnapshot(
                status="passed",
                provider_status="SUCCESS",
                provider_run_id=provider_run_id,
                ref=repo.metadata.get("default_ref", "main"),
                metadata={"simulated": True},
            )
        if run_definition is None:
            run_definition = next(iter(repo.run_types.values()), RunDefinition(name="ci", default_provider=self.binding.name))
        job = self._resolve_job(run_definition)

        build_id = provider_run_id
        if provider_run_id.startswith("queue:"):
            queue_id = provider_run_id.split(":", 1)[1]
            resolved = self._resolve_build_from_queue(job, queue_id)
            if resolved is None:
                return ProviderRunSnapshot(
                    status="queued",
                    provider_status="queued",
                    provider_run_id=provider_run_id,
                    metadata={"job": job, "queue_item": queue_id},
                )
            build_id = resolved

        response = self.session.get(
            self._job_api_url(
                job,
                f"/{quote(str(build_id), safe='')}/api/json",
            ),
            params={"tree": "id,number,url,result,building,timestamp,duration,actions[parameters[name,value],buildsByBranchName[*]]"},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json() or {}
        actions = payload.get("actions") or []
        params = self._extract_parameters(actions)
        ref_name = str(params.get("REF_NAME") or params.get("BRANCH_NAME") or "")
        created_at = _normalize_provider_timestamp(payload.get("timestamp"))
        finished_at = _normalize_provider_timestamp((payload.get("timestamp") or 0) + (payload.get("duration") or 0))
        stages = self._wfapi_stages(job, str(payload.get("number", build_id)))
        provider_status = payload.get("result") or ("RUNNING" if payload.get("building") else "QUEUED")
        normalized_status = self._normalize_status(payload.get("result"), bool(payload.get("building")))
        metadata: Dict[str, Any] = {
            "job": job,
            "build_number": payload.get("number"),
            "run_type": params.get("RUN_TYPE"),
            "version": params.get("VERSION") or params.get("version"),
            "parameters": params,
        }
        return ProviderRunSnapshot(
            status=normalized_status,
            provider_status=str(provider_status),
            provider_run_id=str(payload.get("number", build_id)),
            url=payload.get("url"),
            created_at=created_at,
            started_at=created_at,
            finished_at=finished_at if normalized_status not in {"queued", "running"} else None,
            sha=self._extract_sha(actions),
            ref=ref_name or None,
            stages=stages,
            metadata=metadata,
        )

    def list_recent_runs(self, repo: RepoDefinition, limit: int = 20) -> List[ProviderRunSnapshot]:
        if self.binding.config.get("simulate"):
            return []
        run_definition = next(iter(repo.run_types.values()), RunDefinition(name="ci", default_provider=self.binding.name))
        job = self._resolve_job(run_definition)
        response = self.session.get(
            self._job_api_url(job, "/api/json"),
            params={"tree": f"builds[number,result,building,url,timestamp,duration]{{0,{limit}}}"},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json() or {}
        snapshots: List[ProviderRunSnapshot] = []
        for item in payload.get("builds") or []:
            if not isinstance(item, dict):
                continue
            number = item.get("number")
            if number is None:
                continue
            status = self._normalize_status(item.get("result"), bool(item.get("building")))
            snapshots.append(
                ProviderRunSnapshot(
                    status=status,
                    provider_status=str(item.get("result") or ("RUNNING" if item.get("building") else "QUEUED")),
                    provider_run_id=str(number),
                    url=item.get("url"),
                    created_at=_normalize_provider_timestamp(item.get("timestamp")),
                    started_at=_normalize_provider_timestamp(item.get("timestamp")),
                    finished_at=_normalize_provider_timestamp((item.get("timestamp") or 0) + (item.get("duration") or 0)),
                    metadata={"job": job, "build_number": number},
                )
            )
        return snapshots

    def find_build_by_ref(self, repo: RepoDefinition, ref: str) -> Optional[str]:
        if self.binding.config.get("simulate"):
            return None
        run_definition = next(iter(repo.run_types.values()), RunDefinition(name="ci", default_provider=self.binding.name))
        job = self._resolve_job(run_definition)
        response = self.session.get(
            self._job_api_url(job, "/api/json"),
            params={"tree": "builds[number]"},
            timeout=15,
        )
        if not response.ok:
            return None
        payload = response.json() or {}
        for item in payload.get("builds") or []:
            if not isinstance(item, dict):
                continue
            build_number = item.get("number")
            if build_number is None:
                continue
            try:
                snapshot = self.get_run(repo, str(build_number))
            except Exception:
                continue
            params = snapshot.metadata.get("parameters") or {}
            if str(params.get("REF_NAME", "")) == ref:
                return str(build_number)
        return None

    def get_logs(self, repo: RepoDefinition, provider_run_id: str) -> Dict[str, str]:
        if provider_run_id.startswith("sim-"):
            return {"simulated": "Simulated Jenkins run; no remote logs were fetched."}
        run_definition = next(iter(repo.run_types.values()), RunDefinition(name="ci", default_provider=self.binding.name))
        job = self._resolve_job(run_definition)
        if provider_run_id.startswith("queue:"):
            return {"queue": "Build is still queued; logs are not available yet."}
        response = self.session.get(
            self._job_api_url(job, f"/{quote(str(provider_run_id), safe='')}/consoleText"),
            timeout=30,
        )
        response.raise_for_status()
        return {"console": response.text}


def build_ci_connector(binding: ProviderBinding) -> CiProviderConnector:
    if binding.kind == "github-actions":
        return GitHubActionsConnector(binding)
    if binding.kind == "drone":
        return DroneConnector(binding)
    if binding.kind == "jenkins":
        return JenkinsConnector(binding)
    raise ValueError(f"unsupported CI provider kind: {binding.kind}")
