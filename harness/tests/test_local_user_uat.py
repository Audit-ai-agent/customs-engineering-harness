from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "local_user_uat.py"
SPEC = importlib.util.spec_from_file_location("local_user_uat", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
uat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(uat)


class LocalUserUatLifecycleTests(unittest.TestCase):
    def test_runtime_binding_uses_exact_frontend_loopback_origin(self) -> None:
        digest = "a" * 64
        metadata = {
            "agent_commit": "agent-commit",
            "runtime_release": {"agent_code_snapshot_sha256": digest},
            "model": {"id": "claude-test"},
            "agent": {"source_snapshot_sha256": digest},
            "meaning_catalog_sha256": digest,
        }
        capability = {
            "agent_commit": "agent-commit",
            "agent_code_snapshot_sha256": digest,
            "evidence_runtime_v3_sha256": digest,
            "issued_at": "2026-08-03T00:00:00Z",
            "expires_at": "2026-08-04T00:00:00Z",
            "prompt_bundle_sha256": digest,
            "schema_bundle_sha256": digest,
            "tool_registry_id": "customs-tools/v1",
            "tool_registry_sha256": digest,
            "capability_sha256": digest,
            "file_sha256": digest,
        }
        with tempfile.TemporaryDirectory() as raw:
            binding_path = Path(raw) / "binding.json"
            with (
                patch.object(uat, "RUNTIME_BINDING", binding_path),
                patch.object(uat, "_http_json", return_value=(200, metadata)),
            ):
                binding = uat._write_runtime_binding(
                    evidence_identity={"runtime_release_v3_sha256": digest},
                    capability=capability,
                    agent_environment={
                        "source_snapshot_sha256": digest,
                        "wheel_sha256": digest,
                    },
                    agent_source={"commit": "agent-commit"},
                    credentials={"agent_service_token": "not-recorded"},
                )
        self.assertEqual(binding["loopback_origin"], uat.FRONTEND_ORIGIN)

    def test_candidate_functional_identity_ignores_rotating_attestation(self) -> None:
        digest = "a" * 64
        binding = {
            "schema_version": "customs-local-uat-runtime-binding/v1",
            "mode": "development",
            "issued_at_utc": "2026-08-03T00:00:00Z",
            "expires_at_utc": "2026-08-04T00:00:00Z",
            "loopback_origin": uat.FRONTEND_ORIGIN,
            "agent": {
                "commit": "agent-commit",
                "working_tree_sha256": digest,
                "wheel_sha256": digest,
                "code_snapshot_sha256": digest,
                "model_id": "claude-test",
                "prompt_bundle_sha256": digest,
                "schema_bundle_sha256": digest,
                "tool_registry_id": "customs-tools/v1",
                "tool_registry_sha256": digest,
                "meaning_catalog_sha256": digest,
            },
            "evidence_runtime_v3_sha256": digest,
            "capability_sha256": digest,
            "capability_file_sha256": digest,
            "mock_fallback_allowed": False,
            "auto_completion_allowed": False,
            "binding_sha256": digest,
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            frontend = root / "frontend"
            backend = root / "backend"
            (frontend / "e2e").mkdir(parents=True)
            (backend / "src" / "customs_backend").mkdir(parents=True)
            (frontend / "e2e" / "commercial-workbench.spec.ts").write_text(
                "candidate", encoding="utf-8"
            )
            (backend / "src" / "customs_backend" / "agent_experience_contract.lock.json").write_text(
                "{}", encoding="utf-8"
            )
            binding_path = root / "binding.json"
            state = {"source_fingerprints": {"ai_agent": {"commit": "agent-commit"}}}
            repositories = {"frontend": frontend, "backend": backend}
            with (
                patch.object(uat, "RUNTIME_BINDING", binding_path),
                patch.object(uat, "_state", return_value=state),
                patch.object(uat, "_repo", side_effect=lambda name: repositories[name]),
            ):
                uat._write_json(binding_path, binding)
                baseline = uat._functional_identity_sha256()

                rotating = json.loads(json.dumps(binding))
                rotating.update(
                    {
                        "issued_at_utc": "2026-08-03T01:00:00Z",
                        "expires_at_utc": "2026-08-04T01:00:00Z",
                        "capability_sha256": "b" * 64,
                        "capability_file_sha256": "c" * 64,
                        "binding_sha256": "d" * 64,
                    }
                )
                uat._write_json(binding_path, rotating)
                self.assertEqual(baseline, uat._functional_identity_sha256())

                prompt_changed = json.loads(json.dumps(binding))
                prompt_changed["agent"]["prompt_bundle_sha256"] = "e" * 64
                uat._write_json(binding_path, prompt_changed)
                self.assertNotEqual(baseline, uat._functional_identity_sha256())

                data_changed = json.loads(json.dumps(binding))
                data_changed["evidence_runtime_v3_sha256"] = "f" * 64
                uat._write_json(binding_path, data_changed)
                self.assertNotEqual(baseline, uat._functional_identity_sha256())

                uat._write_json(binding_path, binding)
                state["source_fingerprints"]["ai_agent"]["commit"] = "new-agent-commit"
                self.assertNotEqual(baseline, uat._functional_identity_sha256())

    def test_candidate_browser_checkpoint_is_identity_bound_and_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            harness_checkpoint = root / "harness.json"
            browser_checkpoint = root / "browser.json"
            uat._write_json(
                harness_checkpoint,
                {"candidate_id": "old", "functional_identity_sha256": "a" * 64},
            )
            uat._write_json(browser_checkpoint, {"identity_sha256": "a" * 64})
            environment: dict[str, str] = {}
            with (
                patch.object(uat, "CANDIDATE_CHECKPOINT", harness_checkpoint),
                patch.object(uat, "CANDIDATE_BROWSER_CHECKPOINT", browser_checkpoint),
            ):
                candidate_id = uat._prepare_candidate_checkpoints("b" * 64, environment)
                self.assertFalse(browser_checkpoint.exists())
                self.assertNotEqual(candidate_id, "old")
                self.assertEqual(
                    environment["CUSTOMS_CANDIDATE_STATE_FILE"],
                    str(browser_checkpoint.resolve()),
                )
                self.assertEqual(
                    environment["CUSTOMS_CANDIDATE_IDENTITY_SHA256"], "b" * 64
                )

                uat._write_json(browser_checkpoint, {"identity_sha256": "b" * 64})
                same_environment: dict[str, str] = {}
                self.assertEqual(
                    uat._prepare_candidate_checkpoints("b" * 64, same_environment),
                    candidate_id,
                )
                self.assertTrue(browser_checkpoint.is_file())
                uat._clear_candidate_checkpoints()
                self.assertFalse(harness_checkpoint.exists())
                self.assertFalse(browser_checkpoint.exists())

    def test_reconcile_uses_smallest_dependency_closure(self) -> None:
        sources = {
            name: {"source_fingerprint_sha256": f"{index:064x}"}
            for index, name in enumerate(("frontend", "backend", "ai_agent", "data_platform"), 1)
        }
        state = {
            "source_fingerprints": dict(sources),
            "runtime_environments": {
                name: {} for name in ("backend", "ai_agent", "data_platform")
            },
        }
        observed = {
            "components": {"frontend": 200, "backend": 200, "agent": 200, "data": 200},
            "processes_by_name": {
                name: {"identity_match": True} for name in uat.PROCESS_ORDER
            },
            "actual_law_count": 3,
            "article_count": 1007,
            "capability_valid": True,
            "runtime_binding_match": True,
        }
        changed = {name: dict(value) for name, value in sources.items()}
        changed["frontend"] = {"source_fingerprint_sha256": "f" * 64}
        self.assertEqual(
            uat._reconcile_scope(state, observed, changed, force_restart=False),
            "frontend",
        )
        changed = {name: dict(value) for name, value in sources.items()}
        changed["backend"] = {"source_fingerprint_sha256": "e" * 64}
        self.assertEqual(
            uat._reconcile_scope(state, observed, changed, force_restart=False),
            "backend",
        )
        changed = {name: dict(value) for name, value in sources.items()}
        changed["ai_agent"] = {"source_fingerprint_sha256": "d" * 64}
        self.assertEqual(
            uat._reconcile_scope(state, observed, changed, force_restart=False),
            "agent",
        )
        changed = {name: dict(value) for name, value in sources.items()}
        changed["data_platform"] = {"source_fingerprint_sha256": "c" * 64}
        self.assertEqual(
            uat._reconcile_scope(state, observed, changed, force_restart=False),
            "data",
        )

    def test_process_record_rejects_pid_reuse(self) -> None:
        record = {
            "pid": 17,
            "executable": str(Path("C:/managed/python.exe")),
            "started_at_utc": "2026-08-03T00:00:00Z",
        }
        with patch.object(
            uat,
            "_process_identity",
            return_value={
                "executable": str(Path("C:/managed/python.exe")),
                "started_at_utc": "2026-08-03T00:00:01Z",
            },
        ):
            self.assertFalse(uat._record_matches_process(record))

    def test_backend_port_guard_allows_an_available_loopback_port(self) -> None:
        with patch.object(uat, "_loopback_listener_is_present", return_value=False):
            uat._assert_backend_listener_available({"processes": []})

    def test_backend_port_guard_rejects_untracked_listener_without_stopping_it(self) -> None:
        with (
            patch.object(uat, "_loopback_listener_is_present", return_value=True),
            patch.object(uat, "_run") as run,
        ):
            with self.assertRaisesRegex(uat.UatError, "untracked listener"):
                uat._assert_backend_listener_available({"processes": []})
        run.assert_not_called()

    def test_backend_group_identity_requires_consecutive_stable_observations(self) -> None:
        records = [
            {"name": name, "source_fingerprint": "source"}
            for name in ("backend", "worker", "outbox")
        ]
        with (
            patch.object(
                uat,
                "_backend_group_identity_matches",
                side_effect=(True, False, True, True),
            ) as matches,
            patch.object(uat.time, "sleep"),
        ):
            uat._wait_for_backend_group_identity(
                records,
                "source",
                timeout=1,
                poll_interval=0,
                stable_observations=2,
            )
        self.assertEqual(matches.call_count, 4)

    def test_down_retains_environments_and_hibernate_removes_exact_root(self) -> None:
        with (
            patch.object(uat, "_state", return_value={"runtime_environments": {}, "processes": []}),
            patch.object(uat, "_stop_recorded_processes", return_value=[]),
            patch.object(uat, "_docker_environment", return_value={}),
            patch.object(uat, "_data_environment", return_value={}),
            patch.object(uat, "_repo", return_value=Path("C:/repo")),
            patch.object(uat, "_run") as run,
            patch.object(uat, "_write_json"),
            patch.object(uat, "_remove_managed_environment") as remove,
        ):
            run.return_value.returncode = 0
            result = uat.down()
            self.assertTrue(result["runtime_environments_preserved"])
            remove.assert_not_called()

        with (
            patch.object(uat, "down", return_value={"status": "STOPPED"}),
            patch.object(uat, "_remove_managed_environment") as remove,
            patch.object(Path, "unlink"),
        ):
            result = uat.hibernate()
            remove.assert_called_once_with(uat.ENV_ROOT, allow_root=True)
            self.assertEqual(result["status"], "HIBERNATED")

    def test_smoke_calls_agent_directly_without_browser(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            fixture = repository / "tools" / "agent_execution_inspector"
            fixture.mkdir(parents=True)
            (fixture / "clean_text_production_candidate_v3.request.json").write_text(
                json.dumps({"case_id": "old", "review_run_id": "old", "llm_mode": "llm"}),
                encoding="utf-8",
            )
            envelope = {
                "envelope_sha256": "a" * 64,
                "canonical_result": {
                    "review_run_id": "run",
                    "result_sha256": "b" * 64,
                    "retrieval_status": "ok",
                    "retrieval_coverage": "complete",
                    "review_disposition": "human_review_required",
                    "model_calls": [{"provider": "anthropic"} for _ in range(4)],
                    "rag_evidence": [{"retrieval_methods": ["dense", "bm25"]}],
                    "runtime_release": {"evidence_runtime_v3_sha256": "c" * 64},
                },
                "user_presentation": {
                    "sections": [
                        {"section_code": item}
                        for item in (
                            "conclusion",
                            "process",
                            "document_comparison",
                            "issues_and_causes",
                            "regulatory_meaning",
                            "business_impact",
                            "next_actions",
                            "scope_and_limits",
                        )
                    ],
                    "generation_audit": {"mode": "claude"},
                    "verification_audit": {"status": "passed"},
                },
                "capability_attestation": {"auto_completion_enabled": False},
            }
            with (
                patch.object(uat, "status", return_value={"status": "READY"}),
                patch.object(uat, "_repo", return_value=repository),
                patch.object(uat, "_read_json", side_effect=lambda path: (
                    json.loads(path.read_text(encoding="utf-8"))
                    if path.name.endswith("request.json")
                    else {"agent_service_token": "secret"}
                )),
                patch.object(uat, "_post_json", return_value=(200, envelope)) as post,
                patch.object(
                    uat,
                    "_state",
                    return_value={
                        "evidence_runtime_v3_sha256": "c" * 64,
                        "mock_fallback_allowed": False,
                    },
                ),
                patch.object(
                    uat,
                    "_config",
                    return_value={"limits": {"claude_review_timeout_seconds": 700}},
                ),
            ):
                result = uat.smoke()
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["report_section_count"], 8)
            post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
