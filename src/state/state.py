"""State machine engine for coralforge release lifecycle.

Provides a minimal StateMachine base class and the concrete ReleaseMachine
that codes the release pipeline as a set of named states with evaluate()
transitioning based on external state (GitHub, brinecrypt).
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("coralforge.state")


class StateMachine(ABC):
    """Minimal state machine base.

    Subclasses define their own states and transitions in evaluate().
    """

    def __init__(self, initial_state: str):
        self._current_state: str = initial_state

    @property
    def current_state(self) -> str:
        return self._current_state

    @abstractmethod
    def evaluate(self, context: Dict[str, Any]) -> str:
        """Check conditions and transition to next state.

        Returns the new state name (may be the same as current if no
        transition condition is met).
        """
        ...


# ── Release lifecycle statuses ─────────────────────────────────────

UNKNOWN = "unknown"
BUILDING = "building"
UNIT_TEST = "unit-test"
INTEGRATION_TEST = "integration-test"
E2E_TEST = "e2e-test"
RELEASING = "releasing"
RELEASED = "released"
DONE = "done"

# Final publish phase sub-statuses tracked by the machine
STABLE = "stable"
FAILED = "failed"

ALL_STATUSES = [
    UNKNOWN, BUILDING, UNIT_TEST, INTEGRATION_TEST, E2E_TEST,
    RELEASING, RELEASED, DONE,
]

# Statuses that represent a completed/final state
TERMINAL_STATUSES = {DONE, FAILED}


# ── Helper: semver parsing (mirrors app_old.py) ────────────────────

def _parse_semver(v: str) -> Tuple[int, int, int]:
    parts = v.strip().lstrip("v").split(".")
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        return (0, 0, 0)


def bump_version(versions: List[str], bump: str) -> str:
    """Return the next version string given a list of existing versions."""
    parsed = [_parse_semver(v) for v in versions if v]
    base = max(parsed) if parsed else (0, 0, 0)
    major, minor, patch = base
    if bump == "major":
        return f"{major + 1}.0.0"
    elif bump == "minor":
        return f"{major}.{minor + 1}.0"
    else:
        return f"{major}.{minor}.{patch + 1}"


# ── The main release machine ───────────────────────────────────────

class ReleaseMachine(StateMachine):
    """State machine for a single repo's release lifecycle.

    States & transitions (code-defined):

        unknown  ──(trigger_release)──> building
        building ──(ci_stamp_detected)─> unit-test
        unit-test──(tag_intest_detected)> integration-test
        integration-test ──(tag_rc_detected)──> e2e-test
        e2e-test ──(manual_promote)───> releasing
        releasing ──(gh_release_detected)> released
        released ──(merged_or_cleanup)> done

    The machine stores its state externally via a StateStore (Postgres)
    and rehydrates on startup.
    """

    def __init__(
        self,
        repo_name: str,
        gh: Any,               # GitHubConnector instance
        bc: Any,               # BrinecryptConnector instance
        store: Any,            # StateStore instance
        initial_state: str = UNKNOWN,
    ):
        super().__init__(initial_state)
        self.repo_name = repo_name
        self.gh = gh
        self.bc = bc
        self.store = store

        # Load persisted state if available
        persisted = store.get_repo_state(repo_name)
        if persisted:
            self._current_state = persisted.get("status", initial_state)
            self.version = persisted.get("version")
            self.bump_type = persisted.get("bump_type", "patch")
            self.failed = persisted.get("failed", False)
            self.commit_hash = persisted.get("commit_hash")
            self.registries = persisted.get("registries", [])
        else:
            self.version = None
            self.bump_type = "patch"
            self.failed = False
            self.commit_hash = None
            self.registries = []

    def _persist(self) -> None:
        """Write current state to the store."""
        self.store.set_repo_state(self.repo_name, {
            "status": self._current_state,
            "version": self.version,
            "bump_type": self.bump_type,
            "failed": self.failed,
            "commit_hash": self.commit_hash,
            "registries": self.registries,
        })

    def trigger_release(self, bump: str = "patch") -> bool:
        """Manually trigger a new release. Only works from unknown/failed."""
        if self._current_state not in (UNKNOWN, FAILED, DONE):
            logger.info(
                "[%s] trigger_release skipped — current state is %s",
                self.repo_name, self._current_state,
            )
            return False

        self.bump_type = bump
        # Determine next version from existing branches + tags
        branches = self.gh.get_release_branches()  # {version: sha}
        tags = self.gh.get_tags()                   # {tag: sha}
        all_versions = list(branches.keys()) + [
            t.lstrip("v") for t in tags if not any(
                suffix in t for suffix in ("-intest", "-rc", "-stable")
            )
        ]
        self.version = bump_version(all_versions, bump)

        # Create release branch from main
        branch_name = f"release/{self.version}"
        main_sha = None
        # Use compare to find main head
        branches_dict = branches
        if not branches_dict:
            logger.warning("[%s] No existing release branches found", self.repo_name)

        # Attempt branch creation via GitHub API
        # We use the first existing branch SHA as a reference point
        if not self._create_release_branch(branch_name):
            logger.error("[%s] Failed to create branch %s", self.repo_name, branch_name)
            return False

        self._current_state = BUILDING
        self.failed = False
        self._persist()
        logger.info(
            "[%s] Release %s (%s) triggered → building",
            self.repo_name, self.version, bump,
        )
        return True

    def _create_release_branch(self, branch_name: str) -> bool:
        """Create a release branch from main via GitHub API."""
        try:
            # Get main branch SHA
            r = self.gh.session.get(
                f"{self.gh.base_url}/git/ref/heads/main",
                timeout=10,
            )
            if r.status_code != 200:
                logger.error("[%s] Cannot get main branch ref: %s", self.repo_name, r.text)
                return False

            main_sha = r.json().get("object", {}).get("sha")
            if not main_sha:
                logger.error("[%s] No SHA in main branch response", self.repo_name)
                return False

            # Create new ref from main SHA
            r = self.gh.session.post(
                f"{self.gh.base_url}/git/refs",
                json={"ref": f"refs/heads/{branch_name}", "sha": main_sha},
                timeout=10,
            )
            if r.status_code not in (201, 422):
                logger.error("[%s] Failed to create branch: %s", self.repo_name, r.text)
                return False

            logger.info("[%s] Created branch %s", self.repo_name, branch_name)
            return True
        except Exception as e:
            logger.error("[%s] Branch creation exception: %s", self.repo_name, e)
            return False

    def evaluate(self, context: Optional[Dict[str, Any]] = None) -> str:
        """Run one evaluation cycle. Checks external state and transitions.

        Called periodically by the AppCore poll loop.
        """
        if self._current_state in TERMINAL_STATUSES:
            return self._current_state

        # Refresh GitHub state
        branches = self.gh.get_release_branches() or {}
        tags = self.gh.get_tags() or {}
        gh_releases = self.gh.get_releases() or set()

        # Determine the version we're tracking
        version = self.version
        if not version:
            # Auto-detect from branches
            for v in branches:
                if not version or _parse_semver(v) > _parse_semver(version):
                    version = v
            if not version:
                return UNKNOWN
            self.version = version

        # ── Transition evaluation ───────────────────────────────

        # building → unit-test: CI stamp exists, branch SHA matches stamp
        if self._current_state == BUILDING:
            if self._check_ci_stamp(version):
                if not self._check_workflow_failed(version):
                    self._transition_to(UNIT_TEST)
                    self._create_intest_tag(version)
                    return self._current_state

        # unit-test → integration-test: vX.Y.Z-intest tag exists on GitHub
        if self._current_state == UNIT_TEST:
            if f"v{version}-intest" in tags:
                self._transition_to(INTEGRATION_TEST)
                return self._current_state

        # TODO: after tag vX.Y.Z-rc is created (e.g. by CI),
        #   integration-test → e2e-test
        #   (Requires the CI to create -rc tag after tests pass.)

        # e2e-test → releasing: manual promote (RC approved)
        if self._current_state == E2E_TEST:
            # Manual promotion is triggered via API, not detected here
            pass

        # releasing → released: GitHub Release exists
        if self._current_state == RELEASING:
            if f"v{version}" in gh_releases:
                self._transition_to(RELEASED)
                self._finalize_release(version)
                return self._current_state

        # released → done: branch merged into main
        if self._current_state == RELEASED:
            branch_name = f"release/{version}"
            if version in branches:
                branch_sha = branches[version]
                try:
                    compare = self.gh.compare("main", branch_name)
                    if compare and compare.get("ahead_by", 1) == 0:
                        self._transition_to(DONE)
                        return self._current_state
                except Exception as e:
                    logger.warning("[%s] Compare failed: %s", self.repo_name, e)

        return self._current_state

    def _transition_to(self, new_state: str) -> None:
        self._current_state = new_state
        self._persist()
        logger.info("[%s] → %s", self.repo_name, new_state)

    # ── Condition checks ──────────────────────────────────────────

    def _check_ci_stamp(self, version: str) -> bool:
        """Check if brinecrypt has a CI stamp for this version."""
        stamp = self.bc.read_resource("ci", f"brinecrypt-release-{version}")
        return stamp is not None

    def _check_workflow_failed(self, version: str) -> bool:
        """Check if any GitHub workflow runs failed for this version's SHA."""
        try:
            branch_name = f"release/{version}"
            branches = self.gh.get_release_branches() or {}
            sha = branches.get(version)
            if not sha:
                return False
            runs = self.gh.get_workflow_runs(sha, per_page=5)
            if runs:
                for run in runs:
                    if run.get("conclusion") == "failure":
                        self.failed = True
                        self._persist()
                        return True
        except Exception as e:
            logger.warning("[%s] Workflow check failed: %s", self.repo_name, e)
        return False

    # ── Actions ───────────────────────────────────────────────────

    def _create_intest_tag(self, version: str) -> None:
        """Create vX.Y.Z-intest tag on GitHub."""
        branches = self.gh.get_release_branches() or {}
        sha = branches.get(version)
        if sha:
            self.gh.create_tag(f"v{version}-intest", sha)
            logger.info("[%s] Created tag v%s-intest", self.repo_name, version)

    def promote_to_release(self) -> bool:
        """Manual promotion from e2e-test → releasing.

        Deletes vX.Y.Z-rc tag, creates vX.Y.Z tag at the same SHA.
        Called by API, not by poll loop.
        """
        if self._current_state not in (E2E_TEST, INTEGRATION_TEST):
            return False

        version = self.version
        tags = self.gh.get_tags() or {}

        # Find SHA from -rc or -intest tag
        sha = None
        for tag_name in (f"v{version}-rc", f"v{version}-intest"):
            if tag_name in tags:
                sha = tags[tag_name]
                self.gh.delete_tag(tag_name)
                break

        if not sha:
            branches = self.gh.get_release_branches() or {}
            sha = branches.get(version)

        if sha:
            self.gh.create_tag(f"v{version}", sha)
            self._transition_to(RELEASING)
            return True

        logger.warning("[%s] Could not find SHA for promote", self.repo_name)
        return False

    def merge_and_complete(self) -> bool:
        """Merge release branch into main. Transitions released → done.

        Called by API (POST merge), also auto-detected in poll loop.
        """
        if self._current_state != RELEASED:
            return False

        version = self.version
        branch_name = f"release/{version}"

        # GitHub merge
        try:
            result = self.gh.session.post(
                f"{self.gh.base_url}/merges",
                json={"base": "main", "head": branch_name},
                timeout=15,
            )
            if result.ok:
                self._transition_to(DONE)
                return True
            else:
                logger.warning(
                    "[%s] Merge failed: %s", self.repo_name, result.text,
                )
        except Exception as e:
            logger.error("[%s] Merge exception: %s", self.repo_name, e)

        return False

    def mark_stable(self, version: str) -> bool:
        """Tag a version as stable on GitHub and record in the store."""
        # Create/replace stable tag
        tags = self.gh.get_tags() or {}
        sha = tags.get(f"v{version}")
        if not sha:
            return False

        # Delete old stable tag if it exists
        if "stable" in tags:
            self.gh.delete_tag("stable")

        self.gh.create_tag("stable", sha)
        self.store.set_stable(self.repo_name, version)
        logger.info("[%s] Marked v%s as stable", self.repo_name, version)
        return True

    def _finalize_release(self, version: str) -> None:
        """Called when a release is detected. Writes broadcast data, cleans up."""
        branch_name = f"release/{version}"
        branches = self.gh.get_release_branches() or {}
        sha = branches.get(version)

        if sha:
            self.commit_hash = sha
            # Write broadcast data to brinecrypt
            self.bc.write_broadcast(
                repo_friendly=self.repo_name.replace("/", "-"),
                version=version,
                commit_hash=sha,
                registries=self.registries,
                build_timestamp=__import__("datetime").datetime.utcnow().isoformat() + "Z",
            )
            self._persist()

        logger.info("[%s] Release v%s finalized", self.repo_name, version)

    def stop(self, version: str) -> bool:
        """Stop a specific version build. Only works before release."""
        if self._current_state in (RELEASED, DONE):
            return False

        if self.version != version:
            return False

        # Clean up: delete release branch
        try:
            branch_name = f"release/{version}"
            # Delete branch via GitHub API
            self.gh.session.delete(
                f"{self.gh.base_url}/git/refs/heads/{branch_name}",
                timeout=10,
            )
        except Exception as e:
            logger.warning("[%s] Branch cleanup failed: %s", self.repo_name, e)

        self._current_state = FAILED
        self._persist()
        logger.info("[%s] Release v%s stopped", self.repo_name, version)
        return True