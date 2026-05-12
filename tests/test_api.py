import os
import tempfile
import textwrap
import unittest

from src.app import create_app
from src.config.app_config import AppConfig


class ApiTests(unittest.TestCase):
    def setUp(self):
        AppConfig._instance = None

    def test_runs_endpoints_round_trip(self):
        with tempfile.TemporaryDirectory() as tempdir:
            repo_dir = os.path.join(tempdir, "repo")
            os.makedirs(repo_dir)
            with open(os.path.join(repo_dir, ".drone.yml"), "w", encoding="utf-8") as handle:
                handle.write("kind: pipeline\nname: ci\nsteps:\n  - name: build\n")

            config_path = os.path.join(repo_dir, ".coralforge.yml")
            with open(config_path, "w", encoding="utf-8") as handle:
                handle.write(
                    textwrap.dedent(
                        f"""
                        version: 1
                        repo:
                          name: api-sample
                          owner: acme
                          repo: api-sample
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
                              - name: build
                                provider: drone
                                target:
                                  pipeline: ci
                                  step: build
                        """
                    ).strip()
                )

            os.environ["CORALFORGE_REPO_CONFIGS"] = config_path
            app = create_app()
            client = app.test_client()

            repos_response = client.get("/api/v1/repos")
            self.assertEqual(repos_response.status_code, 200)
            self.assertEqual(repos_response.get_json()["repos"][0]["name"], "api-sample")

            trigger_response = client.post(
                "/api/v1/runs",
                json={"target": "api-sample", "run_type": "ci", "ref": "main"},
            )
            self.assertEqual(trigger_response.status_code, 202)
            run_id = trigger_response.get_json()["run_id"]

            run_response = client.get(f"/api/v1/runs/{run_id}")
            self.assertEqual(run_response.status_code, 200)
            self.assertEqual(run_response.get_json()["provider"], "drone")

            provider_response = client.get(f"/api/v1/runs/{run_id}/provider")
            self.assertEqual(provider_response.status_code, 200)
            self.assertEqual(provider_response.get_json()["provider_kind"], "drone")

            logs_response = client.get(f"/api/v1/runs/{run_id}/logs")
            self.assertEqual(logs_response.status_code, 200)
            self.assertIn("simulated", logs_response.get_json()["logs"])

            del os.environ["CORALFORGE_REPO_CONFIGS"]


if __name__ == "__main__":
    unittest.main()
