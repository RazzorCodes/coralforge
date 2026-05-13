import os
import tempfile
import textwrap
import unittest

from src.config.app_config import AppConfig
from src.data.data_connector import InMemoryStateStore
from src.orchestration.models import CiStage, CiStep, ProviderRunSnapshot
from src.orchestration.service import OrchestrationService


class ServiceTests(unittest.TestCase):
    def setUp(self):
        AppConfig._instance = None

    def _load_config(self):
        tempdir = tempfile.TemporaryDirectory()
        repo_dir = os.path.join(tempdir.name, "repo")
        os.makedirs(repo_dir)
        with open(os.path.join(repo_dir, ".drone.yml"), "w", encoding="utf-8") as handle:
            handle.write(
                textwrap.dedent(
                    """
                    kind: pipeline
                    name: ci
                    steps:
                      - name: configure
                      - name: build
                      - name: test
                      - name: lint
                    """
                ).strip()
            )
        config_path = os.path.join(repo_dir, ".coralforge.yml")
        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write(
                textwrap.dedent(
                    f"""
                    version: 1
                    repo:
                      name: sample
                      owner: acme
                      repo: sample
                      workspace_path: {repo_dir}
                    providers:
                      drone:
                        kind: drone
                        endpoint: https://drone.example
                        simulate: true
                    run_types:
                      ci:
                        default_provider: drone
                        provider_target:
                          pipeline: ci
                        stages:
                          - name: configure
                            provider: drone
                            target:
                              pipeline: ci
                              step: configure
                          - name: build
                            provider: drone
                            target:
                              pipeline: ci
                              step: build
                          - name: test
                            provider: drone
                            target:
                              pipeline: ci
                              step: test
                          - name: lint
                            provider: drone
                            target:
                              pipeline: ci
                              step: lint
                    metadata:
                      default_ref: main
                    """
                ).strip()
            )
        config = AppConfig()
        config.load(repo_config_paths=[config_path])
        return tempdir, config

    def test_trigger_run_persists_normalized_run(self):
        tempdir, config = self._load_config()
        self.addCleanup(tempdir.cleanup)
        store = InMemoryStateStore()
        service = OrchestrationService(config, store)
        service.validate_repositories()

        run = service.trigger_run("sample", "ci", ref="main", actor="tester")
        self.assertEqual(run.status, "queued")
        self.assertEqual(run.provider, "drone")
        self.assertEqual(run.current_stage, "configure")
        self.assertEqual(len(store.list_runs()), 1)

        logs = service.get_logs(run.run_id)
        self.assertIn("simulated", logs["logs"])

    def test_list_runs_imports_recent_drone_runs(self):
        tempdir, config = self._load_config()
        self.addCleanup(tempdir.cleanup)
        store = InMemoryStateStore()
        service = OrchestrationService(config, store)
        service.validate_repositories()

        repo = config.get_repo("sample")

        class FakeDroneConnector:
            def discover_definitions(self, _repo):
                return {"ci": {"steps": ["configure", "build", "test", "lint"]}}

            def list_recent_runs(self, _repo, limit=20):
                if limit != 20:
                    raise AssertionError(f"unexpected limit {limit}")
                return [
                    ProviderRunSnapshot(
                        status="passed",
                        provider_status="success",
                        provider_run_id="42",
                        created_at="2026-05-13T10:00:00Z",
                        started_at="2026-05-13T10:00:05Z",
                        finished_at="2026-05-13T10:03:00Z",
                        ref="main",
                        sha="deadbeef",
                        metadata={"pipeline": "ci", "event": "push"},
                        stages=[
                            CiStage(
                                name="pipeline",
                                status="passed",
                                provider="drone",
                                steps=[
                                    CiStep(name="configure", status="passed"),
                                    CiStep(name="build", status="passed"),
                                    CiStep(name="test", status="passed"),
                                    CiStep(name="lint", status="passed"),
                                ],
                            )
                        ],
                    )
                ]

            def get_run(self, _repo, provider_run_id):
                raise AssertionError(f"unexpected get_run({provider_run_id}) during import")

            def get_logs(self, _repo, provider_run_id):
                raise AssertionError(f"unexpected get_logs({provider_run_id}) during import")

            def trigger_run(self, _repo, run_definition, ref, inputs=None):
                raise AssertionError(f"unexpected trigger_run({run_definition.name}, {ref}, {inputs})")

        service._connectors[(repo.name, "drone")] = FakeDroneConnector()

        runs = service.list_runs(repo_name="sample", limit=20)

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["provider_run_id"], "42")
        self.assertEqual(runs[0]["status"], "passed")
        self.assertEqual(runs[0]["actor"], "provider-import")
        self.assertEqual(
            [stage["name"] for stage in runs[0]["stages"]],
            ["configure", "build", "test", "lint"],
        )
        self.assertEqual(
            [stage["status"] for stage in runs[0]["stages"]],
            ["passed", "passed", "passed", "passed"],
        )

    def test_list_runs_normalizes_legacy_numeric_timestamps(self):
        tempdir, config = self._load_config()
        self.addCleanup(tempdir.cleanup)
        store = InMemoryStateStore()
        service = OrchestrationService(config, store)
        service.validate_repositories()

        run = service.trigger_run("sample", "ci", ref="main", actor="tester")
        raw = store._runs[run.run_id]
        raw["created_at"] = 1715700000
        raw["started_at"] = 1715700010
        raw["finished_at"] = 1715700100
        for stage in raw.get("stages") or []:
            stage["started_at"] = 1715700010
            stage["finished_at"] = 1715700100
            for step in stage.get("steps") or []:
                step["started_at"] = 1715700010
                step["finished_at"] = 1715700100

        runs = service.list_runs(repo_name="sample", limit=20)
        self.assertEqual(len(runs), 1)
        self.assertTrue(str(runs[0]["created_at"]).startswith("2024-"))
        self.assertTrue("T" in runs[0]["started_at"])
        self.assertTrue("T" in runs[0]["finished_at"])

    def test_get_run_refresh_failure_returns_cached_run(self):
        tempdir, config = self._load_config()
        self.addCleanup(tempdir.cleanup)
        store = InMemoryStateStore()
        service = OrchestrationService(config, store)
        service.validate_repositories()

        run = service.trigger_run("sample", "ci", ref="main", actor="tester")
        repo = config.get_repo("sample")

        class BrokenRefreshConnector:
            def discover_definitions(self, _repo):
                return {"ci": {"steps": ["configure", "build", "test", "lint"]}}

            def list_recent_runs(self, _repo, limit=20):
                return []

            def get_run(self, _repo, provider_run_id):
                raise RuntimeError(f"drone timeout for {provider_run_id}")

            def get_logs(self, _repo, provider_run_id):
                return {"x": provider_run_id}

            def trigger_run(self, _repo, run_definition, ref, inputs=None):
                raise AssertionError(f"unexpected trigger_run({run_definition.name}, {ref}, {inputs})")

        service._connectors[(repo.name, "drone")] = BrokenRefreshConnector()

        payload = service.get_run(run.run_id, refresh=True)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["run_id"], run.run_id)
        self.assertEqual(payload["provider_run_id"], run.provider_run_id)
        self.assertEqual(payload["status"], "queued")

    def test_validate_repositories_reports_missing_jenkins_stage_mapping(self):
        with tempfile.TemporaryDirectory() as tempdir:
            repo_dir = os.path.join(tempdir, "repo")
            os.makedirs(repo_dir)
            config_path = os.path.join(repo_dir, ".coralforge.yml")
            with open(config_path, "w", encoding="utf-8") as handle:
                handle.write(
                    textwrap.dedent(
                        f"""
                        version: 1
                        repo:
                          name: sample-jenkins
                          owner: acme
                          repo: sample-jenkins
                          workspace_path: {repo_dir}
                        providers:
                          jenkins:
                            kind: jenkins
                            endpoint: http://jenkins.example
                            job: jackfield-pipeline
                            simulate: true
                        run_types:
                          ci:
                            default_provider: jenkins
                            provider_target:
                              job: jackfield-pipeline
                            stages:
                              - name: ci
                                provider: jenkins
                                target:
                                  job: jackfield-pipeline
                                  stage: ci
                              - name: release
                                provider: jenkins
                                target:
                                  job: jackfield-pipeline
                                  stage: release
                        """
                    ).strip()
                )
            config = AppConfig()
            config.load(repo_config_paths=[config_path])
            repo = config.get_repo("sample-jenkins")
            self.assertIsNotNone(repo)

            service = OrchestrationService(config, InMemoryStateStore())

            class FakeJenkinsConnector:
                def discover_definitions(self, _repo):
                    return {"jackfield-pipeline": {"stages": ["ci"]}}

            service._connectors[(repo.name, "jenkins")] = FakeJenkinsConnector()
            service.validate_repositories()

            self.assertTrue(
                any("missing Jenkins stage 'release'" in error for error in repo.validation_errors)
            )


if __name__ == "__main__":
    unittest.main()
