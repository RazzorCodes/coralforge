import os
import tempfile
import textwrap
import unittest

from src.orchestration.providers import read_drone_definitions, read_github_workflow_definitions


class ReaderTests(unittest.TestCase):
    def test_reads_github_actions_workflow_metadata(self):
        with tempfile.TemporaryDirectory() as tempdir:
            workflow_dir = os.path.join(tempdir, ".github", "workflows")
            os.makedirs(workflow_dir)
            with open(os.path.join(workflow_dir, "ci.yml"), "w", encoding="utf-8") as handle:
                handle.write(
                    textwrap.dedent(
                        """
                        name: CI
                        on:
                          workflow_dispatch:
                          push:
                            branches: [main]
                        jobs:
                          build:
                            runs-on: ubuntu-latest
                          test:
                            runs-on: ubuntu-latest
                        """
                    ).strip()
                )

            definitions = read_github_workflow_definitions(tempdir)
            self.assertIn("ci.yml", definitions)
            self.assertTrue(definitions["ci.yml"]["dispatchable"])
            self.assertEqual(definitions["ci.yml"]["jobs"], ["build", "test"])
            self.assertEqual(definitions["ci.yml"]["branches"], ["main"])

    def test_reads_drone_pipeline_metadata(self):
        with tempfile.TemporaryDirectory() as tempdir:
            with open(os.path.join(tempdir, ".drone.yml"), "w", encoding="utf-8") as handle:
                handle.write(
                    textwrap.dedent(
                        """
                        kind: pipeline
                        type: docker
                        name: bm25-ci
                        trigger:
                          branch:
                            include: [main, release/*]
                        steps:
                          - name: configure
                          - name: test
                        """
                    ).strip()
                )

            definitions = read_drone_definitions(tempdir)
            self.assertIn("bm25-ci", definitions)
            self.assertEqual(definitions["bm25-ci"]["steps"], ["configure", "test"])
            self.assertEqual(definitions["bm25-ci"]["branches"], ["main", "release/*"])


if __name__ == "__main__":
    unittest.main()
