"""Legacy state compatibility package."""

from src.state.state import (
    ALL_STATUSES,
    BLOCKED,
    CANCELLED,
    DONE,
    FAILED,
    PASSED,
    QUEUED,
    RELEASED,
    RUNNING,
    UNKNOWN,
    ReleaseMachine,
    bump_version,
)

__all__ = [
    "ALL_STATUSES",
    "BLOCKED",
    "CANCELLED",
    "DONE",
    "FAILED",
    "PASSED",
    "QUEUED",
    "RELEASED",
    "RUNNING",
    "UNKNOWN",
    "ReleaseMachine",
    "bump_version",
]
