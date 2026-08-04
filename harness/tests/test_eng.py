from __future__ import annotations

import hashlib
import inspect
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from harness import eng


class HarnessTests(unittest.TestCase):
    def test_public_command_surface_is_limited(self) -> None:
        parser = eng.build_parser()
        self.assertEqual(parser.parse_args(["doctor"]).command, "doctor")
        self.assertIsNone(parser.parse_args(["setup"]).repo)
        parsed = parser.parse_args(["check", "quick", "--repo", "backend"])
        self.assertEqual((parsed.command, parsed.level, parsed.repo, parsed.cross), ("check", "quick", "backend", False))
        parsed = parser.parse_args(["check", "quick", "--repo", "customs-data-platform"])
        self.assertEqual(
            (parsed.command, parsed.level, parsed.repo, parsed.cross),
            ("check", "quick", "customs-data-platform", False),
        )
        parsed = parser.parse_args(["check", "contract", "--repo", "frontend", "--cross"])
        self.assertEqual((parsed.command, parsed.level, parsed.repo, parsed.cross), ("check", "contract", "frontend", True))
        parsed = parser.parse_args(["snapshot", "verify"])
        self.assertEqual((parsed.command, parsed.snapshot_command), ("snapshot", "verify"))
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["uat", "smoke"])
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["check", "candidate", "--repo", "backend"])

    def test_normalizes_repository_selectors(self) -> None:
        self.assertEqual(eng._repository_name("data_platform"), "data_platform")
        self.assertEqual(eng._repository_name("customs-data-platform"), "data_platform")
        self.assertIsNone(eng._repository_name(None))
        with self.assertRaises(eng.HarnessError):
            eng._repository_name("unrelated-repository")

    def test_normalizes_https_and_ssh_origins(self) -> None:
        expected = "audit-ai-agent/customs-backend"
        self.assertEqual(
            eng.normalize_github_origin("https://github.com/Audit-ai-agent/customs-backend.git"), expected
        )
        self.assertEqual(
            eng.normalize_github_origin("git@github.com:Audit-ai-agent/customs-backend.git"), expected
        )
        self.assertEqual(
            eng.normalize_github_origin("ssh://git@github.com/Audit-ai-agent/customs-backend"), expected
        )
        with self.assertRaises(eng.HarnessError):
            eng.normalize_github_origin("https://example.invalid/Audit-ai-agent/customs-backend.git")
        with self.assertRaises(eng.HarnessError):
            eng.normalize_github_origin("https://token@github.com/Audit-ai-agent/customs-backend.git")

    def test_snapshot_config_has_no_personal_upstream_requirement(self) -> None:
        config = eng._workspace_config()
        self.assertNotIn("expected_upstream", config["repos"]["data_platform"])
        self.assertNotIn("expected_upstream_push", config["repos"]["data_platform"])

    def test_tooling_rejects_node_below_the_shared_minimum(self) -> None:
        tooling = {
            "tooling": {
                "python": f"{sys.version_info.major}.{sys.version_info.minor}",
                "git_minimum": "2.40",
                "node_major": 22,
                "node_minimum": "22.22.0",
                "npm_minimum": "10.9.8",
            }
        }

        def versions(command: tuple[str, ...]) -> str:
            return {
                ("git", "--version"): "git version 2.49.0",
                ("node", "--version"): "v22.23.1",
                ("npm", "--version"): "10.9.8",
                ("uv", "--version"): "uv 0.11.28",
            }[command]

        with (
            patch.object(eng, "_workspace_config", return_value=tooling),
            patch.object(eng, "_first_line", side_effect=versions),
        ):
            self.assertEqual(eng._tool_versions()["node"], "v22.23.1")

        with (
            patch.object(eng, "_workspace_config", return_value=tooling),
            patch.object(
                eng,
                "_first_line",
                side_effect=lambda command: "v22.15.0"
                if command == ("node", "--version")
                else versions(command),
            ),
        ):
            with self.assertRaisesRegex(eng.HarnessError, "22.22.0"):
                eng._tool_versions()

        with (
            patch.object(eng, "_workspace_config", return_value=tooling),
            patch.object(
                eng,
                "_first_line",
                side_effect=lambda command: "10.9.7"
                if command == ("npm", "--version")
                else versions(command),
            ),
        ):
            with self.assertRaisesRegex(eng.HarnessError, "npm 10.9.8"):
                eng._tool_versions()

    def test_snapshot_config_uses_main_and_inventory_digests(self) -> None:
        snapshot = eng._load_toml("project-snapshot.toml")
        self.assertEqual(snapshot["schema_version"], "customs-project-snapshot/v2")
        self.assertEqual(snapshot["inventory"], "git ls-files -s -z")
        for record in snapshot["repositories"].values():
            self.assertEqual(record["branch"], "main")
            self.assertRegex(record["tracked_files_sha256"], r"^[0-9a-f]{64}$")

    def test_snapshot_treats_commit_as_provenance_not_the_gate(self) -> None:
        identity = "audit-ai-agent/customs-backend"
        records = {
            name: {
                "branch": "main",
                "repository": identity,
                "revision": "a" * 40,
                "tracked_files_sha256": "b" * 64,
            }
            for name in eng.REPOSITORIES
        }
        definitions = {name: {"repository": "Audit-ai-agent/customs-backend"} for name in eng.REPOSITORIES}
        state = {"branch": "main", "dirty": False, "revision": "c" * 40}
        with (
            patch.object(eng, "_snapshot_definitions", return_value=records),
            patch.object(eng, "_definitions", return_value=definitions),
            patch.object(eng, "_repo", return_value=Path(".")),
            patch.object(eng, "_repo_state", return_value=state),
            patch.object(eng, "_tracked_files_sha256", return_value="b" * 64),
        ):
            result = eng.snapshot_verify()
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["repositories"]["frontend"]["revision_matches_provenance"])

    def test_check_never_installs_or_cleans_dependencies(self) -> None:
        implementation = inspect.getsource(eng.check)
        self.assertNotIn("_setup_", implementation)
        self.assertNotIn("rmtree", implementation)
        self.assertNotIn("unlink", implementation)

    def test_cross_contract_is_limited_to_contract_checks(self) -> None:
        with self.assertRaisesRegex(eng.HarnessError, "only for a contract"):
            eng.check("quick", "frontend", cross=True)

    def test_cross_contract_requires_an_explicit_provider_mapping(self) -> None:
        with self.assertRaisesRegex(eng.HarnessError, "no configured cross-repository"):
            eng._cross_contract_files("data_platform", Path("."))

    def test_cross_contract_compares_exact_consumer_and_provider_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            consumer = root / "consumer"
            provider = root / "provider"
            (consumer / "contracts").mkdir(parents=True)
            (provider / "schemas").mkdir(parents=True)
            (consumer / "contracts" / "api.json").write_bytes(b'{"version":3}\n')
            (provider / "schemas" / "api.json").write_bytes(b'{"version":3}\n')
            definitions = {
                "frontend": {
                    "cross_contract": {
                        "files": [{
                            "provider": "backend",
                            "provider_path": "schemas/api.json",
                            "consumer_path": "contracts/api.json",
                        }]
                    }
                }
            }
            with (
                patch.object(eng, "_definitions", return_value=definitions),
                patch.object(eng, "_repo", side_effect=lambda name: provider if name == "backend" else consumer),
                patch.object(eng, "_assert_repository_origin", return_value="audit-ai-agent/customs-backend"),
            ):
                result = eng._cross_contract_files("frontend", consumer)
                self.assertEqual(result[0]["provider"], "backend")
                (consumer / "contracts" / "api.json").write_bytes(b'{"version":4}\n')
                with self.assertRaisesRegex(eng.HarnessError, "byte drift"):
                    eng._cross_contract_files("frontend", consumer)

    def test_frontend_setup_uses_the_checked_in_lock_without_ci_cleanup(self) -> None:
        implementation = inspect.getsource(eng._setup_frontend)
        self.assertIn('"--package-lock=true"', implementation)
        self.assertNotIn('"--package-lock=false"', implementation)
        self.assertNotIn('["npm", "ci"', implementation)

    def test_data_default_setup_does_not_pull_store_or_model_extras(self) -> None:
        setup = eng._definitions()["data_platform"]["setup"]
        self.assertEqual(setup, [["uv", "sync", "--locked", "--group", "dev"]])
        for command in eng._definitions()["data_platform"]["quick"] + eng._definitions()["data_platform"]["contract"]:
            self.assertIn("--no-sync", command)

    def test_agent_quick_check_includes_its_small_unit_suite(self) -> None:
        quick = eng._definitions()["ai_agent"]["quick"]
        self.assertIn(["python", "-m", "pytest", "-q"], quick)

    def test_tracked_inventory_digest_is_raw_git_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            (repository / "tracked.txt").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
            expected = subprocess.check_output(
                ["git", "ls-files", "-s", "-z"], cwd=repository
            )
            self.assertEqual(eng._tracked_files_sha256(repository), hashlib.sha256(expected).hexdigest())

    def test_removed_runtime_files_are_not_tracked(self) -> None:
        tracked = subprocess.check_output(
            ["git", "ls-files"], cwd=eng.HARNESS_ROOT, text=True, encoding="utf-8"
        ).splitlines()
        forbidden = ("functional_uat", "local_user_uat", "tool_cache", "bootstrap-", "harness/uat/")
        existing = [
            path
            for path in tracked
            if any(item in path for item in forbidden) and (eng.HARNESS_ROOT / path).exists()
        ]
        self.assertFalse(existing)


if __name__ == "__main__":
    unittest.main()
