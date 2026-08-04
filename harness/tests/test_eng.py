from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "eng.py"
SPEC = importlib.util.spec_from_file_location("core_eng", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
eng = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(eng)


class CoreHarnessTests(unittest.TestCase):
    def test_public_command_surface_is_small(self) -> None:
        parser = eng.build_parser()
        samples = (
            ["doctor"],
            ["check", "quick", "--repo", "ai_agent"],
            ["check", "contract", "--repo", "backend", "--task", "TASK-9999"],
            ["check", "candidate", "--task", "TASK-9999", "--live"],
            [
                "check", "functional", "--task", "TASK-0033", "--live",
                "--scope", "all",
            ],
            ["uat", "smoke"],
            ["uat", "candidate"],
            ["uat", "restart"],
            ["uat", "hibernate"],
            ["clean", "--dry-run"],
        )
        for argv in samples:
            with self.subTest(argv=argv):
                self.assertIsNotNone(parser.parse_args(argv))

    def test_only_contract_and_candidate_persist_results(self) -> None:
        gates = eng._load_toml("gates.toml")["levels"]
        self.assertFalse(gates["inspect"]["permanent_artifact"])
        self.assertFalse(gates["quick"]["permanent_artifact"])
        self.assertTrue(gates["contract"]["permanent_artifact"])
        self.assertTrue(gates["candidate"]["permanent_artifact"])
        self.assertTrue(gates["functional"]["permanent_artifact"])
        self.assertNotIn("release", gates)

    def test_shared_harness_origin_is_configured(self) -> None:
        self.assertEqual(
            eng._expected_harness_origin(),
            "https://github.com/Audit-ai-agent/customs-engineering-harness.git",
        )

    def test_local_only_commands_are_not_exposed(self) -> None:
        parser = eng.build_parser()
        for argv in (
            ["task", "start", "TASK-9999", "--title", "sample"],
            ["bundle", "verify"],
            ["knowledge", "select", "--id", "sample"],
        ):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                parser.parse_args(argv)

    def test_powershell_launcher_has_no_isolated_environment(self) -> None:
        launcher = (eng.ENGINEERING_ROOT / "harness" / "eng.ps1").read_text(encoding="utf-8")
        self.assertNotIn("uv run", launcher)
        self.assertNotIn("--isolated", launcher)

    def test_removed_certification_commands_are_not_exposed(self) -> None:
        parser = eng.build_parser()
        for argv in (["check", "release"], ["uat", "full"], ["uat", "scenario-suite"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                parser.parse_args(argv)

    def test_candidate_is_one_live_journey(self) -> None:
        source = (eng.ENGINEERING_ROOT / "harness" / "eng.py").read_text(encoding="utf-8")
        self.assertIn('uat_result = uat("candidate")', source)
        self.assertIn('"journey_count": 1', source)
        self.assertNotIn('uat("full")', source)
        self.assertNotIn('"scenario-suite"', source)

    def test_bootstrap_scripts_discover_the_shared_harness_dynamically(self) -> None:
        for name in ("bootstrap-node.ps1", "bootstrap-uv.ps1"):
            source = (eng.ENGINEERING_ROOT / "harness" / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertIn("$HarnessDirectory", source)
                self.assertNotIn("'_engineering'", source)
                self.assertNotIn("'AGENTS.md'", source)


if __name__ == "__main__":
    unittest.main()
