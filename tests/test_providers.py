import unittest

from src.orchestration.models import ProviderBinding, RepoDefinition, RunDefinition
from src.orchestration.providers import DroneConnector


class _FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text
        self.ok = 200 <= status_code < 300

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.post_calls = []
        self.get_calls = []

    def post(self, url, json=None, params=None, timeout=None):
        self.post_calls.append({"url": url, "json": json, "params": params, "timeout": timeout})
        # Simulate Drone returning `null` payload for build trigger response.
        return _FakeResponse(None, status_code=200, text="null")

    def get(self, url, params=None, timeout=None):
        self.get_calls.append({"url": url, "params": params, "timeout": timeout})
        # Simulate list-builds fallback with a newly queued build on requested branch.
        return _FakeResponse(
            [
                {
                    "number": 123,
                    "status": "pending",
                    "link": "https://drone.example/acme/sample/123",
                    "target": "main",
                    "event": "custom",
                    "created": 1715700000,
                    "started": 1715700005,
                    "stopped": 0,
                    "after": "deadbeef",
                }
            ]
        )


class DroneConnectorTests(unittest.TestCase):
    def test_trigger_run_handles_null_trigger_payload_with_fallback(self):
        binding = ProviderBinding(
            name="drone",
            kind="drone",
            config={
                "endpoint": "https://drone.example",
                "owner": "acme",
                "repo": "sample",
            },
        )
        connector = DroneConnector(binding)
        connector.session = _FakeSession()

        repo = RepoDefinition(
            name="sample",
            owner="acme",
            repo="sample",
            workspace_path="/tmp/sample",
            config_path="/tmp/sample/.coralforge.yml",
            config_version=1,
            metadata={"default_ref": "main"},
        )
        run_definition = RunDefinition(
            name="ci",
            default_provider="drone",
            provider_target={"pipeline": "bm25-ci"},
        )

        snapshot = connector.trigger_run(repo, run_definition, ref="main")

        self.assertEqual(snapshot.provider_run_id, "123")
        self.assertEqual(snapshot.status, "queued")
        self.assertEqual(snapshot.ref, "main")
        self.assertEqual(snapshot.metadata.get("pipeline"), "bm25-ci")

        call = connector.session.post_calls[0]
        self.assertIsNone(call["json"])
        self.assertEqual(call["params"], {"branch": "main"})


if __name__ == "__main__":
    unittest.main()
