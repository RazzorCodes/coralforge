import unittest

from src.orchestration.models import ProviderBinding, ProviderRunSnapshot, RepoDefinition, RunDefinition
from src.orchestration.providers import DroneConnector, GitHubActionsConnector, JenkinsConnector


class _FakeResponse:
    def __init__(self, payload, status_code=200, text="", headers=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text
        self.ok = 200 <= status_code < 300
        self.headers = headers or {}

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.post_calls = []
        self.get_calls = []

    def post(self, url, json=None, params=None, headers=None, timeout=None):
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


class GitHubActionsConnectorTests(unittest.TestCase):
    def _build_connector(self):
        binding = ProviderBinding(
            name="github",
            kind="github-actions",
            config={
                "endpoint": "https://api.github.com",
                "owner": "acme",
                "repo": "sample",
                "token": "token",
            },
        )
        return GitHubActionsConnector(binding)

    def _build_repo(self):
        return RepoDefinition(
            name="sample",
            owner="acme",
            repo="sample",
            workspace_path="/tmp/sample",
            config_path="/tmp/sample/.coralforge.yml",
            config_version=1,
            metadata={"default_ref": "main"},
        )

    def test_trigger_run_uses_tag_flow_and_never_dispatches(self):
        connector = self._build_connector()
        repo = self._build_repo()
        run_definition = RunDefinition(
            name="ci",
            default_provider="github",
            provider_target={"workflow": "ci.yml"},
        )

        connector.discover_definitions = lambda _repo: {  # type: ignore[method-assign]
            "ci.yml": {"tag_patterns": ["build-*"]}
        }

        calls = {"tag": 0}
        expected_snapshot = ProviderRunSnapshot(
            status="queued",
            provider_status="tagged",
            ref="main",
            metadata={"workflow": "ci.yml", "tag": "build-1.2.3", "version": "1.2.3"},
        )

        def fake_trigger_via_tag(workflow, workflow_def, ref_name, inputs):
            calls["tag"] += 1
            self.assertEqual(workflow, "ci.yml")
            self.assertEqual(ref_name, "main")
            self.assertEqual(workflow_def["tag_patterns"], ["build-*"])
            return expected_snapshot

        connector._trigger_via_tag = fake_trigger_via_tag  # type: ignore[method-assign]
        run_snapshot = connector.trigger_run(
            repo,
            run_definition,
            ref="main",
            inputs={"bump": "patch"},
        )

        self.assertEqual(calls["tag"], 1)
        self.assertEqual(run_snapshot.provider_status, "tagged")
        self.assertEqual(run_snapshot.ref, "main")

    def test_trigger_run_errors_for_non_tag_workflow(self):
        connector = self._build_connector()
        repo = self._build_repo()
        run_definition = RunDefinition(
            name="ci",
            default_provider="github",
            provider_target={"workflow": "ci.yml"},
        )
        connector.discover_definitions = lambda _repo: {  # type: ignore[method-assign]
            "ci.yml": {"dispatchable": True, "tag_patterns": []}
        }

        with self.assertRaisesRegex(ValueError, "not tag-triggerable"):
            connector.trigger_run(repo, run_definition, ref="main")


class _FakeJenkinsSession:
    def __init__(self):
        self.posts = []
        self.gets = []

    def post(self, url, params=None, headers=None, timeout=None):
        self.posts.append({"url": url, "params": params, "headers": headers or {}, "timeout": timeout})
        return _FakeResponse(
            None,
            status_code=201,
            headers={"Location": "http://jenkins.example/queue/item/321/"},
        )

    def get(self, url, params=None, timeout=None):
        self.gets.append({"url": url, "params": params, "timeout": timeout})
        if url.endswith("/crumbIssuer/api/json"):
            return _FakeResponse({"crumbRequestField": "Jenkins-Crumb", "crumb": "crumb-token"})
        if "/queue/item/321/api/json" in url:
            return _FakeResponse({"executable": {"number": 99}})
        if "/job/jackfield-pipeline/99/wfapi/describe" in url:
            return _FakeResponse(
                {
                    "stages": [
                        {"name": "ci", "status": "SUCCESS", "startTimeMillis": 1715700000000, "endTimeMillis": 1715700060000},
                        {"name": "release", "status": "IN_PROGRESS", "startTimeMillis": 1715700060000, "endTimeMillis": None},
                    ]
                }
            )
        if "/job/jackfield-pipeline/99/api/json" in url:
            return _FakeResponse(
                {
                    "number": 99,
                    "url": "http://jenkins.example/job/jackfield-pipeline/99/",
                    "result": None,
                    "building": True,
                    "timestamp": 1715700000000,
                    "duration": 60000,
                    "actions": [
                        {
                            "parameters": [
                                {"name": "RUN_TYPE", "value": "release"},
                                {"name": "REF_NAME", "value": "main"},
                                {"name": "VERSION", "value": "1.2.3"},
                            ]
                        }
                    ],
                }
            )
        if url.endswith("/job/jackfield-pipeline/api/json"):
            return _FakeResponse(
                {
                    "name": "jackfield-pipeline",
                    "fullName": "jackfield-pipeline",
                    "buildable": True,
                    "lastBuild": {"number": 99},
                    "builds": [{"number": 99, "result": "SUCCESS", "building": False, "url": "http://jenkins.example/job/jackfield-pipeline/99/", "timestamp": 1715700000000, "duration": 60000}],
                }
            )
        if url.endswith("/job/jackfield-pipeline/99/consoleText"):
            return _FakeResponse(None, text="jenkins console output")
        return _FakeResponse({}, status_code=404)


class JenkinsConnectorTests(unittest.TestCase):
    def _build_repo(self):
        return RepoDefinition(
            name="jackfield",
            owner="RazzorCodes",
            repo="jackfield",
            workspace_path="/tmp/jackfield",
            config_path="/tmp/jackfield/.coralforge.yml",
            config_version=1,
            metadata={"default_ref": "main"},
            run_types={
                "ci": RunDefinition(name="ci", default_provider="jenkins", provider_target={"job": "jackfield-pipeline"})
            },
        )

    def test_trigger_run_uses_build_with_parameters_and_queue_id(self):
        binding = ProviderBinding(
            name="jenkins",
            kind="jenkins",
            config={"endpoint": "http://jenkins.example", "job": "jackfield-pipeline", "user": "u", "token": "t"},
        )
        connector = JenkinsConnector(binding)
        connector.session = _FakeJenkinsSession()

        run_def = RunDefinition(name="release", default_provider="jenkins", provider_target={"job": "jackfield-pipeline"})
        snapshot = connector.trigger_run(self._build_repo(), run_def, ref="main", inputs={"actor": "tester", "version": "1.2.3"})
        self.assertEqual(snapshot.status, "queued")
        self.assertEqual(snapshot.provider_run_id, "queue:321")
        self.assertEqual(snapshot.metadata.get("job"), "jackfield-pipeline")

        post_call = connector.session.posts[0]
        self.assertIn("/job/jackfield-pipeline/buildWithParameters", post_call["url"])
        self.assertEqual(post_call["params"]["RUN_TYPE"], "release")
        self.assertEqual(post_call["params"]["VERSION"], "1.2.3")

    def test_get_run_and_logs_parse_jenkins_payload(self):
        binding = ProviderBinding(
            name="jenkins",
            kind="jenkins",
            config={"endpoint": "http://jenkins.example", "job": "jackfield-pipeline", "user": "u", "token": "t"},
        )
        connector = JenkinsConnector(binding)
        connector.session = _FakeJenkinsSession()

        repo = self._build_repo()
        snapshot = connector.get_run(repo, "queue:321")
        self.assertEqual(snapshot.provider_run_id, "99")
        self.assertEqual(snapshot.status, "running")
        self.assertEqual(snapshot.metadata.get("job"), "jackfield-pipeline")
        self.assertEqual(snapshot.metadata.get("version"), "1.2.3")
        self.assertEqual([stage.name for stage in snapshot.stages], ["ci", "release"])

        logs = connector.get_logs(repo, "99")
        self.assertIn("console", logs)


if __name__ == "__main__":
    unittest.main()
