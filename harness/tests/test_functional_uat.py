from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


HARNESS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARNESS))
MODULE_PATH = HARNESS / "functional_uat.py"
SPEC = importlib.util.spec_from_file_location("functional_uat", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
functional = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(functional)


def identity(**changes):
    value = {
        "functional_identity_sha256": "a" * 64,
        "source_fingerprints": {
            "frontend": {"commit": "f"},
            "backend": {"commit": "b"},
            "ai_agent": {"commit": "a"},
            "data_platform": {"commit": "d"},
        },
        "evidence_runtime_v3_sha256": "e" * 64,
        "runtime_binding_sha256": "r" * 64,
        "data_store_manifest_sha256": "s" * 64,
        "actual_law_count": 3,
        "article_count": 1007,
        "mock_fallback_allowed": False,
        "auto_completion_enabled": False,
    }
    value.update(changes)
    return value


class FunctionalUatTests(unittest.TestCase):
    def tearDown(self) -> None:
        functional._TOKEN_ROLE_BY_VALUE.clear()
        functional._CURRENT_TOKEN_BY_ROLE.clear()

    def test_request_refreshes_expired_role_token_once(self) -> None:
        old_token = "o" * 40
        new_token = "n" * 40
        functional._TOKEN_ROLE_BY_VALUE[old_token] = "reviewer"
        functional._CURRENT_TOKEN_BY_ROLE["reviewer"] = old_token
        unauthorized = urllib.error.HTTPError(
            "http://local.test/job",
            401,
            "expired",
            {},
            io.BytesIO(b"{}"),
        )

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read(_limit):
                return b'{"status":"succeeded"}'

        with (
            patch.object(
                functional.urllib.request,
                "urlopen",
                side_effect=[unauthorized, Response()],
            ) as urlopen,
            patch.object(
                functional,
                "_refresh_role_token",
                return_value=new_token,
            ) as refresh,
        ):
            code, value = functional._request_json(
                "GET", "/api/v2/jobs/job-1", token=old_token
            )

        self.assertEqual((code, value), (200, {"status": "succeeded"}))
        refresh.assert_called_once_with("reviewer", stale_token=old_token)
        self.assertEqual(
            urlopen.call_args_list[1].args[0].get_header("Authorization"),
            f"Bearer {new_token}",
        )

    def test_external_call_counters_use_safe_agent_and_data_markers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            log_root = root / "logs"
            log_root.mkdir()
            (log_root / "agent.log").write_bytes(
                b'{"event":"review_started"}\n'
                b'{"event":"review_finished"}\n'
                b'{"event":"review_started"}\n'
            )
            trace = root / "trace.jsonl"
            trace.write_bytes(b'{"request_id":"one"}\n\n{"request_id":"two"}\n')
            with (
                patch.object(functional.uat, "LOG_ROOT", log_root),
                patch.object(functional.uat, "DATA_TRACE", trace),
            ):
                self.assertEqual(functional._external_call_counters(), (2, 2))

    def test_supported_review_date_is_explicitly_bound(self) -> None:
        self.assertEqual(
            functional._supported_review_as_of({"supported_review_as_of": "2026-07-10"}),
            "2026-07-10",
        )
        with self.assertRaises(functional.FunctionalUatError):
            functional._supported_review_as_of({})

    def test_checkpoint_replace_retries_a_transient_windows_reader(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "checkpoint.json"
            real_replace = functional.os.replace
            calls = 0

            def replace(source, target):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise PermissionError("transient reader")
                real_replace(source, target)

            with (
                patch.object(functional.os, "replace", side_effect=replace),
                patch.object(functional.time, "sleep"),
            ):
                functional._write_object(destination, {"status": "PASS"})

            self.assertEqual(calls, 2)
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), {"status": "PASS"})

    def test_scenario_verdict_receives_actual_case_binding(self) -> None:
        binding = {
            "input_manifest_sha256": "1" * 64,
            "scenario_manifest_sha256": "2" * 64,
            "expected_semantics_sha256": "3" * 64,
        }
        with patch.object(
            functional,
            "_frontend_command",
            return_value={"status": "PASS", "verdict_sha256": "4" * 64},
        ) as frontend:
            functional._evaluate_scenario("scenario", {}, {}, {}, binding)

        self.assertEqual(
            frontend.call_args.kwargs["stdin"]["actual"]["scenario_binding"],
            binding,
        )

    def test_identity_change_invalidates_only_affected_scopes(self) -> None:
        before = identity()
        after = identity()
        after["source_fingerprints"] = dict(before["source_fingerprints"])
        after["source_fingerprints"]["frontend"] = {"commit": "new"}
        self.assertEqual(
            functional.invalidated_scopes(before, after),
            {"scenarios", "chat", "hitl", "ui"},
        )

        after = identity(evidence_runtime_v3_sha256="z" * 64)
        self.assertEqual(functional.invalidated_scopes(before, after), set(functional.SCOPES))

    def test_run_lock_rejects_a_live_exact_owner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "run.lock"
            with patch.object(
                functional,
                "_process_identity",
                return_value=(True, "process-start-1"),
            ):
                with functional.FunctionalRunLock(path):
                    with self.assertRaisesRegex(
                        functional.FunctionalUatError, "already running"
                    ):
                        with functional.FunctionalRunLock(path):
                            self.fail("a live functional owner must not be replaced")
                self.assertFalse(path.exists())

    def test_run_lock_safely_reclaims_a_dead_owner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "run.lock"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": functional.FunctionalRunLock._SCHEMA,
                        "task_id": functional.TASK_ID,
                        "pid": 424242,
                        "process_start_marker": "dead-start",
                        "lock_id": "dead-lock",
                        "acquired_at_utc": "2026-08-04T00:00:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            def process_identity(pid):
                return (False, None) if pid == 424242 else (True, "current-start")

            with patch.object(functional, "_process_identity", side_effect=process_identity):
                with functional.FunctionalRunLock(path):
                    owner = json.loads(path.read_text(encoding="utf-8"))
                    self.assertEqual(owner["pid"], os.getpid())
                    self.assertEqual(owner["process_start_marker"], "current-start")
            self.assertFalse(path.exists())

    def test_prepare_recovers_all_stale_running_scopes_before_resume(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "checkpoint.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "customs-functional-uat-checkpoint/v1",
                        "task_id": functional.TASK_ID,
                        "identity": identity(),
                        "scopes": {
                            "scenarios": {
                                "status": "RUNNING",
                                "progress": {"cases": {"first": {"status": "PASS"}}},
                            },
                            "chat": {
                                "status": "RUNNING",
                                "progress": {"groups": {"mt-summary": {"status": "RUNNING"}}},
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            checkpoint = functional.CheckpointStore(path).prepare(identity())

            self.assertEqual(checkpoint["scopes"]["scenarios"]["status"], "INTERRUPTED")
            self.assertEqual(checkpoint["scopes"]["chat"]["status"], "INTERRUPTED")
            self.assertEqual(
                checkpoint["scopes"]["scenarios"]["progress"]["cases"]["first"]["status"],
                "PASS",
            )
            self.assertFalse(
                any(
                    value.get("status") == "RUNNING"
                    for value in checkpoint["scopes"].values()
                )
            )

    def test_checkpoint_refuses_two_live_running_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = functional.CheckpointStore(Path(raw) / "checkpoint.json")
            checkpoint = store.prepare(identity())
            store.set_scope(checkpoint, "scenarios", status="RUNNING")
            with self.assertRaisesRegex(
                functional.FunctionalUatError, "cannot start two scopes"
            ):
                store.set_scope(checkpoint, "chat", status="RUNNING")
            self.assertEqual(checkpoint["scopes"]["scenarios"]["status"], "RUNNING")
            self.assertNotIn("chat", checkpoint["scopes"])

    def test_checkpoint_preserves_progress_across_infrastructure_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = functional.CheckpointStore(Path(raw) / "checkpoint.json")
            checkpoint = store.prepare(identity())
            checkpoint["scopes"]["scenarios"] = {
                "status": "RUNNING",
                "progress": {"cases": {"first": {"status": "PASS"}}},
            }
            store.save(checkpoint)
            store.set_scope(
                checkpoint,
                "scenarios",
                status="INTERRUPTED",
                error_code="infrastructure_or_harness_interrupted",
            )
            persisted = json.loads(store.path.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["scopes"]["scenarios"]["progress"]["cases"]["first"]["status"],
                "PASS",
            )

    def test_all_scope_runs_in_dependency_order_and_reuses_safe_results(self) -> None:
        observed: list[str] = []

        def result(name: str):
            observed.append(name)
            return {"status": "PASS", "scope": name}

        with tempfile.TemporaryDirectory() as raw:
            checkpoint = Path(raw) / "checkpoint.json"
            with (
                patch.object(functional, "_safe_identity", return_value=identity()),
                patch.object(
                    functional,
                    "_provider_preflight",
                    return_value={"status": "PASS", "model_id": "claude-test"},
                ),
                patch.object(functional, "_role_tokens", return_value={
                    "reviewer": "token", "approver": "token", "auditor": "token"
                }),
                patch.object(
                    functional,
                    "run_scenarios",
                    side_effect=lambda _checkpoint, _store, _tokens: result("scenarios"),
                ),
                patch.object(
                    functional,
                    "run_chat",
                    side_effect=lambda _checkpoint, _store, _tokens: result("chat"),
                ),
                patch.object(
                    functional,
                    "run_hitl",
                    side_effect=lambda _checkpoint, _store, _tokens: result("hitl"),
                ),
                patch.object(functional, "run_adaptive", side_effect=lambda: result("adaptive")),
                patch.object(
                    functional,
                    "run_ui",
                    side_effect=lambda _checkpoint: result("ui"),
                ),
            ):
                summary = functional.run("all", checkpoint_path=checkpoint)
        self.assertEqual(observed, list(functional.SCOPES))
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["mock_fallback_count"], 0)
        self.assertEqual(summary["auto_completion_count"], 0)

    def test_functional_failure_is_not_retried_at_same_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "checkpoint.json"
            store = functional.CheckpointStore(path)
            checkpoint = store.prepare(identity())
            store.set_scope(
                checkpoint,
                "scenarios",
                status="FAIL_FUNCTIONAL",
                error_code="functional_assertion_failed",
            )
            with (
                patch.object(functional, "_safe_identity", return_value=identity()),
                patch.object(functional, "_role_tokens", return_value={
                    "reviewer": "token", "approver": "token", "auditor": "token"
                }),
                self.assertRaisesRegex(functional.FunctionalUatError, "not auto-rerun"),
            ):
                functional.run("scenarios", checkpoint_path=path)

    def test_provider_preflight_error_is_reduced_to_safe_circuit_code(self) -> None:
        payload = {
            "error": {
                "message": "credit balance is too low; secret-value-must-not-escape"
            }
        }
        code = functional._anthropic_preflight_error_code(400, payload)
        error = functional.ProviderCircuitOpen(code)
        self.assertEqual(str(error), "anthropic_billing_or_credit_unavailable")
        self.assertNotIn("secret-value", str(error))

    def test_provider_preflight_does_not_expose_provider_body_or_api_key(self) -> None:
        api_key = "secret-api-key-must-not-escape"
        response = urllib.error.HTTPError(
            functional.ANTHROPIC_MESSAGES_URL,
            400,
            "rejected",
            {},
            io.BytesIO(
                b'{"error":{"message":"credit balance secret-body-must-not-escape"}}'
            ),
        )
        with (
            patch.object(
                functional,
                "_read_object",
                return_value={"agent": {"model_id": "claude-test-model"}},
            ),
            patch.object(functional.uat, "_dotenv", return_value={
                "ANTHROPIC_API_KEY": api_key
            }),
            patch.object(functional.urllib.request, "urlopen", side_effect=response),
            self.assertRaises(functional.ProviderCircuitOpen) as raised,
        ):
            functional._provider_preflight()
        serialized = repr(raised.exception)
        self.assertEqual(
            raised.exception.error_code,
            "anthropic_billing_or_credit_unavailable",
        )
        self.assertNotIn(api_key, serialized)
        self.assertNotIn("secret-body", serialized)

    def test_provider_block_stops_before_tokens_and_invalidates_incomplete_case(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "checkpoint.json"
            store = functional.CheckpointStore(path)
            checkpoint = store.prepare(identity())
            checkpoint["scopes"]["scenarios"] = {
                "status": "INTERRUPTED",
                "progress": {
                    "cases": {
                        "pending": {
                            "status": "REVIEW_QUEUED",
                            "case_id": "case-1",
                            "job_id": "job-1",
                        }
                    }
                },
            }
            store.save(checkpoint)
            with (
                patch.object(functional, "_safe_identity", return_value=identity()),
                patch.object(
                    functional,
                    "_provider_preflight",
                    side_effect=functional.ProviderCircuitOpen(
                        "anthropic_billing_or_credit_unavailable"
                    ),
                ),
                patch.object(functional, "_role_tokens") as role_tokens,
                self.assertRaises(functional.ProviderCircuitOpen),
            ):
                functional.run("scenarios", checkpoint_path=path)

            role_tokens.assert_not_called()
            persisted = json.loads(path.read_text(encoding="utf-8"))
            case = persisted["scopes"]["scenarios"]["progress"]["cases"]["pending"]
            self.assertEqual(case["status"], "INVALIDATED_INFRASTRUCTURE")
            self.assertEqual(case["invalidated_attempt"]["job_id"], "job-1")
            self.assertEqual(
                persisted["provider_preflight"]["error_code"],
                "anthropic_billing_or_credit_unavailable",
            )

    def test_ui_only_scope_does_not_call_paid_provider_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "checkpoint.json"
            store = functional.CheckpointStore(path)
            checkpoint = store.prepare(identity())
            for prerequisite in ("scenarios", "chat", "hitl"):
                store.set_scope(
                    checkpoint,
                    prerequisite,
                    status="PASS",
                    result={"status": "PASS"},
                )
            with (
                patch.object(functional, "_safe_identity", return_value=identity()),
                patch.object(functional, "_provider_preflight") as preflight,
                patch.object(
                    functional,
                    "_role_tokens",
                    return_value={"reviewer": "token"},
                ),
                patch.object(
                    functional,
                    "run_ui",
                    return_value={"status": "PASS"},
                ),
            ):
                summary = functional.run("ui", checkpoint_path=path)
            preflight.assert_not_called()
            self.assertEqual(summary["status"], "PASS")

    def test_mid_scope_provider_circuit_preserves_specific_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "checkpoint.json"
            store = functional.CheckpointStore(path)
            checkpoint = store.prepare(identity())
            store.set_scope(
                checkpoint,
                "scenarios",
                status="PASS",
                result={"status": "PASS"},
            )
            with (
                patch.object(functional, "_safe_identity", return_value=identity()),
                patch.object(
                    functional,
                    "_provider_preflight",
                    side_effect=[
                        {"status": "PASS", "model_id": "claude-test"},
                        functional.ProviderCircuitOpen(
                            "anthropic_billing_or_credit_unavailable"
                        ),
                    ],
                ),
                patch.object(
                    functional,
                    "_role_tokens",
                    return_value={"reviewer": "token"},
                ),
                patch.object(
                    functional,
                    "run_chat",
                    side_effect=functional.JobTerminalFailure(
                        "response_received_rejected"
                    ),
                ),
                self.assertRaises(functional.ProviderCircuitOpen),
            ):
                functional.run("chat", checkpoint_path=path)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["scopes"]["chat"]["status"], "INTERRUPTED")
            self.assertEqual(
                persisted["scopes"]["chat"]["error_code"],
                "anthropic_billing_or_credit_unavailable",
            )

    def test_rerank_projection_is_request_bound_and_safe(self) -> None:
        record = {
            "request_id": "request-1",
            "retrieval": {
                "rerank": {
                    "attempted": True,
                    "succeeded": True,
                    "attempt_count": 1,
                    "result_count": 5,
                    "top_k_result_count": 5,
                    "duration_ms": 17,
                }
            },
            "query": "must not escape",
        }
        with tempfile.TemporaryDirectory() as raw:
            trace = Path(raw) / "trace.jsonl"
            trace.write_text(json.dumps(record) + "\n", encoding="utf-8")
            with patch.object(functional.uat, "DATA_TRACE", trace):
                projection = functional._rerank_trace("request-1")
        self.assertEqual(set(projection), {
            "attempted", "succeeded", "attempt_count", "result_count",
            "top_k_result_count", "duration_ms",
        })
        self.assertNotIn("query", projection)

    def test_chat_gold_is_selected_by_id_instead_of_copied(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn('"multi-turn-01"', source)
        self.assertNotIn("이 심사 결과를 요약해 주십시오.", source)
        self.assertIn("그 부분은 어떻게 해야 합니까?", source)

    def test_authoritative_chat_requires_exact_bound_pairs(self) -> None:
        messages = [
            {
                "message_id": "user-1",
                "conversation_id": "conversation-1",
                "review_run_id": "run-1",
                "case_id": "case-1",
                "bound_result_sha256": "a" * 64,
                "parent_message_id": None,
                "role": "user",
            },
            {
                "message_id": "assistant-1",
                "conversation_id": "conversation-1",
                "review_run_id": "run-1",
                "case_id": "case-1",
                "bound_result_sha256": "a" * 64,
                "parent_message_id": "user-1",
                "role": "assistant",
            },
        ]
        functional._assert_authoritative_chat_messages(
            messages,
            conversation_id="conversation-1",
            review_run_id="run-1",
            case_id="case-1",
            result_sha256="a" * 64,
            expected_pairs=[("user-1", "assistant-1")],
        )
        with self.assertRaisesRegex(
            functional.FunctionalAssertionFailure, "duplicate or malformed"
        ):
            functional._assert_authoritative_chat_messages(
                [*messages, dict(messages[-1])],
                conversation_id="conversation-1",
                review_run_id="run-1",
                case_id="case-1",
                result_sha256="a" * 64,
                expected_pairs=[("user-1", "assistant-1")],
            )

    def test_chat_gold_uses_public_claim_and_classifier_audits(self) -> None:
        entry = {
            "eval_id": "gold-1",
            "requires_claude_scope_classifier": True,
            "required_guidance_topics": ["hsk"],
            "requires_refusal": False,
            "required_claim_sections": ["next_actions"],
            "accepted_claim_roles": ["action"],
        }
        answer = {
            "answer_scope": "general_customs_guidance",
            "status": "answered",
            "citations": [],
            "generation_audit": {
                "scope_classification": {
                    "mode": "claude",
                    "guidance_topics": ["hsk"],
                }
            },
            "claim_selections": {
                "direct_answer": [],
                "explanation": [],
                "evidence_meaning": [],
                "impact": [],
                "next_steps": ["experience_claim_" + "a" * 64],
            },
        }
        functional._assert_chat_gold_projection(entry, answer)
        answer["generation_audit"]["scope_classification"]["mode"] = "fallback"
        with self.assertRaisesRegex(
            functional.FunctionalAssertionFailure, "classifier mode"
        ):
            functional._assert_chat_gold_projection(entry, answer)

    def test_chat_generation_contract_distinguishes_answers_from_safe_termination(self) -> None:
        answered = {
            "answer_scope": "case_grounded",
            "status": "answered",
            "generation_audit": {"mode": "claude"},
            "verification_audit": {"status": "passed"},
        }
        functional._assert_chat_generation_contract({"eval_id": "answered"}, answered)

        for scope in ("insufficient_evidence", "out_of_scope", "clarification_needed"):
            safe = {
                "answer_scope": scope,
                "status": scope,
                "generation_audit": {"mode": "deterministic_fallback"},
                "verification_audit": {"status": "fallback"},
            }
            functional._assert_chat_generation_contract({"eval_id": scope}, safe)

        answered["generation_audit"]["mode"] = "deterministic_fallback"
        with self.assertRaisesRegex(
            functional.FunctionalAssertionFailure, "generation contract"
        ):
            functional._assert_chat_generation_contract({"eval_id": "answered"}, answered)

        unsafe_refusal = {
            "answer_scope": "out_of_scope",
            "status": "out_of_scope",
            "generation_audit": {"mode": "claude"},
            "verification_audit": {"status": "passed"},
        }
        with self.assertRaisesRegex(
            functional.FunctionalAssertionFailure, "generation contract"
        ):
            functional._assert_chat_generation_contract(
                {"eval_id": "unsafe-refusal"}, unsafe_refusal
            )

    def test_ambiguous_functional_question_requires_live_claude_classifier(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        ambiguous = source.split('"eval_id": "ambiguous-functional-01"', 1)[1].split(
            "groups.append", 1
        )[0]
        self.assertIn('"requires_claude_scope_classifier": True', ambiguous)

    def test_hitl_authority_projects_only_safe_state(self) -> None:
        responses = [
            (200, {"revision": 3, "title": "must not persist"}),
            (200, {"items": [{"review_run_id": "run-1"}]}),
            (
                200,
                {
                    "items": [
                        {
                            "proposal_id": "proposal-1",
                            "status": "rejected",
                            "child_job_id": None,
                            "corrections": [{"new_value": "must not persist"}],
                        }
                    ]
                },
            ),
            (200, {"items": [{"action": "correction_proposal.decided"}]}),
        ]
        with patch.object(functional, "_request_json", side_effect=responses):
            value = functional._hitl_authority(
                {"case_id": "case-1", "review_run_id": "run-1"}, "token"
            )
        self.assertEqual(
            value,
            {
                "case_revision": 3,
                "review_run_ids": ["run-1"],
                "proposals": {
                    "proposal-1": {"status": "rejected", "child_job_id": None}
                },
                "audit_actions": ["correction_proposal.decided"],
            },
        )
        self.assertNotIn("must not persist", json.dumps(value))

    def test_wait_job_does_not_finalize_a_transient_retryable_failure(self) -> None:
        responses = [
            (200, {"status": "failed", "attempt_count": 1}),
            (200, {"status": "queued", "attempt_count": 1}),
            (200, {"status": "succeeded", "attempt_count": 2, "review_run_id": "run-1"}),
        ]
        with (
            patch.object(functional, "_request_json", side_effect=responses),
            patch.object(functional.time, "sleep"),
        ):
            result = functional._wait_job("/api/v2/jobs/job-1", "token", timeout=30)
        self.assertEqual(result["review_run_id"], "run-1")

    def test_wait_job_exposes_safe_terminal_failure_code(self) -> None:
        with patch.object(
            functional,
            "_request_json",
            return_value=(200, {"status": "response_received_rejected"}),
        ):
            with self.assertRaises(functional.JobTerminalFailure) as raised:
                functional._wait_job("/api/v2/jobs/job-1", "token", timeout=1)
        self.assertEqual(raised.exception.state, "response_received_rejected")
        self.assertEqual(
            raised.exception.error_code,
            "job_response_received_rejected",
        )

    def test_scenario_terminal_job_failure_keeps_job_error_code(self) -> None:
        checkpoint = {"scopes": {"scenarios": {"status": "RUNNING"}}}

        class MemoryStore:
            def save(self, _value):
                return None

        with (
            patch.object(
                functional,
                "_scenario_export",
                return_value=[{"scenario_id": "graph-request"}],
            ),
            patch.object(
                functional,
                "_run_scenario_batch",
                side_effect=functional.JobTerminalFailure(
                    "response_received_rejected"
                ),
            ),
            patch.object(functional, "_metadata", return_value={}),
            patch.object(
                functional,
                "_provider_preflight",
                return_value={"status": "PASS", "model_id": "claude-test"},
            ),
            self.assertRaises(functional.FunctionalAssertionFailure),
        ):
            functional.run_scenarios(checkpoint, MemoryStore(), {})
        record = checkpoint["scopes"]["scenarios"]["progress"]["cases"][
            "graph-request"
        ]
        self.assertEqual(record["status"], "FAIL")
        self.assertEqual(record["error_code"], "job_response_received_rejected")

    def test_scenario_semantics_failure_keeps_semantics_error_code(self) -> None:
        checkpoint = {"scopes": {"scenarios": {"status": "RUNNING"}}}

        class MemoryStore:
            def save(self, _value):
                return None

        with (
            patch.object(
                functional,
                "_scenario_export",
                return_value=[{"scenario_id": "semantic-mismatch"}],
            ),
            patch.object(
                functional,
                "_run_scenario_batch",
                side_effect=functional.FunctionalAssertionFailure("semantic mismatch"),
            ),
            patch.object(functional, "_metadata", return_value={}),
            self.assertRaises(functional.FunctionalAssertionFailure),
        ):
            functional.run_scenarios(checkpoint, MemoryStore(), {})
        record = checkpoint["scopes"]["scenarios"]["progress"]["cases"][
            "semantic-mismatch"
        ]
        self.assertEqual(record["status"], "FAIL")
        self.assertEqual(record["error_code"], "scenario_semantics_failed")

    def test_provider_circuit_breaker_stops_remaining_scenarios(self) -> None:
        checkpoint = {"scopes": {"scenarios": {"status": "RUNNING"}}}

        class MemoryStore:
            def save(self, _value):
                return None

        scenarios = [{"scenario_id": "first"}, {"scenario_id": "second"}]
        with (
            patch.object(functional, "_scenario_export", return_value=scenarios),
            patch.object(functional, "_metadata", return_value={}),
            patch.object(
                functional,
                "_run_scenario_batch",
                side_effect=functional.JobTerminalFailure("response_received_rejected"),
            ) as batch,
            patch.object(
                functional,
                "_provider_preflight",
                side_effect=functional.ProviderCircuitOpen(
                    "anthropic_billing_or_credit_unavailable"
                ),
            ),
            self.assertRaises(functional.ProviderCircuitOpen),
        ):
            functional.run_scenarios(checkpoint, MemoryStore(), {})
        self.assertEqual(batch.call_count, 1)

    def test_scenario_resume_polls_saved_job_without_new_external_call(self) -> None:
        scenario = {
            "scenario_id": "saved-scenario",
            "manifest_v2": {
                "input_manifest_sha256": "i" * 64,
                "manifest_sha256": "m" * 64,
                "case_external_reference": "saved-reference",
                "expected_semantics_sha256": "e" * 64,
            },
        }
        checkpoint = {
            "scopes": {
                "scenarios": {
                    "status": "RUNNING",
                    "progress": {
                        "cases": {
                            "saved-scenario": {
                                "status": "REVIEW_QUEUED",
                                "case_id": "case-1",
                                "job_id": "job-1",
                                "document_ids": ["document-1"],
                                "invoice_document_id": "document-1",
                            }
                        }
                    },
                }
            }
        }

        def safe_get(method, path, **_kwargs):
            self.assertEqual(method, "GET")
            if path.startswith("/api/v1/review-runs/"):
                return 200, {
                    "result_detail": {
                        "evidence_provenance": {"request_id": "request-1"}
                    },
                    "result_digest": "d" * 64,
                    "provider_result_sha256": "p" * 64,
                    "agent_input_manifest_sha256": "a" * 64,
                }
            if path.endswith("/experience"):
                return 200, {}
            if path.endswith("/evidence-trace"):
                return 200, {"request_id": "request-1", "retrieval": {}}
            self.fail(f"unexpected request: {path}")

        class MemoryStore:
            def save(self, _value):
                return None

        with (
            patch.object(functional, "_scenario_export", return_value=[scenario]),
            patch.object(functional, "_metadata", return_value={}),
            patch.object(functional, "_create_scenario") as create_scenario,
            patch.object(
                functional,
                "_wait_job",
                return_value={"review_run_id": "run-1"},
            ) as wait_job,
            patch.object(functional, "_request_json", side_effect=safe_get),
            patch.object(functional, "_rerank_trace", return_value={}),
            patch.object(
                functional,
                "_evaluate_scenario",
                return_value={"status": "PASS", "verdict_sha256": "v" * 64},
            ),
        ):
            result = functional._run_scenario_batch(
                checkpoint,
                MemoryStore(),
                {"reviewer": "token"},
                scenario_ids={"saved-scenario"},
            )

        create_scenario.assert_not_called()
        wait_job.assert_called_once()
        self.assertEqual(result["cases"]["saved-scenario"]["job_id"], "job-1")
        self.assertEqual(result["cases"]["saved-scenario"]["status"], "PASS")

if __name__ == "__main__":
    unittest.main()
