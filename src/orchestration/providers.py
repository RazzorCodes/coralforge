"""Provider interfaces and concrete GitHub/Drone implementations."""

from __future__ import annotations

from datetime import datetime, timezone
import io
import logging
import os
import zipfile
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

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

        response = self.session.post(
            f"{self.base_url}/actions/workflows/{workflow}/dispatches",
            json={"ref": ref_name, "inputs": inputs or {}},
            timeout=15,
        )
        response.raise_for_status()
        return ProviderRunSnapshot(
            status="queued",
            provider_status="dispatched",
            ref=ref_name,
            metadata={"workflow": workflow, "inputs": inputs or {}},
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
    ) -> ProviderRunSnapshot:
        trigger = pipeline_def.get("trigger") or {}
        ref_block = trigger.get("ref") or {}
        ref_includes = ref_block.get("include", []) if isinstance(ref_block, dict) else []

        tag_prefix: Optional[str] = None
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

        # Route to GitHub tag creation for pipelines that trigger on tag events
        definitions = self.discover_definitions(repo)
        pipeline_def = definitions.get(pipeline, {})
        trigger = pipeline_def.get("trigger") or {}
        event_block = trigger.get("event") or {}
        trigger_events = (
            event_block.get("include", []) if isinstance(event_block, dict)
            else event_block if isinstance(event_block, list)
            else []
        )
        if "tag" in trigger_events:
            sess = self._github_session()
            if not sess:
                raise ValueError(f"github_token required to trigger tag-based pipeline '{pipeline}'")
            return self._trigger_via_tag(sess, owner, repo_name, pipeline, pipeline_def, ref_name, inputs or {})

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


def build_ci_connector(binding: ProviderBinding) -> CiProviderConnector:
    if binding.kind == "github-actions":
        return GitHubActionsConnector(binding)
    if binding.kind == "drone":
        return DroneConnector(binding)
    raise ValueError(f"unsupported CI provider kind: {binding.kind}")
