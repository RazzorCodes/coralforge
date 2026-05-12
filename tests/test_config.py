import os
import tempfile
import textwrap
import unittest

from src.config.app_config import AppConfig


class FakeBrinecrypt:
    def __init__(self, values):
        self.values = values

    def read_resource_raw(self, namespace, name):
        return self.values.get((namespace, name))


class AppConfigTests(unittest.TestCase):
    def setUp(self):
        AppConfig._instance = None

    def test_loads_repo_config_and_resolves_named_secrets(self):
        with tempfile.TemporaryDirectory() as tempdir:
            repo_dir = os.path.join(tempdir, "bm25lib")
            os.makedirs(os.path.join(repo_dir, ".github", "workflows"))
            with open(os.path.join(repo_dir, ".drone.yml"), "w", encoding="utf-8") as handle:
                handle.write("kind: pipeline\nname: ci\nsteps:\n  - name: build\n")

            config_path = os.path.join(repo_dir, ".coralforge.yml")
            with open(config_path, "w", encoding="utf-8") as handle:
                handle.write(
                    textwrap.dedent(
                        f"""
                        version: 1
                        repo:
                          name: bm25lib
                          owner: RazzorCodes
                          repo: bm25lib
                          workspace_path: {repo_dir}
                        providers:
                          drone:
                            kind: drone
                            endpoint: https://drone.example
                            auth:
                              token:
                                secretRef:
                                  namespace: coralforge
                                  name: bm25lib-drone
                                  key: token
                        secrets:
                          publish_token:
                            secretRef:
                              namespace: coralforge
                              name: bm25lib-publish
                              key: token
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

            bc = FakeBrinecrypt(
                {
                    ("coralforge", "bm25lib-drone"): '{"token": "drone-token"}',
                    ("coralforge", "bm25lib-publish"): '{"token": "publish-secret"}',
                }
            )
            config = AppConfig()
            config.load(bc_connector=bc, repo_config_paths=[config_path])

            repo = config.get_repo("bm25lib")
            self.assertIsNotNone(repo)
            self.assertEqual(repo.providers["drone"].config["token"], "drone-token")
            self.assertEqual(repo.metadata["resolved_secrets"]["publish_token"], "publish-secret")
            self.assertEqual(repo.run_types["ci"].provider_target["pipeline"], "ci")


if __name__ == "__main__":
    unittest.main()
