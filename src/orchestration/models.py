"""Normalized domain models used by the Coralforge orchestrator."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _drop_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _drop_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_drop_none(item) for item in value]
    return value


@dataclass
class SecretReference:
    namespace: str
    name: str
    key: str = "value"

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


@dataclass
class ProviderBinding:
    name: str
    kind: str
    config: Dict[str, Any] = field(default_factory=dict)
    secrets: Dict[str, SecretReference] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["secrets"] = {
            key: value.to_dict() for key, value in self.secrets.items()
        }
        return _drop_none(payload)


@dataclass
class StageDefinition:
    name: str
    provider: str
    mode: str = "automatic"
    target: Dict[str, Any] = field(default_factory=dict)
    runner: Optional[str] = None
    depends_on: List[str] = field(default_factory=list)
    trigger: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return _drop_none(asdict(self))


@dataclass
class RunDefinition:
    name: str
    default_provider: str
    provider_target: Dict[str, Any] = field(default_factory=dict)
    trigger: Dict[str, Any] = field(default_factory=dict)
    stages: List[StageDefinition] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["stages"] = [stage.to_dict() for stage in self.stages]
        return _drop_none(payload)


@dataclass
class RepoDefinition:
    name: str
    owner: str
    repo: str
    workspace_path: str
    config_path: str
    config_version: int
    providers: Dict[str, ProviderBinding] = field(default_factory=dict)
    run_types: Dict[str, RunDefinition] = field(default_factory=dict)
    secret_references: Dict[str, SecretReference] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    validation_errors: List[str] = field(default_factory=list)

    @property
    def repo_slug(self) -> str:
        if self.owner and self.repo:
            return f"{self.owner}/{self.repo}"
        return self.repo or self.name

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["providers"] = {
            key: value.to_dict() for key, value in self.providers.items()
        }
        payload["run_types"] = {
            key: value.to_dict() for key, value in self.run_types.items()
        }
        payload["secret_references"] = {
            key: value.to_dict() for key, value in self.secret_references.items()
        }
        return _drop_none(payload)


@dataclass
class CiStep:
    name: str
    status: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    provider_step: Optional[str] = None
    log_key: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return _drop_none(asdict(self))


@dataclass
class CiStage:
    name: str
    status: str
    provider: str
    provider_target: Dict[str, Any] = field(default_factory=dict)
    mode: str = "automatic"
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    steps: List[CiStep] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["steps"] = [step.to_dict() for step in self.steps]
        return _drop_none(payload)


@dataclass
class AuditEntry:
    repo: str
    event_type: str
    message: str
    created_at: str = field(default_factory=utcnow_iso)
    run_id: Optional[str] = None
    provider: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return _drop_none(asdict(self))


@dataclass
class LogSnapshot:
    run_id: str
    provider: str
    logs: Dict[str, str]
    fetched_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> Dict[str, Any]:
        return _drop_none(asdict(self))


@dataclass
class CiRun:
    run_id: str
    repo: str
    run_type: str
    provider: str
    provider_kind: str
    status: str
    current_stage: Optional[str]
    actor: str
    created_at: str = field(default_factory=utcnow_iso)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    ref: Optional[str] = None
    sha: Optional[str] = None
    version: Optional[str] = None
    config_version: Optional[int] = None
    provider_run_id: Optional[str] = None
    provider_url: Optional[str] = None
    provider_status: Optional[str] = None
    provider_metadata: Dict[str, Any] = field(default_factory=dict)
    stages: List[CiStage] = field(default_factory=list)
    timeline: List[AuditEntry] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["stages"] = [stage.to_dict() for stage in self.stages]
        payload["timeline"] = [entry.to_dict() for entry in self.timeline]
        return _drop_none(payload)


@dataclass
class ProviderRunSnapshot:
    status: str
    provider_status: str
    provider_run_id: Optional[str] = None
    url: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    sha: Optional[str] = None
    ref: Optional[str] = None
    stages: List[CiStage] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    logs: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["stages"] = [stage.to_dict() for stage in self.stages]
        return _drop_none(payload)
