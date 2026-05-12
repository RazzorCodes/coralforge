"""Normalized multi-provider CI orchestration service."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import threading
import uuid
from typing import Any, Dict, List, Optional, Tuple

from src.config.app_config import AppConfig
from src.data.data_connector import StateStore
from src.orchestration.models import (
    AuditEntry,
    CiRun,
    CiStage,
    CiStep,
    LogSnapshot,
    ProviderRunSnapshot,
    RepoDefinition,
    RunDefinition,
)
from src.orchestration.providers import build_ci_connector

logger = logging.getLogger("coralforge.service")

ACTIVE_RUN_STATUSES = {"queued", "running", "blocked"}


def _normalize_timestamp(value: Any) -> Optional[str]:
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


class OrchestrationService:
    def __init__(self, config: AppConfig, store: StateStore):
        self.config = config
        self.store = store
        self._connectors: Dict[Tuple[str, str], Any] = {}
        self._lock = threading.Lock()
        self._repo_definition_cache: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}

    def _connector_for(self, repo: RepoDefinition, provider_name: str):
        key = (repo.name, provider_name)
        if key not in self._connectors:
            binding = repo.providers[provider_name]
            self._connectors[key] = build_ci_connector(binding)
        return self._connectors[key]

    def _provider_definitions(self, repo: RepoDefinition, provider_name: str) -> Dict[str, Dict[str, Any]]:
        key = (repo.name, provider_name)
        if key not in self._repo_definition_cache:
            connector = self._connector_for(repo, provider_name)
            self._repo_definition_cache[key] = connector.discover_definitions(repo)
        return self._repo_definition_cache[key]

    def validate_repositories(self) -> None:
        for repo in self.config.repos:
            for provider_name, binding in repo.providers.items():
                try:
                    discovered = self._provider_definitions(repo, provider_name)
                except Exception as exc:
                    repo.validation_errors.append(
                        f"provider discovery failed for '{provider_name}': {exc}"
                    )
                    self._audit(
                        AuditEntry(
                            repo=repo.name,
                            provider=provider_name,
                            event_type="provider.discovery_failed",
                            message=f"Provider discovery failed: {exc}",
                        )
                    )
                    continue

                for run_name, run_definition in repo.run_types.items():
                    stage_definitions = [
                        stage for stage in run_definition.stages if stage.provider == provider_name
                    ]
                    if not stage_definitions and run_definition.default_provider != provider_name:
                        continue
                    self._validate_run_definition(repo, binding.kind, run_name, run_definition, discovered)

            self.store.set_repo_state(
                repo.name,
                {
                    "config_path": repo.config_path,
                    "config_version": repo.config_version,
                    "validation_errors": list(repo.validation_errors),
                    "providers": list(repo.providers.keys()),
                },
            )

    def _validate_run_definition(
        self,
        repo: RepoDefinition,
        provider_kind: str,
        run_name: str,
        run_definition: RunDefinition,
        discovered: Dict[str, Dict[str, Any]],
    ) -> None:
        if provider_kind == "github-actions":
            workflow = run_definition.provider_target.get("workflow")
            if workflow and workflow not in discovered:
                repo.validation_errors.append(
                    f"run type '{run_name}' references missing GitHub workflow '{workflow}'"
                )
            for stage in run_definition.stages:
                if stage.provider not in repo.providers:
                    continue
                if repo.providers[stage.provider].kind != "github-actions":
                    continue
                workflow_name = stage.target.get("workflow")
                if workflow_name and workflow_name not in discovered:
                    repo.validation_errors.append(
                        f"stage '{run_name}:{stage.name}' references missing GitHub workflow '{workflow_name}'"
                    )
                    continue
                job_name = stage.target.get("job")
                if workflow_name and job_name:
                    jobs = discovered.get(workflow_name, {}).get("jobs") or []
                    if job_name not in jobs:
                        repo.validation_errors.append(
                            f"stage '{run_name}:{stage.name}' references missing workflow job '{job_name}'"
                        )
        elif provider_kind == "drone":
            pipeline = run_definition.provider_target.get("pipeline")
            if pipeline and pipeline not in discovered:
                repo.validation_errors.append(
                    f"run type '{run_name}' references missing Drone pipeline '{pipeline}'"
                )
            for stage in run_definition.stages:
                if stage.provider not in repo.providers:
                    continue
                if repo.providers[stage.provider].kind != "drone":
                    continue
                pipeline_name = stage.target.get("pipeline") or run_definition.provider_target.get("pipeline")
                if pipeline_name and pipeline_name not in discovered:
                    repo.validation_errors.append(
                        f"stage '{run_name}:{stage.name}' references missing Drone pipeline '{pipeline_name}'"
                    )
                    continue
                step_name = stage.target.get("step")
                if pipeline_name and step_name:
                    steps = discovered.get(pipeline_name, {}).get("steps") or []
                    if step_name not in steps:
                        repo.validation_errors.append(
                            f"stage '{run_name}:{stage.name}' references missing Drone step '{step_name}'"
                        )

    def list_repo_names(self) -> List[str]:
        return self.config.get_repo_names()

    def list_repos(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": repo.name,
                "slug": repo.repo_slug,
                "workspace_path": repo.workspace_path,
                "config_path": repo.config_path,
                "config_version": repo.config_version,
                "providers": {
                    key: {
                        "kind": binding.kind,
                        "simulate": bool(binding.config.get("simulate")),
                    }
                    for key, binding in repo.providers.items()
                },
                "run_types": list(repo.run_types.keys()),
                "validation_errors": list(repo.validation_errors),
            }
            for repo in self.config.repos
        ]

    def _initial_stages(self, run_definition: RunDefinition) -> List[CiStage]:
        stages: List[CiStage] = []
        first_stage_name = run_definition.stages[0].name if run_definition.stages else None
        for stage_definition in run_definition.stages:
            if stage_definition.mode == "manual":
                status = "blocked"
            elif stage_definition.name == first_stage_name:
                status = "queued"
            else:
                status = "blocked" if stage_definition.depends_on else "queued"
            stages.append(
                CiStage(
                    name=stage_definition.name,
                    provider=stage_definition.provider,
                    provider_target=dict(stage_definition.target),
                    mode=stage_definition.mode,
                    status=status,
                    metadata={"runner": stage_definition.runner, "depends_on": stage_definition.depends_on},
                )
            )
        return stages

    def _audit(self, entry: AuditEntry) -> None:
        self.store.append_audit_entry(entry)

    def _persist_run(self, run: CiRun, message: str, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        entry = AuditEntry(
            repo=run.repo,
            run_id=run.run_id,
            provider=run.provider,
            event_type=event_type,
            message=message,
            payload=payload or {},
        )
        run.timeline.insert(0, entry)
        self.store.upsert_run(run)
        self._audit(entry)

    def _find_matching_stage(
        self,
        stage_definition,
        snapshot: ProviderRunSnapshot,
    ) -> Tuple[Optional[CiStage], Optional[CiStep]]:
        target = stage_definition.target or {}
        target_name = target.get("step") or target.get("stage")
        if target_name:
            for stage in snapshot.stages:
                if stage.name == target_name:
                    return stage, None
                for step in stage.steps:
                    if step.name == target_name:
                        return stage, step

        for stage in snapshot.stages:
            if stage.name == stage_definition.name:
                return stage, None
            for step in stage.steps:
                if step.name == stage_definition.name:
                    return stage, step
        return None, None

    def _copy_step(self, step: CiStep) -> CiStep:
        return CiStep(
            name=step.name,
            status=step.status,
            started_at=_normalize_timestamp(step.started_at),
            finished_at=_normalize_timestamp(step.finished_at),
            duration_seconds=step.duration_seconds,
            provider_step=step.provider_step,
            log_key=step.log_key,
            metadata=dict(step.metadata),
        )

    def _normalize_snapshot_for_run_definition(
        self,
        run_definition: RunDefinition,
        provider_name: str,
        snapshot: ProviderRunSnapshot,
    ) -> ProviderRunSnapshot:
        normalized_stages: List[CiStage] = []
        initial_by_name = {stage.name: stage for stage in self._initial_stages(run_definition)}

        for stage_definition in run_definition.stages:
            if stage_definition.provider != provider_name:
                normalized_stages.append(initial_by_name[stage_definition.name])
                continue

            provider_stage, provider_step = self._find_matching_stage(stage_definition, snapshot)
            if provider_stage is None:
                fallback = initial_by_name[stage_definition.name]
                fallback.status = "unknown" if snapshot.status not in ACTIVE_RUN_STATUSES else fallback.status
                normalized_stages.append(fallback)
                continue

            stage_status = provider_step.status if provider_step is not None else provider_stage.status
            stage_started_at = provider_step.started_at if provider_step is not None else provider_stage.started_at
            stage_finished_at = provider_step.finished_at if provider_step is not None else provider_stage.finished_at
            stage_duration = (
                provider_step.duration_seconds if provider_step is not None else provider_stage.duration_seconds
            )
            stage_steps = (
                [self._copy_step(provider_step)]
                if provider_step is not None
                else [self._copy_step(step) for step in provider_stage.steps]
            )
            metadata = dict(provider_stage.metadata)
            if provider_stage.name != stage_definition.name:
                metadata["provider_stage_name"] = provider_stage.name
            if provider_step is not None:
                metadata["provider_step_name"] = provider_step.name
            if provider_stage.provider_target:
                metadata["provider_stage_target"] = dict(provider_stage.provider_target)

            normalized_stages.append(
                CiStage(
                    name=stage_definition.name,
                    status=stage_status,
                    provider=provider_name,
                    provider_target=dict(stage_definition.target),
                    mode=stage_definition.mode,
                    started_at=stage_started_at,
                    finished_at=stage_finished_at,
                    duration_seconds=stage_duration,
                    steps=stage_steps,
                    metadata=metadata,
                )
            )

        return ProviderRunSnapshot(
            status=snapshot.status,
            provider_status=snapshot.provider_status,
            provider_run_id=snapshot.provider_run_id,
            url=snapshot.url,
            created_at=_normalize_timestamp(snapshot.created_at),
            started_at=_normalize_timestamp(snapshot.started_at),
            finished_at=_normalize_timestamp(snapshot.finished_at),
            sha=snapshot.sha,
            ref=snapshot.ref,
            stages=normalized_stages,
            metadata=dict(snapshot.metadata),
            logs=dict(snapshot.logs),
        )

    def _score_run_definition(
        self,
        run_definition: RunDefinition,
        provider_name: str,
        snapshot: ProviderRunSnapshot,
    ) -> int:
        provider_stage_definitions = [
            stage for stage in run_definition.stages if stage.provider == provider_name
        ]
        if not provider_stage_definitions and run_definition.default_provider != provider_name:
            return -1

        score = 0
        target_pipeline = run_definition.provider_target.get("pipeline")
        snapshot_pipeline = snapshot.metadata.get("pipeline")
        if target_pipeline and snapshot_pipeline:
            if target_pipeline != snapshot_pipeline:
                return -1
            score += 10

        for stage_definition in provider_stage_definitions:
            matched_stage, matched_step = self._find_matching_stage(stage_definition, snapshot)
            if matched_stage is not None or matched_step is not None:
                score += 2
            elif stage_definition.target.get("step") or stage_definition.target.get("stage"):
                score -= 3

        return score

    def _select_run_definition_for_snapshot(
        self,
        repo: RepoDefinition,
        provider_name: str,
        snapshot: ProviderRunSnapshot,
    ) -> Optional[Tuple[str, RunDefinition]]:
        best_name: Optional[str] = None
        best_definition: Optional[RunDefinition] = None
        best_score = -1

        for run_name, run_definition in repo.run_types.items():
            score = self._score_run_definition(run_definition, provider_name, snapshot)
            if score > best_score:
                best_name = run_name
                best_definition = run_definition
                best_score = score

        if best_name is None or best_definition is None or best_score <= 0:
            return None
        return best_name, best_definition

    def _current_stage_name(self, stages: List[CiStage], fallback: Optional[str] = None) -> Optional[str]:
        current_stage = next((stage.name for stage in stages if stage.status in ACTIVE_RUN_STATUSES), None)
        if current_stage is None:
            current_stage = next((stage.name for stage in stages if stage.status == "failed"), fallback)
        return current_stage

    def _import_recent_provider_runs(self, repo: RepoDefinition, provider_name: str, limit: int) -> None:
        connector = self._connector_for(repo, provider_name)
        try:
            snapshots = connector.list_recent_runs(repo, limit=limit)
        except Exception as exc:
            logger.warning("Recent run import failed for %s/%s: %s", repo.name, provider_name, exc)
            self._audit(
                AuditEntry(
                    repo=repo.name,
                    provider=provider_name,
                    event_type="run.import_failed",
                    message=f"Failed to import recent runs: {exc}",
                )
            )
            return

        existing_runs = self.store.list_runs(repo_name=repo.name, limit=max(limit * 10, 100))
        existing_provider_ids = {
            (payload.get("provider"), str(payload.get("provider_run_id")))
            for payload in existing_runs
            if payload.get("provider_run_id") is not None
        }

        for snapshot in snapshots:
            provider_run_id = str(snapshot.provider_run_id or "").strip()
            if not provider_run_id:
                continue
            if (provider_name, provider_run_id) in existing_provider_ids:
                continue

            matched = self._select_run_definition_for_snapshot(repo, provider_name, snapshot)
            if matched is None:
                continue

            run_type, run_definition = matched
            normalized_snapshot = self._normalize_snapshot_for_run_definition(
                run_definition,
                provider_name,
                snapshot,
            )
            normalized_snapshot.metadata.setdefault(
                "pipeline",
                run_definition.provider_target.get("pipeline"),
            )
            normalized_stages = normalized_snapshot.stages or self._initial_stages(run_definition)

            run = CiRun(
                run_id=str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"coralforge:{repo.name}:{provider_name}:{provider_run_id}",
                    )
                ),
                repo=repo.name,
                run_type=run_type,
                provider=provider_name,
                provider_kind=repo.providers[provider_name].kind,
                status=normalized_snapshot.status,
                current_stage=self._current_stage_name(
                    normalized_stages,
                    run_definition.stages[0].name if run_definition.stages else None,
                ),
                actor="provider-import",
                created_at=normalized_snapshot.created_at or None,
                started_at=normalized_snapshot.started_at,
                finished_at=normalized_snapshot.finished_at,
                ref=normalized_snapshot.ref,
                sha=normalized_snapshot.sha,
                config_version=repo.config_version,
                provider_run_id=provider_run_id,
                provider_url=normalized_snapshot.url,
                provider_status=normalized_snapshot.provider_status,
                provider_metadata=normalized_snapshot.metadata,
                stages=normalized_stages,
            )
            self._persist_run(
                run,
                message=f"Imported provider run '{provider_run_id}' from '{provider_name}'",
                event_type="run.imported",
                payload={"provider_status": run.provider_status, "run_type": run.run_type},
            )
            existing_provider_ids.add((provider_name, provider_run_id))

    def trigger_run(
        self,
        repo_name: str,
        run_type: str,
        ref: Optional[str] = None,
        actor: str = "api",
        provider_name: Optional[str] = None,
        inputs: Optional[Dict[str, Any]] = None,
    ) -> CiRun:
        repo = self.config.get_repo(repo_name)
        if repo is None:
            raise KeyError(f"unknown repo '{repo_name}'")
        if run_type not in repo.run_types:
            raise KeyError(f"repo '{repo_name}' does not support run type '{run_type}'")

        run_definition = repo.run_types[run_type]
        provider_name = provider_name or run_definition.default_provider
        if provider_name not in repo.providers:
            raise KeyError(f"provider '{provider_name}' not configured for repo '{repo_name}'")

        connector = self._connector_for(repo, provider_name)
        provider_snapshot = connector.trigger_run(repo, run_definition, ref, inputs)
        run = CiRun(
            run_id=str(uuid.uuid4()),
            repo=repo.name,
            run_type=run_type,
            provider=provider_name,
            provider_kind=repo.providers[provider_name].kind,
            status=provider_snapshot.status,
            current_stage=run_definition.stages[0].name if run_definition.stages else None,
            actor=actor,
            created_at=provider_snapshot.created_at or None,
            started_at=provider_snapshot.started_at,
            finished_at=provider_snapshot.finished_at,
            ref=provider_snapshot.ref or ref,
            sha=provider_snapshot.sha,
            config_version=repo.config_version,
            provider_run_id=provider_snapshot.provider_run_id,
            provider_url=provider_snapshot.url,
            provider_status=provider_snapshot.provider_status,
            provider_metadata=provider_snapshot.metadata,
            stages=provider_snapshot.stages or self._initial_stages(run_definition),
        )
        self._persist_run(
            run,
            message=f"Triggered {run_type} run via provider '{provider_name}'",
            event_type="run.triggered",
            payload={"ref": run.ref, "provider_status": run.provider_status},
        )
        return run

    def _coerce_run(self, payload: Dict[str, Any]) -> CiRun:
        timeline = [
            AuditEntry(**entry) for entry in payload.get("timeline") or []
        ]
        stages = []
        for stage_payload in payload.get("stages") or []:
            stage = CiStage(
                name=stage_payload["name"],
                status=stage_payload["status"],
                provider=stage_payload["provider"],
                provider_target=dict(stage_payload.get("provider_target") or {}),
                mode=stage_payload.get("mode", "automatic"),
                started_at=_normalize_timestamp(stage_payload.get("started_at")),
                finished_at=_normalize_timestamp(stage_payload.get("finished_at")),
                duration_seconds=stage_payload.get("duration_seconds"),
                metadata=dict(stage_payload.get("metadata") or {}),
                steps=[],
            )
            for step_payload in stage_payload.get("steps") or []:
                from src.orchestration.models import CiStep

                stage.steps.append(
                    CiStep(
                        name=step_payload["name"],
                        status=step_payload["status"],
                        started_at=_normalize_timestamp(step_payload.get("started_at")),
                        finished_at=_normalize_timestamp(step_payload.get("finished_at")),
                        duration_seconds=step_payload.get("duration_seconds"),
                        provider_step=step_payload.get("provider_step"),
                        log_key=step_payload.get("log_key"),
                        metadata=dict(step_payload.get("metadata") or {}),
                    )
                )
            stages.append(stage)

        return CiRun(
            run_id=payload["run_id"],
            repo=payload["repo"],
            run_type=payload["run_type"],
            provider=payload["provider"],
            provider_kind=payload["provider_kind"],
            status=payload["status"],
            current_stage=payload.get("current_stage"),
            actor=payload.get("actor", "unknown"),
            created_at=_normalize_timestamp(payload.get("created_at")),
            started_at=_normalize_timestamp(payload.get("started_at")),
            finished_at=_normalize_timestamp(payload.get("finished_at")),
            ref=payload.get("ref"),
            sha=payload.get("sha"),
            version=payload.get("version"),
            config_version=payload.get("config_version"),
            provider_run_id=payload.get("provider_run_id"),
            provider_url=payload.get("provider_url"),
            provider_status=payload.get("provider_status"),
            provider_metadata=dict(payload.get("provider_metadata") or {}),
            stages=stages,
            timeline=timeline,
            errors=list(payload.get("errors") or []),
        )

    def list_runs(
        self,
        repo_name: Optional[str] = None,
        run_type: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        repos = [self.config.get_repo(repo_name)] if repo_name else self.config.repos
        for repo in repos:
            if repo is None:
                continue
            for provider_name in repo.providers:
                self._import_recent_provider_runs(repo, provider_name, limit)
        payloads = self.store.list_runs(repo_name=repo_name, run_type=run_type, limit=limit)
        return [self._coerce_run(payload).to_dict() for payload in payloads]

    def get_run(self, run_id: str, refresh: bool = False) -> Optional[Dict[str, Any]]:
        payload = self.store.get_run(run_id)
        if payload is None:
            return None
        run = self._coerce_run(payload)
        if refresh and run.status in ACTIVE_RUN_STATUSES:
            try:
                run = self.refresh_run(run)
            except Exception as exc:
                logger.warning("Run refresh failed for %s during get_run: %s", run.run_id, exc)
                self._audit(
                    AuditEntry(
                        repo=run.repo,
                        run_id=run.run_id,
                        provider=run.provider,
                        event_type="run.refresh_failed",
                        message=f"Failed to refresh run: {exc}",
                    )
                )
        return run.to_dict()

    def _merge_stage_statuses(self, existing: List[CiStage], snapshot: ProviderRunSnapshot) -> List[CiStage]:
        if not snapshot.stages:
            return existing

        merged: List[CiStage] = []
        by_name = {stage.name: stage for stage in snapshot.stages}
        for stage in existing:
            if stage.name in by_name:
                remote_stage = by_name[stage.name]
                stage.status = remote_stage.status
                stage.started_at = remote_stage.started_at
                stage.finished_at = remote_stage.finished_at
                stage.steps = remote_stage.steps
                stage.metadata.update(remote_stage.metadata)
            merged.append(stage)
        for stage in snapshot.stages:
            if stage.name not in {item.name for item in merged}:
                merged.append(stage)
        return merged

    def refresh_run(self, run: CiRun) -> CiRun:
        repo = self.config.get_repo(run.repo)
        if repo is None or not run.provider_run_id:
            return run

        connector = self._connector_for(repo, run.provider)
        snapshot = connector.get_run(repo, run.provider_run_id)
        run_definition = repo.run_types.get(run.run_type)
        if run_definition is not None:
            snapshot = self._normalize_snapshot_for_run_definition(run_definition, run.provider, snapshot)
            snapshot.metadata.setdefault("pipeline", run_definition.provider_target.get("pipeline"))
        run.status = snapshot.status
        run.provider_status = snapshot.provider_status
        run.provider_url = snapshot.url or run.provider_url
        run.started_at = _normalize_timestamp(run.started_at) or _normalize_timestamp(snapshot.started_at)
        run.created_at = _normalize_timestamp(run.created_at) or _normalize_timestamp(snapshot.created_at) or run.created_at
        run.finished_at = _normalize_timestamp(run.finished_at)
        run.sha = snapshot.sha or run.sha
        run.ref = snapshot.ref or run.ref
        run.provider_metadata.update(snapshot.metadata)
        run.stages = self._merge_stage_statuses(run.stages, snapshot)

        if snapshot.status not in ACTIVE_RUN_STATUSES and run.finished_at is None:
            from src.orchestration.models import utcnow_iso

            run.finished_at = _normalize_timestamp(snapshot.finished_at) or utcnow_iso()

        run.current_stage = self._current_stage_name(run.stages, run.current_stage)
        self._persist_run(
            run,
            message=f"Refreshed provider status: {run.provider_status}",
            event_type="run.refreshed",
            payload={"status": run.status},
        )
        return run

    def refresh_active_runs(self) -> int:
        refreshed = 0
        for payload in self.store.list_runs(limit=200):
            if payload.get("status") not in ACTIVE_RUN_STATUSES:
                continue
            run = self._coerce_run(payload)
            try:
                self.refresh_run(run)
                refreshed += 1
            except Exception as exc:
                logger.warning("Run refresh failed for %s: %s", run.run_id, exc)
                self._audit(
                    AuditEntry(
                        repo=run.repo,
                        run_id=run.run_id,
                        provider=run.provider,
                        event_type="run.refresh_failed",
                        message=f"Failed to refresh run: {exc}",
                    )
                )
        return refreshed

    def get_logs(self, run_id: str, refresh: bool = False) -> Optional[Dict[str, Any]]:
        snapshot = self.store.get_log_snapshot(run_id)
        if snapshot and not refresh:
            return snapshot

        payload = self.store.get_run(run_id)
        if payload is None:
            return None
        run = self._coerce_run(payload)
        repo = self.config.get_repo(run.repo)
        if repo is None or not run.provider_run_id:
            return snapshot

        connector = self._connector_for(repo, run.provider)
        logs = connector.get_logs(repo, run.provider_run_id)
        snapshot_model = LogSnapshot(run_id=run_id, provider=run.provider, logs=logs)
        self.store.save_log_snapshot(snapshot_model)
        self._audit(
            AuditEntry(
                repo=run.repo,
                run_id=run.run_id,
                provider=run.provider,
                event_type="run.logs_fetched",
                message="Fetched provider logs",
                payload={"keys": list(logs.keys())},
            )
        )
        return snapshot_model.to_dict()

    def get_provider_metadata(self, run_id: str) -> Optional[Dict[str, Any]]:
        payload = self.store.get_run(run_id)
        if payload is None:
            return None
        return {
            "provider": payload.get("provider"),
            "provider_kind": payload.get("provider_kind"),
            "provider_run_id": payload.get("provider_run_id"),
            "provider_url": payload.get("provider_url"),
            "provider_status": payload.get("provider_status"),
            "provider_metadata": payload.get("provider_metadata") or {},
        }

    def latest_run_for_repo(self, repo_name: str, run_type: Optional[str] = None) -> Optional[CiRun]:
        runs = self.store.list_runs(repo_name=repo_name, run_type=run_type, limit=1)
        if not runs:
            return None
        return self._coerce_run(runs[0])

    def mark_stable(self, repo_name: str, version: str) -> bool:
        return self.store.set_stable(repo_name, version)

    def get_stable(self, repo_name: Optional[str] = None) -> List[Dict[str, Any]]:
        if repo_name:
            repos = [repo_name]
        else:
            repos = self.config.get_repo_names()
        return [{"repo": name, "stable": self.store.get_stable(name)} for name in repos]

    def health(self) -> Dict[str, Any]:
        repo_errors = sum(len(repo.validation_errors) for repo in self.config.repos)
        return {
            "status": "ok" if repo_errors == 0 else "degraded",
            "repos": len(self.config.repos),
            "validation_errors": repo_errors,
            "runs": len(self.store.list_runs(limit=1000)),
            "store_type": type(self.store).__name__,
        }
