"""Python client for the Brinecrypt REST API.

Used only for:
- Reading secrets (DB creds, repo tokens, config)
- Writing final broadcast data (ci-open/<repo-friendly>-v<semver>)

Auth: Bearer token from projected SA token file or env var.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("coralforge.brinecrypt")

_BC_TOKEN_FILE = "/var/run/secrets/brinecrypt.io/serviceaccount/token"


class BrinecryptConnector:
    """Thin wrapper around the Brinecrypt REST API."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )
        self._load_token()

    def _load_token(self) -> None:
        """Load SA token from projected volume, env fallback, or K8s default."""
        token = os.environ.get("BRINECRYPT_TOKEN")
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
            return

        for path in (_BC_TOKEN_FILE, "/var/run/secrets/kubernetes.io/serviceaccount/token"):
            try:
                with open(path) as f:
                    self.session.headers["Authorization"] = f"Bearer {f.read().strip()}"
                    return
            except OSError:
                continue

        logger.warning("No brinecrypt token found — requests will be unauthenticated")

    def _put(self, path: str, body: dict) -> Optional[dict]:
        url = f"{self.base_url}{path}"
        try:
            r = self.session.put(url, json=body, timeout=10)
            if r.status_code in (200, 201):
                return r.json()
            logger.error(f"PUT {url} -> {r.status_code}: {r.text}")
            return None
        except requests.RequestException as e:
            logger.error(f"PUT {url} failed: {e}")
            return None

    def _post(self, path: str, body: Optional[dict] = None) -> Optional[dict]:
        url = f"{self.base_url}{path}"
        try:
            r = self.session.post(url, json=body or {}, timeout=10)
            if r.status_code == 200:
                return r.json()
            logger.error(f"POST {url} -> {r.status_code}: {r.text}")
            return None
        except requests.RequestException as e:
            logger.error(f"POST {url} failed: {e}")
            return None

    def _delete(self, path: str, body: dict) -> bool:
        url = f"{self.base_url}{path}"
        try:
            r = self.session.delete(url, json=body, timeout=10)
            if r.status_code in (200, 204):
                return True
            logger.error(f"DELETE {url} -> {r.status_code}: {r.text}")
            return False
        except requests.RequestException as e:
            logger.error(f"DELETE {url} failed: {e}")
            return False

    # ── Resource CRUD ──────────────────────────────────────────────

    def _extract_value(self, result: dict) -> Optional[str]:
        """Extract the decrypted plaintext from a Brinecrypt resource response.

        Brinecrypt returns value as {"data": "<plaintext>", "uuid": ..., ...}.
        """
        raw = result.get("value")
        if isinstance(raw, dict):
            return raw.get("data")
        return raw

    def read_resource(self, namespace: str, name: str) -> Optional[Dict[str, Any]]:
        """Read a resource by namespace and name. Returns parsed JSON value."""
        body = {"namespace": namespace, "name": name}
        result = self._post("/api/v1/resource?op=query", body)
        if result is None:
            return None

        raw = self._extract_value(result)
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {"value": raw}
        return raw

    def read_resource_raw(self, namespace: str, name: str) -> Optional[str]:
        """Read a resource and return the raw value string."""
        body = {"namespace": namespace, "name": name}
        result = self._post("/api/v1/resource?op=query", body)
        if result is None:
            return None
        return self._extract_value(result)

    def write_resource(
        self, namespace: str, name: str, value: Any,
        resource_type: str = "cleartext",
    ) -> bool:
        """Create or update a resource."""
        if not isinstance(value, str):
            value = json.dumps(value)
        body = {
            "namespace": namespace,
            "name": name,
            "type": resource_type,
            "value": value,
        }
        return self._put("/api/v1/resource", body) is not None

    def delete_resource(self, namespace: str, name: str) -> bool:
        """Delete a resource."""
        body = {"namespace": namespace, "name": name}
        return self._delete("/api/v1/resource", body)

    # ── Namespace operations ───────────────────────────────────────

    def list_namespace(self, namespace: str) -> Optional[List[Dict[str, Any]]]:
        """List all resources in a namespace."""
        body = {"namespace": namespace}
        result = self._post("/api/v1/namespace?op=query", body)
        if result is None:
            return None
        return result.get("resources", [])

    def namespace_exists(self, namespace: str) -> bool:
        """Check if a namespace exists (list returns non-empty or empty list vs error)."""
        result = self.list_namespace(namespace)
        return result is not None

    # ── Convenience for broadcast data ─────────────────────────────

    def write_broadcast(
        self, repo_friendly: str, version: str,
        commit_hash: str, registries: List[str],
        build_timestamp: str,
    ) -> bool:
        """Write final release data to ci-open/<repo>-<version>.

        This is the only place brinecrypt stores release data — used for
        out-of-band broadcasting, not for internal state.
        """
        namespace = "ci-open"
        name = f"{repo_friendly}-v{version}"
        value = {
            "commit_hash": commit_hash,
            "registries": registries,
            "build_timestamp": build_timestamp,
            "version": version,
        }
        return self.write_resource(namespace, name, value)

    # ── Health ─────────────────────────────────────────────────────

    def health(self) -> bool:
        """Quick connectivity check."""
        try:
            r = self.session.get(f"{self.base_url}/healthz", timeout=5)
            return r.ok
        except requests.RequestException:
            return False