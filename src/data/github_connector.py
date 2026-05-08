import logging
from typing import Dict, List, Set

import requests

logger = logging.getLogger("coralforge.github")


class GitHubConnector:
    """
    Handles interactions with the GitHub API for a specific repository.
    Designed to be instantiated per-repository for parallel operations.
    """

    def __init__(
        self,
        owner: str,
        repo: str,
        token: str,
        endpoint: str = "https://api.github.com",
    ):
        self.owner = owner
        self.repo = repo
        self.base_url = f"{endpoint.rstrip('/')}/repos/{owner}/{repo}"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def _log_response(self, response: requests.Response):
        """Logs response details without dropping the body."""
        if not response.ok:
            logger.error(
                f"GitHub API Error: {response.status_code} {response.reason} "
                f"for {response.request.method} {response.url}\n"
                f"Body: {response.text}"
            )
        else:
            logger.debug(
                f"GitHub API Success: {response.status_code} for {response.url}"
            )

    def get_release_branches(self) -> Dict[str, str]:
        """Returns dict of {version: sha} for all release/* branches."""
        url = f"{self.base_url}/branches"
        params = {"per_page": 100}

        try:
            r = self.session.get(url, params=params, timeout=10)
            self._log_response(r)
            r.raise_for_status()

            out = {}
            for b in r.json():
                name = b.get("name", "")
                if name.startswith("release/"):
                    version = name[len("release/") :]
                    out[version] = b.get("commit", {}).get("sha", "")
            return out
        except Exception as e:
            logger.error(f"Failed to fetch branches: {e}")
            return {}

    def get_tags(self) -> Dict[str, str]:
        """Returns dict of {tag_name: sha} for all tags."""
        url = f"{self.base_url}/git/refs/tags"

        try:
            r = self.session.get(url, timeout=10)
            if r.status_code == 404:
                return {}
            self._log_response(r)
            r.raise_for_status()

            out = {}
            for ref in r.json():
                name = ref.get("ref", "").replace("refs/tags/", "")
                sha = ref.get("object", {}).get("sha", "")
                out[name] = sha
            return out
        except Exception as e:
            logger.error(f"Failed to fetch tags: {e}")
            return {}

    def get_releases(self) -> Set[str]:
        """Returns set of tag names that have a GitHub Release."""
        url = f"{self.base_url}/releases"
        params = {"per_page": 100}

        try:
            r = self.session.get(url, params=params, timeout=10)
            self._log_response(r)
            r.raise_for_status()

            return {rel.get("tag_name", "") for rel in r.json()}
        except Exception as e:
            logger.error(f"Failed to fetch releases: {e}")
            return set()

    def compare(self, base: str, head: str) -> Dict:
        """Compares two commits/branches."""
        url = f"{self.base_url}/compare/{base}...{head}"

        try:
            r = self.session.get(url, timeout=10)
            self._log_response(r)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Failed to compare {base} and {head}: {e}")
            return {}

    def get_tags_for(self, sha: str) -> List[str]:
        all_tags = self.get_tags()
        return [tag for tag, tag_sha in all_tags.items() if tag_sha == sha]

    def delete_tag(self, tag_name: str) -> bool:
        """Deletes a tag."""
        url = f"{self.base_url}/git/refs/tags/{tag_name}"

        try:
            r = self.session.delete(url, timeout=10)
            self._log_response(r)
            if r.status_code == 404:
                return True
            return r.ok
        except Exception as e:
            logger.error(f"Failed to delete tag {tag_name}: {e}")
            return False

    def create_tag(self, tag_name: str, sha: str) -> bool:
        """Creates a lightweight tag."""
        url = f"{self.base_url}/git/refs"
        payload = {"ref": f"refs/tags/{tag_name}", "sha": sha}

        try:
            r = self.session.post(url, json=payload, timeout=10)
            self._log_response(r)
            return r.ok
        except Exception as e:
            logger.error(f"Failed to create tag {tag_name} at {sha}: {e}")
            return False

    def get_workflow_runs(self, sha: str, per_page: int = 10) -> List[Dict]:
        """Returns workflow runs for a specific SHA."""
        url = f"{self.base_url}/actions/runs"
        params = {"head_sha": sha, "per_page": per_page}

        try:
            r = self.session.get(url, params=params, timeout=10)
            self._log_response(r)
            r.raise_for_status()
            return r.json().get("workflow_runs", [])
        except Exception as e:
            logger.error(f"Failed to fetch workflow runs for {sha}: {e}")
            return []
