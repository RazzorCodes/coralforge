"""Compatibility helpers for the legacy release-state module.

Coralforge now runs through the normalized orchestrator in
`src.orchestration`. This module keeps a small compatibility surface for
older imports while making it explicit that the release-specific state
machine is no longer the primary execution model.
"""

from __future__ import annotations

from typing import List, Tuple

UNKNOWN = "unknown"
QUEUED = "queued"
RUNNING = "running"
BLOCKED = "blocked"
PASSED = "passed"
FAILED = "failed"
CANCELLED = "cancelled"
RELEASED = "released"
DONE = "done"

ALL_STATUSES = [
    UNKNOWN,
    QUEUED,
    RUNNING,
    BLOCKED,
    PASSED,
    FAILED,
    CANCELLED,
    RELEASED,
    DONE,
]


def _parse_semver(v: str) -> Tuple[int, int, int]:
    parts = v.strip().lstrip("v").split(".")
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        return (0, 0, 0)


def bump_version(versions: List[str], bump: str) -> str:
    """Return the next semantic version string from a version list."""
    parsed = [_parse_semver(v) for v in versions if v]
    base = max(parsed) if parsed else (0, 0, 0)
    major, minor, patch = base
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


class ReleaseMachine:
    """Compatibility stub for the retired GitHub-only release machine."""

    def __init__(self, *_args, **_kwargs):
        raise RuntimeError(
            "ReleaseMachine has been retired. Use src.orchestration.service.OrchestrationService instead."
        )
