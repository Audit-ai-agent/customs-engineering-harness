from __future__ import annotations

import hashlib
import inspect
import subprocess
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
        self.assertEqual((parsed.command, parsed.level, parsed.repo), ("check", "quick", "backend"))
        parsed = parser.parse_args(["check", "quick", "--repo", "customs-data-platform"])
        self.assertEqual(
            (parsed.command, parsed.level, parsed.repo),
            ("check", "quick", "customs-data-platform"),
        )
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

    def test_frontend_setup_uses_the_checked_in_lock_without_ci_cleanup(self) -> None:
        implementation = inspect.getsource(eng._setup_frontend)
        self.assertIn('"--package-lock=true"', implementation)
        self.assertNotIn('"--package-lock=false"', implementation)
        self.assertNotIn('["npm", "ci"', implementation)

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
