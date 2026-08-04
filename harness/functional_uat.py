"""Bounded current-code functional UAT orchestrator.

The product repositories own scenarios, expected semantics, Chat Gold and
adaptive controls.  This module only reads those versioned inputs, invokes the
public loopback APIs, and persists safe IDs/hashes so an infrastructure outage
can resume without repeating an uncertain external call.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import local_user_uat as uat


TASK_ID = "TASK-0033"
PROFILE = "preserved-3law-v3-scenario-eval"
SCOPES = ("scenarios", "chat", "hitl", "adaptive", "ui")
Scope = Literal["scenarios", "chat", "hitl", "adaptive", "ui", "all"]
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
STATE_ROOT = (
    WORKSPACE_ROOT
    / "_local"
    / "state"
    / "local-user-uat"
    / "functional"
    / TASK_ID
)
CHECKPOINT = STATE_ROOT / "checkpoint.json"
RUN_LOCK = STATE_ROOT / "run.lock"
FRONTEND_ORIGIN = "http://127.0.0.1:18088"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
TOKEN_ENDPOINT = f"{FRONTEND_ORIGIN}/oidc/realms/customs/protocol/openid-connect/token"
TERMINAL_JOB_STATES = {
    "succeeded",
    "failed",
    "delivery_uncertain",
    "response_received_rejected",
    "response_persistence_uncertain",
}
TERMINAL_JOB_FAILURE_CODES = {
    "failed": "job_failed",
    "delivery_uncertain": "job_delivery_uncertain",
    "response_received_rejected": "job_response_received_rejected",
    "response_persistence_uncertain": "job_response_persistence_uncertain",
}
CHAT_EVAL_IDS = (
    "multi-turn-01",
    "multi-turn-02",
    "multi-turn-03",
    "multi-turn-07",
    "multi-turn-08",
    "multi-turn-09",
    "multi-turn-10",
    "multi-turn-11",
    "multi-turn-12",
    "scenario-actual_ccct_requirement-evidence",
    "insufficient-01",
    "unrelated-01",
    "injection-01",
    "paraphrase-16",
)

_TOKEN_LOCK = threading.Lock()
_TOKEN_ROLE_BY_VALUE: dict[str, str] = {}
_CURRENT_TOKEN_BY_ROLE: dict[str, str] = {}
SCENARIO_ID_BY_CHAT_SCENARIO = {
    "clean_no_requirement": "clean_text_no_requirement_uat",
    "actual_ccct_requirement": "actual_3law_structured_requirement_human_only",
    "untrusted_issuer": "clean_text_untrusted_supporting_document",
    "production_candidate_blocked": "clean_text_production_candidate_blocked",
    "quantity_mismatch": "quantity_mismatch",
    "invoice_amount_mismatch": "invoice_total_mismatch",
    "origin_mismatch": "origin_mismatch",
    "required_certificate_missing": "missing_required_document",
    "prompt_injection": "prompt_injection",
}
CLAIM_SELECTION_BY_SECTION = {
    "conclusion": "direct_answer",
    "document_comparison": "explanation",
    "issues_and_causes": "explanation",
    "regulatory_meaning": "evidence_meaning",
    "business_impact": "impact",
    "next_actions": "next_steps",
    "scope_and_limits": "explanation",
}
PROVIDER_DEPENDENT_SCOPES = frozenset({"scenarios", "chat", "hitl", "adaptive"})
_SAFE_BACKEND_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


class FunctionalUatError(RuntimeError):
    """Safe functional-UAT failure without response bodies or credentials."""


class FunctionalAssertionFailure(FunctionalUatError):
    """A product behavior differs from its versioned expected semantics."""


class ProviderCircuitOpen(FunctionalUatError):
    """A provider-wide condition makes further paid live work unsafe."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class JobTerminalFailure(FunctionalAssertionFailure):
    """A persisted Job reached a safe, non-success terminal state."""

    def __init__(self, state: str, backend_error_code: str | None = None) -> None:
        self.state = state
        self.error_code = TERMINAL_JOB_FAILURE_CODES.get(state, "job_terminal_failure")
        self.backend_error_code = (
            backend_error_code
            if isinstance(backend_error_code, str)
            and _SAFE_BACKEND_ERROR_CODE.fullmatch(backend_error_code)
            else None
        )
        super().__init__(f"job ended in {state}")


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise FunctionalUatError(f"required local file is invalid: {path.name}") from exc
    if not isinstance(value, dict):
        raise FunctionalUatError(f"required local file is invalid: {path.name}")
    return value


def _anthropic_preflight_error_code(status_code: int, payload: Mapping[str, Any]) -> str:
    """Reduce an untrusted provider error to one non-secret circuit code."""

    error = payload.get("error")
    message = str(error.get("message", "")) if isinstance(error, dict) else ""
    normalized = message.casefold()
    if status_code == 401:
        return "anthropic_authentication_failed"
    if status_code == 403:
        return "anthropic_permission_denied"
    if status_code == 429:
        return "anthropic_rate_limited"
    if status_code >= 500:
        return "anthropic_provider_unavailable"
    if status_code == 400 and any(
        marker in normalized for marker in ("billing", "credit", "balance", "spend")
    ):
        return "anthropic_billing_or_credit_unavailable"
    if status_code == 400 and "model" in normalized and any(
        marker in normalized
        for marker in ("access", "available", "does not exist", "invalid", "not found")
    ):
        return "anthropic_model_unavailable"
    return "anthropic_request_rejected"


def _provider_preflight() -> dict[str, str]:
    """Make one tiny, document-free request before starting paid live scopes.

    The fixed endpoint ignores every Anthropic transport override.  Provider
    response text is used only in memory for an allowlisted classification and
    is never returned, logged, checkpointed, or included in an exception.
    """

    binding = _read_object(uat.RUNTIME_BINDING)
    agent_binding = binding.get("agent")
    model_id = agent_binding.get("model_id") if isinstance(agent_binding, dict) else None
    if (
        not isinstance(model_id, str)
        or len(model_id) > 128
        or re.fullmatch(r"claude-[A-Za-z0-9._-]+", model_id) is None
    ):
        raise FunctionalUatError("runtime-bound Claude model identity is invalid")
    agent_values = uat._dotenv(uat.AGENT_ENV)  # noqa: SLF001 - same local control plane
    source_values = uat._dotenv(uat.SOURCE_SECRET)  # noqa: SLF001
    api_key = agent_values.get("ANTHROPIC_API_KEY") or source_values.get("ANTHROPIC_API_KEY")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ProviderCircuitOpen("anthropic_api_key_missing")
    request_body = json.dumps(
        {
            "model": model_id,
            "max_tokens": 8,
            "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": "Reply with OK."}],
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        ANTHROPIC_MESSAGES_URL,
        data=request_body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Anthropic-Version": ANTHROPIC_API_VERSION,
            "X-Api-Key": api_key.strip(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            # The probe contains no Case data.  Still bound the response and
            # discard it immediately; only HTTP success and the pinned model
            # identity are needed by the functional runner.
            raw = response.read(64 * 1024 + 1)
            if response.status != 200 or len(raw) > 64 * 1024:
                raise ProviderCircuitOpen("anthropic_provider_response_invalid")
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(32 * 1024 + 1)
            payload = json.loads(raw.decode("utf-8")) if len(raw) <= 32 * 1024 else {}
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            payload = {}
        safe_payload = payload if isinstance(payload, dict) else {}
        raise ProviderCircuitOpen(
            _anthropic_preflight_error_code(int(exc.code), safe_payload)
        ) from None
    except (TimeoutError, urllib.error.URLError, OSError):
        raise ProviderCircuitOpen("anthropic_provider_unavailable") from None
    return {"status": "PASS", "model_id": model_id}


def _write_object(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for attempt in range(3):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 2:
                raise
            # Windows readers can briefly hold a non-delete-sharing handle.
            time.sleep(0.05)


def _process_identity(pid: int) -> tuple[bool, str | None]:
    """Return liveness and an OS process-start marker without third-party packages."""

    if pid <= 0:
        return False, None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        error_invalid_parameter = 87
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            # Access denied is treated as alive-but-uninspectable. Only the
            # explicit nonexistent-PID error is safe to reclaim.
            return (False, None) if ctypes.get_last_error() == error_invalid_parameter else (True, None)
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return True, None
            value = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
            return True, f"windows-filetime:{value}"
        finally:
            kernel32.CloseHandle(handle)

    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        raw = proc_stat.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False, None
    except (OSError, UnicodeError):
        raw = ""
    if raw:
        closing = raw.rfind(")")
        fields = raw[closing + 2 :].split() if closing >= 0 else []
        if len(fields) > 19:
            return True, f"proc-start-ticks:{fields[19]}"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, None
    except PermissionError:
        return True, None
    except OSError:
        return True, None
    return True, None


class FunctionalRunLock:
    """One exact TASK functional run, with conservative stale-lock recovery."""

    _SCHEMA = "customs-functional-uat-run-lock/v1"
    _MAX_BYTES = 4096

    def __init__(self, path: Path, *, task_id: str = TASK_ID) -> None:
        self.path = path
        self.task_id = task_id
        self._encoded: bytes | None = None

    def _read_existing(self) -> tuple[bytes, dict[str, Any]]:
        try:
            metadata = self.path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or self.path.is_symlink():
                raise FunctionalUatError("functional UAT run lock is not a regular file")
            raw = self.path.read_bytes()
            if not raw or len(raw) > self._MAX_BYTES:
                raise FunctionalUatError("functional UAT run lock is invalid")
            value = json.loads(raw.decode("utf-8"))
        except FileNotFoundError:
            raise
        except FunctionalUatError:
            raise
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise FunctionalUatError("functional UAT run lock is invalid") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != self._SCHEMA
            or value.get("task_id") != self.task_id
            or not isinstance(value.get("pid"), int)
            or not isinstance(value.get("process_start_marker"), str)
            or not isinstance(value.get("lock_id"), str)
        ):
            raise FunctionalUatError("functional UAT run lock is invalid")
        return raw, value

    def __enter__(self) -> "FunctionalRunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pid = os.getpid()
        alive, marker = _process_identity(pid)
        if not alive:
            raise FunctionalUatError("current functional UAT process identity is unavailable")
        owner = {
            "schema_version": self._SCHEMA,
            "task_id": self.task_id,
            "pid": pid,
            "process_start_marker": marker or "unavailable",
            "lock_id": uuid4().hex,
            "acquired_at_utc": dt.datetime.now(dt.UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        }
        encoded = (
            json.dumps(owner, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        for _attempt in range(3):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                try:
                    existing_raw, existing = self._read_existing()
                except FileNotFoundError:
                    continue
                existing_pid = int(existing["pid"])
                existing_marker = str(existing["process_start_marker"])
                existing_alive, observed_marker = _process_identity(existing_pid)
                if existing_alive and (
                    observed_marker is None
                    or existing_marker == "unavailable"
                    or observed_marker == existing_marker
                ):
                    raise FunctionalUatError(
                        f"{self.task_id} functional UAT is already running"
                    )
                # A dead owner or a reused PID with a different start marker is
                # stale. Re-read exact bytes before unlinking so a replacement
                # lock can never be removed by this contender.
                try:
                    if self.path.read_bytes() != existing_raw:
                        raise FunctionalUatError("functional UAT run lock changed during recovery")
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                self.path.unlink(missing_ok=True)
                raise
            self._encoded = encoded
            return self
        raise FunctionalUatError("functional UAT run lock could not be acquired")

    def __exit__(self, exception_type: Any, _exception: Any, _traceback: Any) -> None:
        encoded = self._encoded
        self._encoded = None
        if encoded is None:
            return
        try:
            current = self.path.read_bytes()
        except FileNotFoundError:
            return
        if current != encoded:
            if exception_type is None:
                raise FunctionalUatError("functional UAT run lock ownership changed")
            return
        self.path.unlink(missing_ok=True)


def _supported_review_as_of(metadata: Mapping[str, Any]) -> str:
    value = metadata.get("supported_review_as_of")
    try:
        return dt.date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        raise FunctionalUatError("UAT supported review date is unavailable") from None


def _request_json(
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, Any]]:
    body = None
    if payload is not None:
        body = json.dumps(
            dict(payload),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    role = _TOKEN_ROLE_BY_VALUE.get(token or "")
    effective_token = _CURRENT_TOKEN_BY_ROLE.get(role, token) if role else token

    for attempt in range(2):
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if effective_token:
            headers["Authorization"] = f"Bearer {effective_token}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(
            f"{FRONTEND_ORIGIN}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(16 * 1024 * 1024)
                value = json.loads(raw.decode("utf-8")) if raw else {}
                return int(response.status), value if isinstance(value, dict) else {}
        except urllib.error.HTTPError as exc:
            # The body can contain case facts and is intentionally not retained.
            exc.read(16 * 1024 * 1024)
            if exc.code != 401 or role is None or attempt == 1:
                return int(exc.code), {}
            effective_token = _refresh_role_token(role, stale_token=effective_token)
        except (OSError, TimeoutError, UnicodeError, ValueError, json.JSONDecodeError):
            return 0, {}
    return 0, {}


def _fetch_role_token(role: str) -> str:
    credentials = _read_object(uat.CREDENTIALS)
    users = credentials.get("users")
    if not isinstance(users, dict):
        raise FunctionalUatError("UAT actor credentials are unavailable")
    actor = users.get(role)
    if not isinstance(actor, dict):
        raise FunctionalUatError(f"UAT {role} credential is unavailable")
    username = actor.get("username")
    password = actor.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise FunctionalUatError(f"UAT {role} credential is unavailable")
    encoded = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "client_id": "customs-workbench-spa",
            "username": username,
            "password": password,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=encoded,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.loads(response.read(1024 * 1024).decode("utf-8"))
    except (OSError, TimeoutError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise FunctionalUatError(f"UAT {role} login failed") from exc
    token = value.get("access_token") if isinstance(value, dict) else None
    if not isinstance(token, str) or len(token) < 32:
        raise FunctionalUatError(f"UAT {role} login failed")
    return token


def _refresh_role_token(role: str, *, stale_token: str | None = None) -> str:
    with _TOKEN_LOCK:
        current = _CURRENT_TOKEN_BY_ROLE.get(role)
        if stale_token is not None and current and current != stale_token:
            return current
        token = _fetch_role_token(role)
        _CURRENT_TOKEN_BY_ROLE[role] = token
        _TOKEN_ROLE_BY_VALUE[token] = role
        if stale_token:
            _TOKEN_ROLE_BY_VALUE[stale_token] = role
        return token


def _role_tokens() -> dict[str, str]:
    return {
        role: _refresh_role_token(role)
        for role in ("reviewer", "approver", "auditor")
    }


def _safe_identity() -> dict[str, Any]:
    ready = uat.status()
    if ready.get("status") != "READY":
        raise FunctionalUatError("LOCAL UAT is not READY")
    if (
        ready.get("actual_law_count") != 3
        or ready.get("article_count") != 1007
        or ready.get("mock_fallback_allowed") is not False
        or ready.get("auto_completion_enabled") is not False
    ):
        raise FunctionalUatError("LOCAL UAT bounded runtime identity is invalid")
    state = uat._state()  # noqa: SLF001 - same private local control plane
    binding = _read_object(uat.RUNTIME_BINDING)
    manifest_sha256 = uat._sha256(uat.DATA_MANIFEST)  # noqa: SLF001
    return {
        "functional_identity_sha256": uat._functional_identity_sha256(),  # noqa: SLF001
        "source_fingerprints": state.get("source_fingerprints"),
        "evidence_runtime_v3_sha256": state.get("evidence_runtime_v3_sha256"),
        "runtime_binding_sha256": binding.get("binding_sha256"),
        "data_store_manifest_sha256": manifest_sha256,
        "actual_law_count": 3,
        "article_count": 1007,
        "mock_fallback_allowed": False,
        "auto_completion_enabled": False,
    }


def invalidated_scopes(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> set[str]:
    """Return the smallest safe invalidation set for a changed runtime identity."""

    if not previous:
        return set(SCOPES)
    before = previous.get("source_fingerprints")
    after = current.get("source_fingerprints")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return set(SCOPES)
    invalid: set[str] = set()
    if before.get("frontend") != after.get("frontend"):
        # Scenario definitions and expected semantics are Frontend-owned.
        # Recreated Root Cases invalidate every downstream Case-bound result;
        # Adaptive controls do not consume those Cases and remain reusable.
        invalid.update(("scenarios", "chat", "hitl", "ui"))
    if before.get("backend") != after.get("backend"):
        invalid.update(("scenarios", "chat", "hitl", "ui"))
    if before.get("ai_agent") != after.get("ai_agent"):
        invalid.update(("scenarios", "chat", "hitl", "adaptive", "ui"))
    if before.get("data_platform") != after.get("data_platform"):
        invalid.update(("scenarios", "chat", "hitl", "adaptive", "ui"))
    for field in (
        "evidence_runtime_v3_sha256",
        "data_store_manifest_sha256",
    ):
        if previous.get(field) != current.get(field):
            invalid.update(("scenarios", "chat", "hitl", "adaptive", "ui"))
    if previous.get("runtime_binding_sha256") != current.get("runtime_binding_sha256"):
        # A rotating capability changes the full binding hash but not product
        # behavior. Functional identity is the authoritative final check.
        if previous.get("functional_identity_sha256") != current.get(
            "functional_identity_sha256"
        ):
            invalid.update(("scenarios", "chat", "hitl", "adaptive", "ui"))
    return invalid


class CheckpointStore:
    def __init__(self, path: Path = CHECKPOINT) -> None:
        self.path = path

    def prepare(self, identity: Mapping[str, Any]) -> dict[str, Any]:
        previous = _read_object(self.path) if self.path.is_file() else {}
        scopes = previous.get("scopes")
        if not isinstance(scopes, dict):
            scopes = {}
        else:
            scopes = dict(scopes)
        for scope in invalidated_scopes(previous.get("identity"), identity):
            scopes.pop(scope, None)
        recovered_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
        for scope, record in tuple(scopes.items()):
            if isinstance(record, dict) and record.get("status") == "RUNNING":
                # The caller owns the only live run lock, so any persisted
                # RUNNING state can only belong to a dead/interrupted owner.
                scopes[scope] = {
                    **record,
                    "status": "INTERRUPTED",
                    "error_code": "stale_running_recovered",
                    "updated_at_utc": recovered_at,
                }
        payload = {
            "schema_version": "customs-functional-uat-checkpoint/v1",
            "task_id": TASK_ID,
            "identity": dict(identity),
            "scopes": scopes,
            "updated_at_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            ),
        }
        self.save(payload)
        return payload

    def save(self, payload: Mapping[str, Any]) -> None:
        scopes = payload.get("scopes")
        running = (
            [
                name
                for name, record in scopes.items()
                if isinstance(record, dict) and record.get("status") == "RUNNING"
            ]
            if isinstance(scopes, dict)
            else []
        )
        if len(running) > 1:
            raise FunctionalUatError("functional UAT checkpoint has multiple RUNNING scopes")
        _write_object(self.path, payload)

    def set_scope(
        self,
        checkpoint: dict[str, Any],
        scope: str,
        *,
        status: str,
        result: Mapping[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        if status == "RUNNING":
            active = [
                name
                for name, value in checkpoint.get("scopes", {}).items()
                if name != scope
                and isinstance(value, dict)
                and value.get("status") == "RUNNING"
            ]
            if active:
                raise FunctionalUatError(
                    "functional UAT cannot start two scopes at the same time"
                )
        record: dict[str, Any] = {
            "status": status,
            "updated_at_utc": dt.datetime.now(dt.UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        }
        if result is not None:
            record["result"] = dict(result)
        if error_code is not None:
            record["error_code"] = error_code
        previous = checkpoint.get("scopes", {}).get(scope)
        if (
            result is None
            and isinstance(previous, dict)
            and isinstance(previous.get("progress"), dict)
        ):
            record["progress"] = previous["progress"]
        checkpoint["scopes"][scope] = record
        checkpoint["updated_at_utc"] = record["updated_at_utc"]
        self.save(checkpoint)


def _invalidate_provider_blocked_progress(
    checkpoint: dict[str, Any],
    store: CheckpointStore,
    error_code: str,
) -> None:
    """Retire only incomplete external-call attempts after a global outage."""

    scenarios = checkpoint.get("scopes", {}).get("scenarios")
    progress = scenarios.get("progress") if isinstance(scenarios, dict) else None
    cases = progress.get("cases") if isinstance(progress, dict) else None
    if isinstance(cases, dict):
        safe_cases = dict(cases)
        for scenario_id, raw in tuple(safe_cases.items()):
            if not isinstance(raw, dict) or raw.get("status") == "PASS":
                continue
            prior_error = raw.get("error_code")
            if raw.get("status") == "FAIL" and prior_error not in {
                "job_failed",
                "job_delivery_uncertain",
                "job_response_received_rejected",
                "job_response_persistence_uncertain",
            }:
                # A semantic mismatch is product evidence, not infrastructure.
                continue
            safe_cases[scenario_id] = {
                **raw,
                "status": "INVALIDATED_INFRASTRUCTURE",
                "error_code": error_code,
                "invalidated_attempt": {
                    "case_id": raw.get("case_id"),
                    "job_id": raw.get("job_id"),
                    "status": raw.get("status"),
                    "error_code": prior_error,
                },
            }
        checkpoint["scopes"]["scenarios"] = {
            **scenarios,
            "status": "INTERRUPTED",
            "error_code": error_code,
            "progress": {"cases": safe_cases},
        }

    chat = checkpoint.get("scopes", {}).get("chat")
    chat_progress = chat.get("progress") if isinstance(chat, dict) else None
    groups = chat_progress.get("groups") if isinstance(chat_progress, dict) else None
    if isinstance(groups, dict):
        safe_groups = dict(groups)
        for group_name, raw in tuple(safe_groups.items()):
            if not isinstance(raw, dict) or raw.get("status") == "PASS":
                continue
            safe_groups[group_name] = {
                "name": group_name,
                "status": "INVALIDATED_INFRASTRUCTURE",
                "review_run_id": raw.get("review_run_id"),
                "error_code": error_code,
                "invalidated_attempt": {
                    "conversation_id": raw.get("conversation_id"),
                    "completed_evaluation_count": len(raw.get("evaluations", []))
                    if isinstance(raw.get("evaluations"), list)
                    else 0,
                },
            }
        checkpoint["scopes"]["chat"] = {
            **chat,
            "status": "INTERRUPTED",
            "error_code": error_code,
            "progress": {"groups": safe_groups},
        }

    checkpoint["provider_preflight"] = {
        "status": "BLOCKED_EXTERNAL",
        "error_code": error_code,
        "checked_at_utc": dt.datetime.now(dt.UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    checkpoint["updated_at_utc"] = checkpoint["provider_preflight"]["checked_at_utc"]
    store.save(checkpoint)


def _record_provider_preflight(
    checkpoint: dict[str, Any],
    store: CheckpointStore,
    result: Mapping[str, Any],
) -> None:
    """Persist only the safe probe outcome, never provider response content."""

    checkpoint["provider_preflight"] = {
        "status": "PASS",
        "model_id": result.get("model_id"),
        "checked_at_utc": dt.datetime.now(dt.UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    checkpoint["updated_at_utc"] = checkpoint["provider_preflight"]["checked_at_utc"]
    store.save(checkpoint)


def _frontend_command(
    arguments: Sequence[str],
    *,
    stdin: Mapping[str, Any] | None = None,
    timeout: int = 180,
) -> dict[str, Any]:
    frontend = uat._repo("frontend")  # noqa: SLF001
    dependency_link: Path | None = None
    try:
        dependency_link = uat._link_frontend_dependencies(  # noqa: SLF001
            uat._ensure_frontend_dependencies()  # noqa: SLF001
        )
        node = uat._npm().parent / ("node.exe" if os.name == "nt" else "node")  # noqa: SLF001
        completed = subprocess.run(
            [str(node), *arguments],
            cwd=frontend,
            input=(
                json.dumps(stdin, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if stdin is not None
                else None
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if completed.returncode:
            raise FunctionalUatError("Frontend-owned functional command failed")
        value = json.loads(completed.stdout)
        if not isinstance(value, dict):
            raise FunctionalUatError("Frontend-owned functional command returned invalid JSON")
        return value
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        raise FunctionalUatError("Frontend-owned functional command failed") from exc
    finally:
        if dependency_link is not None:
            uat._unlink_frontend_dependencies(dependency_link)  # noqa: SLF001


def _scenario_export() -> list[dict[str, Any]]:
    payload = _frontend_command(
        [
            "--experimental-strip-types",
            "scripts/export-uat-scenarios.mjs",
            "--profile",
            PROFILE,
        ]
    )
    scenarios = payload.get("scenarios")
    if (
        payload.get("schema_version") != "frontend-uat-scenario-export/v2"
        or payload.get("evidence_profile") != PROFILE
        or payload.get("scenario_count") != 9
        or not isinstance(scenarios, list)
        or len(scenarios) != 9
        or any(not isinstance(item, dict) for item in scenarios)
    ):
        raise FunctionalUatError("Frontend scenario export is invalid")
    return scenarios


def _wait_job(path: str, token: str, *, timeout: int = 900) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    failed_since: float | None = None
    failed_attempt_count: int | None = None
    while time.monotonic() < deadline:
        code, value = _request_json("GET", path, token=token, timeout=30)
        if code == 200:
            state = value.get("status", value.get("state"))
            if state == "succeeded":
                return value
            if state == "failed":
                # A retryable worker error is persisted briefly before the
                # same Job returns to queued/running.  Waiting for a stable
                # failure prevents that retry from overlapping the next Root
                # Case and avoids duplicate Claude/RAG work.
                attempt_count = value.get("attempt_count")
                normalized_attempt = (
                    attempt_count if isinstance(attempt_count, int) else None
                )
                if failed_since is None or normalized_attempt != failed_attempt_count:
                    failed_since = time.monotonic()
                    failed_attempt_count = normalized_attempt
                elif time.monotonic() - failed_since >= 90:
                    raise JobTerminalFailure(
                        "failed",
                        value.get("error_code") if isinstance(value, dict) else None,
                    )
            elif isinstance(state, str) and state in TERMINAL_JOB_STATES:
                raise JobTerminalFailure(
                    state,
                    value.get("error_code") if isinstance(value, dict) else None,
                )
            else:
                failed_since = None
                failed_attempt_count = None
        elif code not in {0, 502, 503, 504}:
            raise FunctionalUatError(f"job status failed: HTTP {code}")
        time.sleep(1)
    raise FunctionalUatError("job status is uncertain after timeout")


def _rerank_trace(request_id: str) -> dict[str, Any]:
    try:
        raw = uat.DATA_TRACE.read_bytes()
    except OSError as exc:
        raise FunctionalUatError("safe Data trace is unavailable") from exc
    if len(raw) > 8 * 1024 * 1024:
        raise FunctionalUatError("safe Data trace exceeds its bounded size")
    selected: dict[str, Any] | None = None
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise FunctionalUatError("safe Data trace is invalid") from exc
        if isinstance(item, dict) and item.get("request_id") == request_id:
            selected = item
    retrieval = selected.get("retrieval") if isinstance(selected, dict) else None
    rerank = retrieval.get("rerank") if isinstance(retrieval, dict) else None
    if not isinstance(rerank, dict):
        raise FunctionalAssertionFailure("request-bound BGE rerank trace is absent")
    fields = {
        key: rerank.get(key)
        for key in (
            "attempted",
            "succeeded",
            "attempt_count",
            "result_count",
            "top_k_result_count",
            "duration_ms",
        )
    }
    if fields["attempted"] is not True or fields["succeeded"] is not True:
        raise FunctionalAssertionFailure("request-bound BGE rerank did not succeed")
    return fields


def _metadata() -> dict[str, Any]:
    code, metadata = _request_json("GET", "/api/v2/uat/metadata", timeout=30)
    if code != 200:
        raise FunctionalUatError("UAT metadata is unavailable")
    if (
        metadata.get("evidence_profile") != PROFILE
        or metadata.get("evidence_policy_mode")
        != "SEALED_TEST_ONLY_ACTUAL_CCCT_HUMAN_ONLY"
    ):
        raise FunctionalUatError("UAT metadata profile is invalid")
    return metadata


def _create_scenario(
    scenario: Mapping[str, Any],
    tokens: Mapping[str, str],
    run_id: str,
) -> dict[str, Any]:
    manifest = scenario.get("manifest_v2")
    documents = scenario.get("documents")
    if not isinstance(manifest, dict) or not isinstance(documents, list):
        raise FunctionalUatError("scenario input is incomplete")
    scenario_id = str(scenario.get("scenario_id"))
    code, case = _request_json(
        "POST",
        "/api/v1/cases",
        token=tokens["reviewer"],
        payload={
            "title": f"[TASK-0033] {scenario.get('title', scenario_id)}",
            "external_reference": manifest.get("case_external_reference"),
            "assigned_reviewer_subjects": [],
            "metadata": {
                "scenario_id": scenario_id,
                "evidence_profile": PROFILE,
                "input_manifest_sha256": manifest.get("input_manifest_sha256"),
                "expected_semantics_sha256": manifest.get("expected_semantics_sha256"),
                "functional_uat_run_id": run_id,
            },
        },
        timeout=30,
    )
    if code != 201 or not isinstance(case.get("case_id"), str):
        raise FunctionalUatError(f"scenario Case creation failed: HTTP {code}")
    case_id = str(case["case_id"])
    uploaded: list[dict[str, Any]] = []
    for document in documents:
        if not isinstance(document, dict):
            raise FunctionalUatError("scenario document is invalid")
        code, view = _request_json(
            "POST",
            f"/api/v1/cases/{urllib.parse.quote(case_id, safe='')}/documents",
            token=tokens["reviewer"],
            payload=document,
            timeout=45,
        )
        record = view.get("record")
        if code != 201 or not isinstance(record, dict):
            raise FunctionalUatError(f"scenario document registration failed: HTTP {code}")
        uploaded.append({**record, "filename": document.get("filename")})
    supporting_definition = scenario.get("supporting_document")
    if isinstance(supporting_definition, dict):
        source = next(
            (
                item
                for item in uploaded
                if item.get("filename") == supporting_definition.get("filename")
            ),
            None,
        )
        invoice = next((item for item in uploaded if item.get("document_type") == "CI"), None)
        if not isinstance(source, dict) or not isinstance(invoice, dict):
            raise FunctionalUatError("supporting-document source mapping failed")
        support_payload = {
            "document_id": source["document_id"],
            "document_code": supporting_definition["document_code"],
            "issuer": supporting_definition["issuer"],
            "document_number": supporting_definition["document_number"],
            "issued_on": supporting_definition["issued_on"],
            "valid_until": supporting_definition.get("valid_until"),
            "subject_item_ids": [f"{invoice['document_id']}_line_1"],
            "hs_codes": supporting_definition["hs_codes"],
            "source_sha256": source["content_sha256"],
        }
        code, supporting = _request_json(
            "POST",
            f"/api/v1/cases/{urllib.parse.quote(case_id, safe='')}/supporting-documents",
            token=tokens["reviewer"],
            payload=support_payload,
            timeout=30,
        )
        if code != 201:
            raise FunctionalUatError(f"supporting document registration failed: HTTP {code}")
        if scenario.get("requires_approver") is True:
            document_id = supporting.get("document_id")
            code, _ = _request_json(
                "POST",
                (
                    f"/api/v1/cases/{urllib.parse.quote(case_id, safe='')}"
                    f"/supporting-documents/{urllib.parse.quote(str(document_id), safe='')}/assertions"
                ),
                token=tokens["approver"],
                payload={"method": "human_approver"},
                timeout=30,
            )
            if code != 201:
                raise FunctionalUatError(f"supporting assertion failed: HTTP {code}")
    return {"case": case, "documents": uploaded}


def _evaluate_scenario(
    scenario_id: str,
    review_run: Mapping[str, Any],
    experience: Mapping[str, Any],
    trace: Mapping[str, Any],
    scenario_binding: Mapping[str, Any],
) -> dict[str, Any]:
    value = _frontend_command(
        ["--experimental-strip-types", "scripts/evaluate-uat-scenario.mjs"],
        stdin={
            "scenario_id": scenario_id,
            "evidence_profile": PROFILE,
            "actual": {
                "scenario_binding": dict(scenario_binding),
                "review_run": dict(review_run),
                "experience": dict(experience),
                "evidence_trace": dict(trace),
            },
        },
        timeout=120,
    )
    if value.get("status") != "PASS":
        raise FunctionalAssertionFailure(f"scenario semantics failed: {scenario_id}")
    return value


def _persist_scenario_progress(
    checkpoint: dict[str, Any],
    store: CheckpointStore,
    cases: Mapping[str, Any],
) -> None:
    """Persist safe scenario IDs before any subsequent external call."""

    checkpoint["scopes"]["scenarios"] = {
        "status": "RUNNING",
        "progress": {"cases": dict(cases)},
    }
    store.save(checkpoint)


def _run_scenario_batch(
    checkpoint: dict[str, Any],
    store: CheckpointStore,
    tokens: Mapping[str, str],
    *,
    scenario_ids: set[str] | None = None,
    scenarios: Sequence[Mapping[str, Any]] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    selected_scenarios = list(scenarios) if scenarios is not None else _scenario_export()
    selected_metadata = dict(metadata) if metadata is not None else _metadata()
    run_id = f"functional-{uuid4().hex[:16]}"
    existing = checkpoint["scopes"].get("scenarios")
    prior_cases = (
        existing.get("progress", {}).get("cases", {})
        if isinstance(existing, dict) and isinstance(existing.get("progress"), dict)
        else {}
    )
    cases: dict[str, Any] = dict(prior_cases) if isinstance(prior_cases, dict) else {}
    started = time.monotonic()
    for scenario in selected_scenarios:
        scenario_id = str(scenario.get("scenario_id"))
        if scenario_ids is not None and scenario_id not in scenario_ids:
            continue
        prior = cases.get(scenario_id)
        if isinstance(prior, dict) and prior.get("status") == "PASS":
            continue
        invalidated_attempt: dict[str, Any] | None = None
        if isinstance(prior, dict) and prior.get("status") == "INVALIDATED_INFRASTRUCTURE":
            invalidated_attempt = {
                "case_id": prior.get("case_id"),
                "job_id": prior.get("job_id"),
                "error_code": prior.get("error_code"),
            }
            prior = None
        manifest = scenario.get("manifest_v2")
        if not isinstance(manifest, dict):
            raise FunctionalUatError("scenario manifest is incomplete")

        if isinstance(prior, dict) and isinstance(prior.get("case_id"), str):
            case_id = str(prior["case_id"])
            document_ids = prior.get("document_ids")
            if not isinstance(document_ids, list) or not all(
                isinstance(item, str) for item in document_ids
            ):
                raise FunctionalUatError("saved scenario document lineage is invalid")
            invoice_document_id = prior.get("invoice_document_id")
        else:
            created = _create_scenario(scenario, tokens, run_id)
            case_id = str(created["case"]["case_id"])
            document_ids = [
                str(item["document_id"])
                for item in created["documents"]
                if isinstance(item, dict) and isinstance(item.get("document_id"), str)
            ]
            invoice_document_id = next(
                (
                    item.get("document_id")
                    for item in created["documents"]
                    if isinstance(item, dict) and item.get("document_type") == "CI"
                ),
                None,
            )
            cases[scenario_id] = {
                "status": "CASE_CREATED",
                "case_id": case_id,
                "document_ids": document_ids,
                "invoice_document_id": invoice_document_id,
                "input_manifest_sha256": manifest.get("input_manifest_sha256"),
                "scenario_manifest_sha256": manifest.get("manifest_sha256"),
                "external_reference": manifest.get("case_external_reference"),
                "expected_semantics_sha256": manifest.get(
                    "expected_semantics_sha256"
                ),
                "invalidated_attempt": invalidated_attempt,
            }
            _persist_scenario_progress(checkpoint, store, cases)
            prior = cases[scenario_id]

        saved_job_id = prior.get("job_id") if isinstance(prior, dict) else None
        if isinstance(saved_job_id, str) and saved_job_id:
            job_id = saved_job_id
        else:
            idempotency = (
                f"task0033-review-{_canonical_sha256({'case_id': case_id})[:24]}"
            )
            code, queued = _request_json(
                "POST",
                f"/api/v2/cases/{urllib.parse.quote(case_id, safe='')}/reviews",
                token=tokens["reviewer"],
                payload={
                    "review_as_of": _supported_review_as_of(selected_metadata),
                    "trade_type": "import",
                    "submitted_document_codes": [],
                },
                idempotency_key=idempotency,
                timeout=45,
            )
            job = queued.get("job")
            if code != 202 or not isinstance(job, dict):
                raise FunctionalUatError(
                    f"scenario review enqueue failed: HTTP {code}"
                )
            job_id = str(job.get("job_id"))
            if not job_id:
                raise FunctionalUatError("scenario review enqueue returned no Job ID")
            cases[scenario_id] = {
                **(dict(prior) if isinstance(prior, dict) else {}),
                "status": "REVIEW_QUEUED",
                "case_id": case_id,
                "job_id": job_id,
                "document_ids": document_ids,
                "invoice_document_id": invoice_document_id,
            }
            _persist_scenario_progress(checkpoint, store, cases)
        jobs = _wait_job(
            f"/api/v2/jobs/{urllib.parse.quote(job_id, safe='')}",
            tokens["reviewer"],
        )
        review_run_id = str(jobs.get("review_run_id"))
        code, review_run = _request_json(
            "GET",
            f"/api/v1/review-runs/{urllib.parse.quote(review_run_id, safe='')}",
            token=tokens["reviewer"],
        )
        if code != 200:
            raise FunctionalUatError("scenario review result is unavailable")
        code, experience = _request_json(
            "GET",
            f"/api/v2/review-runs/{urllib.parse.quote(review_run_id, safe='')}/experience",
            token=tokens["reviewer"],
        )
        if code != 200:
            raise FunctionalUatError("scenario experience result is unavailable")
        code, trace = _request_json(
            "GET",
            f"/api/v1/uat/review-runs/{urllib.parse.quote(review_run_id, safe='')}/evidence-trace",
            token=tokens["reviewer"],
        )
        if code != 200:
            raise FunctionalAssertionFailure("request-bound Evidence trace is unavailable")
        result_detail = review_run.get("result_detail")
        provenance = (
            result_detail.get("evidence_provenance")
            if isinstance(result_detail, dict)
            else None
        )
        request_id = (
            provenance.get("request_id") if isinstance(provenance, dict) else None
        )
        if not isinstance(request_id, str):
            # ReviewRun exposes the same bounded identifier as a compatibility
            # projection; the trace endpoint itself still binds through the
            # canonical evidence_provenance record.
            request_id = review_run.get("evidence_request_id")
        if not isinstance(request_id, str) or trace.get("request_id") != request_id:
            raise FunctionalAssertionFailure("Evidence request binding differs")
        retrieval = trace.get("retrieval")
        if not isinstance(retrieval, dict):
            raise FunctionalAssertionFailure("Evidence retrieval trace is incomplete")
        trace = {**trace, "retrieval": {**retrieval, "rerank": _rerank_trace(request_id)}}
        review_run = {
            **review_run,
            "uat_metadata": {
                **selected_metadata,
                "auto_completion_enabled": False,
                "production_release": False,
            },
        }
        verdict = _evaluate_scenario(
            scenario_id,
            review_run,
            experience,
            trace,
            {
                "input_manifest_sha256": manifest.get("input_manifest_sha256"),
                "scenario_manifest_sha256": manifest.get("manifest_sha256"),
                "expected_semantics_sha256": manifest.get("expected_semantics_sha256"),
            },
        )
        cases[scenario_id] = {
            **(
                dict(cases[scenario_id])
                if isinstance(cases.get(scenario_id), dict)
                else {}
            ),
            "status": "PASS",
            "case_id": case_id,
            "job_id": job_id,
            "review_run_id": review_run_id,
            "result_digest": review_run.get("result_digest"),
            "provider_result_sha256": review_run.get("provider_result_sha256"),
            "agent_input_manifest_sha256": review_run.get(
                "agent_input_manifest_sha256"
            ),
            "input_manifest_sha256": manifest.get("input_manifest_sha256"),
            "scenario_manifest_sha256": manifest.get("manifest_sha256"),
            "external_reference": manifest.get("case_external_reference"),
            "expected_semantics_sha256": manifest.get(
                "expected_semantics_sha256"
            ),
            "verdict_sha256": verdict.get("verdict_sha256", _canonical_sha256(verdict)),
            "document_ids": document_ids,
            "invoice_document_id": invoice_document_id,
        }
        _persist_scenario_progress(checkpoint, store, cases)
    return {
        "status": "PASS",
        "case_count": len(cases),
        "actual_claude_rag_count": len(cases),
        "report_count": len(cases),
        "mock_fallback_count": 0,
        "auto_completion_count": 0,
        "cases": cases,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def run_scenarios(
    checkpoint: dict[str, Any],
    store: CheckpointStore,
    tokens: Mapping[str, str],
) -> dict[str, Any]:
    """Run every independent Root Case and collect all functional failures."""

    scenarios = _scenario_export()
    metadata = _metadata()
    scenario_ids = [str(item.get("scenario_id")) for item in scenarios]
    started = time.monotonic()
    failed: list[str] = []
    fresh_live_case_count = 0
    resumed_case_count = 0

    def persist_failure(scenario_id: str, error_code: str) -> None:
        current = checkpoint["scopes"].get("scenarios", {})
        progress = current.get("progress", {}) if isinstance(current, dict) else {}
        existing_cases = progress.get("cases", {}) if isinstance(progress, dict) else {}
        safe_cases = dict(existing_cases) if isinstance(existing_cases, dict) else {}
        safe_cases[scenario_id] = {
            **(
                dict(safe_cases[scenario_id])
                if isinstance(safe_cases.get(scenario_id), dict)
                else {}
            ),
            "status": "FAIL",
            "error_code": error_code,
        }
        checkpoint["scopes"]["scenarios"] = {
            "status": "RUNNING",
            "progress": {"cases": safe_cases},
        }
        store.save(checkpoint)

    for scenario_id in scenario_ids:
        current = checkpoint["scopes"].get("scenarios", {})
        progress = current.get("progress", {}) if isinstance(current, dict) else {}
        cases = progress.get("cases", {}) if isinstance(progress, dict) else {}
        prior = cases.get(scenario_id) if isinstance(cases, dict) else None
        if isinstance(prior, dict) and prior.get("status") == "PASS":
            continue
        resumed_attempt = (
            isinstance(prior, dict)
            and prior.get("status") != "INVALIDATED_INFRASTRUCTURE"
        )
        try:
            _run_scenario_batch(
                checkpoint,
                store,
                tokens,
                scenario_ids={scenario_id},
                scenarios=scenarios,
                metadata=metadata,
            )
            if resumed_attempt:
                resumed_case_count += 1
            else:
                fresh_live_case_count += 1
        except JobTerminalFailure as exc:
            try:
                probe = _provider_preflight()
            except ProviderCircuitOpen as circuit:
                _invalidate_provider_blocked_progress(
                    checkpoint,
                    store,
                    circuit.error_code,
                )
                raise
            _record_provider_preflight(checkpoint, store, probe)
            persist_failure(scenario_id, exc.error_code)
            failed.append(scenario_id)
        except FunctionalAssertionFailure:
            persist_failure(scenario_id, "scenario_semantics_failed")
            failed.append(scenario_id)
    current = checkpoint["scopes"].get("scenarios", {})
    progress = current.get("progress", {}) if isinstance(current, dict) else {}
    cases = progress.get("cases", {}) if isinstance(progress, dict) else {}
    if failed:
        raise FunctionalAssertionFailure(
            f"{len(failed)} independent scenarios failed functional UAT"
        )
    if not isinstance(cases, dict) or len(cases) != 9:
        raise FunctionalAssertionFailure("nine Root scenario results were not completed")
    return {
        "status": "PASS",
        "case_count": 9,
        "actual_claude_rag_count": 9,
        "fresh_live_case_count": fresh_live_case_count,
        "resumed_case_count": resumed_case_count,
        "checkpoint_reused_pass_count": 9
        - fresh_live_case_count
        - resumed_case_count,
        "report_count": 9,
        "mock_fallback_count": 0,
        "auto_completion_count": 0,
        "cases": cases,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def _chat_entries() -> dict[str, dict[str, Any]]:
    dataset = _read_object(
        uat._repo("ai_agent") / "evals" / "commercial_chat_eval_v1.json"  # noqa: SLF001
    )
    entries = dataset.get("entries")
    if not isinstance(entries, list):
        raise FunctionalUatError("Agent Chat evaluation dataset is invalid")
    selected = {
        str(item.get("eval_id")): item
        for item in entries
        if isinstance(item, dict) and item.get("eval_id") in CHAT_EVAL_IDS
    }
    if set(selected) != set(CHAT_EVAL_IDS):
        raise FunctionalUatError("selected Agent Chat evaluation IDs are incomplete")
    return selected


def _external_call_counters() -> tuple[int, int]:
    agent_log = uat.LOG_ROOT / "agent.log"
    try:
        agent_bytes = agent_log.read_bytes()
        trace_bytes = uat.DATA_TRACE.read_bytes()
    except OSError as exc:
        raise FunctionalUatError("safe external-call counters are unavailable") from exc
    if len(agent_bytes) > 32 * 1024 * 1024 or len(trace_bytes) > 8 * 1024 * 1024:
        raise FunctionalUatError("safe external-call counters exceed their bounded size")
    return (
        agent_bytes.count(b'"event":"review_started"'),
        sum(bool(line.strip()) for line in trace_bytes.splitlines()),
    )


def _scenario_case_records(checkpoint: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    scenarios = checkpoint.get("scopes", {}).get("scenarios", {})
    result = scenarios.get("result", scenarios.get("progress", {}))
    cases = result.get("cases") if isinstance(result, dict) else None
    if not isinstance(cases, dict) or len(cases) != 9:
        raise FunctionalUatError("Chat/HITL requires nine completed Root scenarios")
    return {str(key): dict(value) for key, value in cases.items() if isinstance(value, dict)}


def _chat_scenario_id(entry: Mapping[str, Any]) -> str:
    value = entry.get("scenario_id")
    if value is None:
        return "clean_text_no_requirement_uat"
    mapped = SCENARIO_ID_BY_CHAT_SCENARIO.get(str(value))
    if mapped is None:
        raise FunctionalUatError("selected Chat entry has no scenario mapping")
    return mapped


def _run_chat_conversation(
    name: str,
    entries: Sequence[Mapping[str, Any]],
    case: Mapping[str, Any],
    token: str,
    *,
    prior: Mapping[str, Any] | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    run_id = str(case["review_run_id"])
    result_sha = str(case.get("provider_result_sha256"))
    conversation_id = prior.get("conversation_id") if isinstance(prior, dict) else None
    eval_results = (
        [dict(item) for item in prior.get("evaluations", []) if isinstance(item, dict)]
        if isinstance(prior, dict) and isinstance(prior.get("evaluations"), list)
        else []
    )
    parent_message_id = (
        str(eval_results[-1].get("assistant_message_id")) if eval_results else None
    )
    if not isinstance(conversation_id, str):
        code, conversation = _request_json(
            "POST",
            f"/api/v2/review-runs/{urllib.parse.quote(run_id, safe='')}/assistant/conversations",
            token=token,
            payload={"bound_result_sha256": result_sha},
            timeout=30,
        )
        conversation_id = conversation.get("conversation_id")
        if code != 201 or not isinstance(conversation_id, str):
            raise FunctionalUatError(f"Chat conversation creation failed: HTTP {code}")
        if progress is not None:
            progress(
                {
                    "name": name,
                    "status": "RUNNING",
                    "conversation_id": conversation_id,
                    "review_run_id": run_id,
                    "evaluations": eval_results,
                }
            )
    completed_ids = {str(item.get("eval_id")) for item in eval_results}
    for index, entry in enumerate(entries):
        eval_id = str(entry.get("eval_id"))
        if eval_id in completed_ids:
            continue
        question = entry.get("question")
        if not isinstance(question, str):
            raise FunctionalUatError("selected Chat question is invalid")
        mutation_before = (
            _case_mutation_snapshot(case, token)
            if entry.get("prohibits_automatic_correction") is True
            else None
        )
        expected_parent_message_id = parent_message_id
        payload = {
            "question": question,
            "parent_message_id": parent_message_id,
            "bound_result_sha256": result_sha,
        }
        code, queued = _request_json(
            "POST",
            f"/api/v2/assistant/conversations/{urllib.parse.quote(conversation_id, safe='')}/messages",
            token=token,
            payload=payload,
            idempotency_key=(
                "task0033-chat-"
                + _canonical_sha256(
                    {"conversation_id": conversation_id, "eval_id": eval_id, "index": index}
                )[:24]
            ),
            timeout=45,
        )
        job = queued.get("job")
        user_message = queued.get("user_message")
        if code != 202 or not isinstance(job, dict) or not isinstance(user_message, dict):
            raise FunctionalUatError(f"Chat message enqueue failed: HTTP {code}")
        assistant_job_id = str(job.get("assistant_job_id"))
        _wait_job(
            f"/api/v2/assistant/jobs/{urllib.parse.quote(assistant_job_id, safe='')}",
            token,
            timeout=360,
        )
        code, messages = _request_json(
            "GET",
            f"/api/v2/assistant/conversations/{urllib.parse.quote(conversation_id, safe='')}/messages",
            token=token,
        )
        items = messages.get("items")
        if code != 200 or not isinstance(items, list):
            raise FunctionalUatError("Chat messages are unavailable")
        assistant = next(
            (
                item
                for item in reversed(items)
                if isinstance(item, dict)
                and item.get("role") == "assistant"
                and item.get("parent_message_id") == user_message.get("message_id")
            ),
            None,
        )
        answer = assistant.get("structured_answer") if isinstance(assistant, dict) else None
        if not isinstance(answer, dict):
            raise FunctionalAssertionFailure("Chat answer is missing")
        current_user_id = user_message.get("message_id")
        current_assistant_id = assistant.get("message_id") if isinstance(assistant, dict) else None
        if (
            not isinstance(current_user_id, str)
            or not isinstance(current_assistant_id, str)
            or user_message.get("parent_message_id") != expected_parent_message_id
            or assistant.get("parent_message_id") != current_user_id
        ):
            raise FunctionalAssertionFailure("Chat message lineage differs")
        _assert_authoritative_chat_messages(
            items,
            conversation_id=conversation_id,
            review_run_id=run_id,
            case_id=str(case["case_id"]),
            result_sha256=result_sha,
            expected_pairs=[
                *[
                    (str(item["user_message_id"]), str(item["assistant_message_id"]))
                    for item in eval_results
                ],
                (current_user_id, current_assistant_id),
            ],
        )
        expected_scope = entry.get("expected_scope")
        expected_status = entry.get("expected_status")
        generation = answer.get("generation_audit")
        verification = answer.get("verification_audit")
        if (
            answer.get("answer_scope") != expected_scope
            or answer.get("status") != expected_status
            or not isinstance(generation, dict)
            or not isinstance(verification, dict)
        ):
            raise FunctionalAssertionFailure(f"Chat semantics failed: {entry.get('eval_id')}")
        _assert_chat_generation_contract(entry, answer)
        if entry.get("requires_citations") is True and not answer.get("citations"):
            raise FunctionalAssertionFailure(f"Chat citation is absent: {entry.get('eval_id')}")
        for area in entry.get("required_answer_areas", []):
            if not answer.get(str(area)):
                raise FunctionalAssertionFailure(
                    f"Chat answer area is absent: {entry.get('eval_id')}"
                )
        if entry.get("requires_prior_context") is True and not answer.get(
            "used_context_message_ids"
        ):
            raise FunctionalAssertionFailure(f"Chat context is absent: {entry.get('eval_id')}")
        if (
            entry.get("requires_prior_context") is True
            and expected_parent_message_id not in answer.get("used_context_message_ids", [])
        ):
            raise FunctionalAssertionFailure(f"Chat parent context differs: {entry.get('eval_id')}")
        _assert_chat_gold_projection(entry, answer)
        if mutation_before is not None and _case_mutation_snapshot(case, token) != mutation_before:
            raise FunctionalAssertionFailure(
                f"Chat changed the Case or review Runs: {entry.get('eval_id')}"
            )
        parent_message_id = str(assistant.get("message_id"))
        eval_results.append(
            {
                "eval_id": entry.get("eval_id"),
                "status": "PASS",
                "assistant_job_id": assistant_job_id,
                "user_message_id": user_message.get("message_id"),
                "assistant_message_id": assistant.get("message_id"),
                "answer_sha256": answer.get("answer_sha256"),
            }
        )
        completed_ids.add(eval_id)
        if progress is not None:
            progress(
                {
                    "name": name,
                    "status": "RUNNING",
                    "conversation_id": conversation_id,
                    "review_run_id": run_id,
                    "evaluations": eval_results,
                }
            )
    record = {
        "name": name,
        "status": "PASS",
        "conversation_id": conversation_id,
        "review_run_id": run_id,
        "evaluations": eval_results,
        "duplicate_message_count": 0,
    }
    if progress is not None:
        progress(record)
    code, authoritative = _request_json(
        "GET",
        f"/api/v2/assistant/conversations/{urllib.parse.quote(conversation_id, safe='')}/messages",
        token=token,
    )
    items = authoritative.get("items")
    if code != 200 or not isinstance(items, list):
        raise FunctionalUatError("authoritative Chat messages are unavailable")
    _assert_authoritative_chat_messages(
        items,
        conversation_id=conversation_id,
        review_run_id=run_id,
        case_id=str(case["case_id"]),
        result_sha256=result_sha,
        expected_pairs=[
            (str(item["user_message_id"]), str(item["assistant_message_id"]))
            for item in eval_results
        ],
    )
    return record


def _assert_authoritative_chat_messages(
    items: Sequence[Any],
    *,
    conversation_id: str,
    review_run_id: str,
    case_id: str,
    result_sha256: str,
    expected_pairs: Sequence[tuple[str, str]],
) -> None:
    messages = [item for item in items if isinstance(item, dict)]
    message_ids = [item.get("message_id") for item in messages]
    if (
        len(messages) != len(items)
        or any(not isinstance(item, str) for item in message_ids)
        or len(message_ids) != len(set(message_ids))
    ):
        raise FunctionalAssertionFailure("Chat contains duplicate or malformed messages")
    if len(messages) != len(expected_pairs) * 2:
        raise FunctionalAssertionFailure("Chat does not contain exactly one user/assistant pair")
    for message in messages:
        if (
            message.get("conversation_id") != conversation_id
            or message.get("review_run_id") != review_run_id
            or message.get("case_id") != case_id
            or message.get("bound_result_sha256") != result_sha256
        ):
            raise FunctionalAssertionFailure("Chat authority binding differs")
    by_id = {str(item["message_id"]): item for item in messages}
    prior_assistant: str | None = None
    for user_id, assistant_id in expected_pairs:
        user = by_id.get(user_id)
        assistant = by_id.get(assistant_id)
        if (
            not isinstance(user, dict)
            or user.get("role") != "user"
            or user.get("parent_message_id") != prior_assistant
            or not isinstance(assistant, dict)
            or assistant.get("role") != "assistant"
            or assistant.get("parent_message_id") != user_id
        ):
            raise FunctionalAssertionFailure("Chat authoritative parent lineage differs")
        prior_assistant = assistant_id


def _assert_chat_generation_contract(
    entry: Mapping[str, Any], answer: Mapping[str, Any]
) -> None:
    generation = answer.get("generation_audit")
    verification = answer.get("verification_audit")
    if not isinstance(generation, dict) or not isinstance(verification, dict):
        raise FunctionalAssertionFailure(f"Chat audit is absent: {entry.get('eval_id')}")
    scope = answer.get("answer_scope")
    status = answer.get("status")
    if status == "answered" and scope in {
        "case_grounded",
        "evidence_grounded",
        "general_customs_guidance",
    }:
        expected = ("claude", "passed")
    elif scope in {
        "insufficient_evidence",
        "out_of_scope",
        "clarification_needed",
    } and status == scope:
        expected = ("deterministic_fallback", "fallback")
    else:
        raise FunctionalAssertionFailure(
            f"Chat generation policy has no valid scope/status mapping: {entry.get('eval_id')}"
        )
    if (generation.get("mode"), verification.get("status")) != expected:
        raise FunctionalAssertionFailure(
            f"Chat generation contract differs: {entry.get('eval_id')}"
        )


def _assert_chat_gold_projection(entry: Mapping[str, Any], answer: Mapping[str, Any]) -> None:
    generation = answer.get("generation_audit")
    classification = (
        generation.get("scope_classification") if isinstance(generation, dict) else None
    )
    if not isinstance(classification, dict):
        raise FunctionalAssertionFailure("Chat scope-classification audit is absent")
    requires_claude = entry.get("requires_claude_scope_classifier") is True
    if requires_claude != (classification.get("mode") == "claude"):
        raise FunctionalAssertionFailure(f"Chat classifier mode differs: {entry.get('eval_id')}")
    expected_topics = entry.get("required_guidance_topics", [])
    actual_topics = classification.get("guidance_topics")
    if not isinstance(expected_topics, list) or not isinstance(actual_topics, list) or any(
        topic not in actual_topics for topic in expected_topics
    ):
        raise FunctionalAssertionFailure(f"Chat guidance topics differ: {entry.get('eval_id')}")
    if entry.get("requires_refusal") is True and (
        answer.get("answer_scope") != "out_of_scope"
        or answer.get("status") != "out_of_scope"
        or answer.get("citations")
    ):
        raise FunctionalAssertionFailure(f"Chat refusal semantics differ: {entry.get('eval_id')}")
    selections = answer.get("claim_selections")
    if not isinstance(selections, dict):
        raise FunctionalAssertionFailure("Chat claim selections are absent")
    for section in entry.get("required_claim_sections", []):
        selection = CLAIM_SELECTION_BY_SECTION.get(str(section))
        if selection is None or not selections.get(selection):
            raise FunctionalAssertionFailure(
                f"Chat required claim section is absent: {entry.get('eval_id')}"
            )
    # Claim roles are not exposed in the browser contract. Their server-validated
    # content-addressed claim IDs are the strongest public projection available.
    if entry.get("accepted_claim_roles") and not any(selections.values()):
        raise FunctionalAssertionFailure(f"Chat accepted claims are absent: {entry.get('eval_id')}")


def _case_mutation_snapshot(case: Mapping[str, Any], token: str) -> dict[str, Any]:
    case_id = str(case["case_id"])
    code, case_record = _request_json(
        "GET", f"/api/v1/cases/{urllib.parse.quote(case_id, safe='')}", token=token
    )
    code_runs, runs = _request_json(
        "GET",
        f"/api/v1/cases/{urllib.parse.quote(case_id, safe='')}/review-runs",
        token=token,
    )
    items = runs.get("items")
    if code != 200 or code_runs != 200 or not isinstance(items, list):
        raise FunctionalUatError("authoritative Case mutation state is unavailable")
    return {
        "case_revision": case_record.get("revision"),
        "review_runs": sorted(
            (
                str(item.get("review_run_id")),
                str(item.get("result_digest")),
                str(item.get("parent_run_id")),
            )
            for item in items
            if isinstance(item, dict)
        ),
    }


def run_chat(
    checkpoint: dict[str, Any],
    store: CheckpointStore,
    tokens: Mapping[str, str],
) -> dict[str, Any]:
    cases = _scenario_case_records(checkpoint)
    entries = _chat_entries()
    groups: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
    for turn_group in ("mt-summary", "mt-issue", "mt-general"):
        values = [
            entries[item]
            for item in CHAT_EVAL_IDS
            if entries[item].get("turn_group") == turn_group
        ]
        scenario_id = _chat_scenario_id(values[0])
        groups.append((turn_group, values, cases[scenario_id]))
    for eval_id in CHAT_EVAL_IDS:
        entry = entries[eval_id]
        if entry.get("turn_group") is None:
            scenario_id = _chat_scenario_id(entry)
            groups.append((eval_id, [entry], cases[scenario_id]))
    ambiguous = {
        "eval_id": "ambiguous-functional-01",
        "question": "그 부분은 어떻게 해야 합니까?",
        "expected_scope": "clarification_needed",
        "expected_status": "clarification_needed",
        "requires_citations": False,
        "requires_prior_context": False,
        "requires_claude_scope_classifier": True,
        "required_answer_areas": [],
    }
    groups.append(
        (
            "ambiguous-functional-01",
            [ambiguous],
            cases["clean_text_no_requirement_uat"],
        )
    )
    started = time.monotonic()
    current = checkpoint["scopes"].get("chat", {})
    progress_value = current.get("progress", {}) if isinstance(current, dict) else {}
    group_records = (
        dict(progress_value.get("groups", {}))
        if isinstance(progress_value, dict)
        and isinstance(progress_value.get("groups"), dict)
        else {}
    )

    def persist(record: Mapping[str, Any]) -> None:
        group_records[str(record["name"])] = dict(record)
        checkpoint["scopes"]["chat"] = {
            "status": "RUNNING",
            "progress": {"groups": group_records},
        }
        store.save(checkpoint)

    # Independent conversations are intentionally bounded to one here. This
    # remains below the allowed maximum of two and gives exact turn-level
    # checkpointing without racing writes to one state file.
    for name, values, case in groups:
        prior = group_records.get(name)
        if (
            isinstance(prior, dict)
            and prior.get("status") == "PASS"
            and prior.get("duplicate_message_count") == 0
        ):
            continue
        if isinstance(prior, dict) and prior.get("status") == "INVALIDATED_INFRASTRUCTURE":
            prior = None
        _run_chat_conversation(
            name,
            values,
            case,
            tokens["reviewer"],
            prior=prior if isinstance(prior, dict) else None,
            progress=persist,
        )
    records = [dict(group_records[name]) for name, _values, _case in groups]
    evaluation_count = sum(len(item["evaluations"]) for item in records)
    duplicate_message_count = sum(
        int(item.get("duplicate_message_count", -1)) for item in records
    )
    if evaluation_count != 15:
        raise FunctionalAssertionFailure("Chat functional selection did not produce 15 answers")
    if duplicate_message_count != 0:
        raise FunctionalAssertionFailure("Chat contains duplicate authoritative messages")
    return {
        "status": "PASS",
        "evaluation_count": evaluation_count,
        "conversation_count": len(records),
        "configured_maximum_independent_concurrency": 2,
        "observed_maximum_independent_concurrency": 1,
        "conversations": records,
        "duplicate_message_count": duplicate_message_count,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def _proposal_payload(
    case: Mapping[str, Any],
    reader_token: str,
    *,
    parent_result_digest: str | None = None,
) -> dict[str, Any]:
    invoice_document_id = case.get("invoice_document_id")
    if not isinstance(invoice_document_id, str):
        raise FunctionalUatError("scenario document lineage is unavailable")
    code, detail = _request_json(
        "GET",
        f"/api/v1/cases/{urllib.parse.quote(str(case['case_id']), safe='')}",
        token=reader_token,
    )
    if code != 200:
        raise FunctionalUatError("HITL Case revision is unavailable")
    return {
        "parent_result_digest": parent_result_digest or case["result_digest"],
        "parent_input_manifest_sha256": case["agent_input_manifest_sha256"],
        "case_revision": detail.get("revision"),
        "corrections": [
            {
                "document_id": invoice_document_id,
                "field_path": "fields.invoice_no",
                "new_value": "CI-2026-0001-R1",
                "reason": "TASK-0033 functional UAT source-bound correction",
                "confidence": 1.0,
            }
        ],
    }


def _create_proposal(
    case: Mapping[str, Any],
    token: str,
    key: str,
    *,
    digest: str | None = None,
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    payload = _proposal_payload(case, token, parent_result_digest=digest)
    code, proposal = _request_json(
        "POST",
        f"/api/v1/review-runs/{urllib.parse.quote(str(case['review_run_id']), safe='')}/correction-proposals",
        token=token,
        payload=payload,
        idempotency_key=key,
    )
    if code == 0:
        recovery_code, listed = _request_json(
            "GET",
            f"/api/v1/review-runs/{urllib.parse.quote(str(case['review_run_id']), safe='')}/correction-proposals",
            token=token,
        )
        items = listed.get("items") if recovery_code == 200 else None
        recovered = next(
            (
                item
                for item in items
                if isinstance(items, list)
                and isinstance(item, dict)
                and item.get("idempotency_key") == key
            ),
            None,
        )
        if isinstance(recovered, dict):
            return 201, recovered, payload
    return code, proposal, payload


def _decide_proposal(
    proposal: Mapping[str, Any],
    payload: Mapping[str, Any],
    token: str,
    key: str,
    decision: str,
) -> tuple[int, dict[str, Any]]:
    code, value = _request_json(
        "POST",
        f"/api/v1/correction-proposals/{urllib.parse.quote(str(proposal.get('proposal_id')), safe='')}/decisions",
        token=token,
        payload={
            "decision": decision,
            "reason": f"TASK-0033 {decision} control",
            "parent_result_digest": payload["parent_result_digest"],
            "case_revision": payload["case_revision"],
        },
        idempotency_key=key,
        timeout=45,
    )
    if code != 0:
        return code, value
    review_run_id = proposal.get("review_run_id")
    if not isinstance(review_run_id, str):
        return code, value
    recovery_code, listed = _request_json(
        "GET",
        f"/api/v1/review-runs/{urllib.parse.quote(review_run_id, safe='')}/correction-proposals",
        token=token,
    )
    items = listed.get("items") if recovery_code == 200 else None
    recovered = next(
        (
            item
            for item in items
            if isinstance(items, list)
            and isinstance(item, dict)
            and item.get("proposal_id") == proposal.get("proposal_id")
        ),
        None,
    )
    expected = "approved" if decision == "approve" else "rejected"
    if isinstance(recovered, dict) and recovered.get("status") == expected:
        child_job_id = recovered.get("child_job_id")
        return 201, {
            "proposal": recovered,
            "job": {"job_id": child_job_id} if isinstance(child_job_id, str) else None,
        }
    return code, value


def _functional_idempotency_key(
    checkpoint: Mapping[str, Any],
    control: str,
    stage: str,
    case: Mapping[str, Any],
) -> str:
    identity = checkpoint.get("identity", {}).get("functional_identity_sha256")
    return "task0033-" + _canonical_sha256(
        {
            "identity": identity,
            "control": control,
            "stage": stage,
            "case_id": case.get("case_id"),
            "review_run_id": case.get("review_run_id"),
        }
    )[:32]


def _hitl_authority(case: Mapping[str, Any], token: str) -> dict[str, Any]:
    case_id = str(case["case_id"])
    review_run_id = str(case["review_run_id"])
    code_case, case_record = _request_json(
        "GET", f"/api/v1/cases/{urllib.parse.quote(case_id, safe='')}", token=token
    )
    code_runs, runs = _request_json(
        "GET",
        f"/api/v1/cases/{urllib.parse.quote(case_id, safe='')}/review-runs",
        token=token,
    )
    code_proposals, proposals = _request_json(
        "GET",
        f"/api/v1/review-runs/{urllib.parse.quote(review_run_id, safe='')}/correction-proposals",
        token=token,
    )
    code_audit, audit = _request_json(
        "GET", f"/api/v1/cases/{urllib.parse.quote(case_id, safe='')}/audit", token=token
    )
    run_items = runs.get("items")
    proposal_items = proposals.get("items")
    audit_items = audit.get("items")
    if (
        code_case != 200
        or code_runs != 200
        or code_proposals != 200
        or code_audit != 200
        or not isinstance(run_items, list)
        or not isinstance(proposal_items, list)
        or not isinstance(audit_items, list)
    ):
        raise FunctionalUatError("authoritative HITL state is unavailable")
    return {
        "case_revision": case_record.get("revision"),
        "review_run_ids": sorted(
            str(item.get("review_run_id")) for item in run_items if isinstance(item, dict)
        ),
        "proposals": {
            str(item.get("proposal_id")): {
                "status": item.get("status"),
                "child_job_id": item.get("child_job_id"),
            }
            for item in proposal_items
            if isinstance(item, dict)
        },
        "audit_actions": [
            str(item.get("action")) for item in audit_items if isinstance(item, dict)
        ],
    }


def run_hitl(
    checkpoint: dict[str, Any],
    store: CheckpointStore,
    tokens: Mapping[str, str],
) -> dict[str, Any]:
    cases = _scenario_case_records(checkpoint)
    started = time.monotonic()
    current = checkpoint["scopes"].get("hitl", {})
    progress_value = current.get("progress", {}) if isinstance(current, dict) else {}
    controls = (
        dict(progress_value.get("controls", {}))
        if isinstance(progress_value, dict)
        and isinstance(progress_value.get("controls"), dict)
        else {}
    )

    def persist(name: str, record: Mapping[str, Any]) -> None:
        controls[name] = dict(record)
        checkpoint["scopes"]["hitl"] = {
            "status": "RUNNING",
            "progress": {"controls": controls},
        }
        store.save(checkpoint)

    # Stale parent binding: no proposal can be created.
    stale_case = cases["actual_3law_structured_requirement_human_only"]
    if controls.get("stale", {}).get("status") != "PASS":
        stale_before = _hitl_authority(stale_case, tokens["reviewer"])
        code, _, _ = _create_proposal(
            stale_case,
            tokens["reviewer"],
            _functional_idempotency_key(checkpoint, "stale", "proposal", stale_case),
            digest="0" * 64,
        )
        if code != 409:
            raise FunctionalAssertionFailure("stale proposal was not rejected with HTTP 409")
        stale_after = _hitl_authority(stale_case, tokens["reviewer"])
        if stale_after != stale_before:
            raise FunctionalAssertionFailure("stale proposal changed authoritative state")
        persist(
            "stale",
            {
                "control": "stale",
                "status": "PASS",
                "http_status": 409,
                "child_run_count": 0,
                "state_change_count": 0,
            },
        )

    # Same approver acts as maker and checker: conflict, no child.
    maker_case = cases["origin_mismatch"]
    if controls.get("maker_checker", {}).get("status") != "PASS":
        maker_before = _hitl_authority(maker_case, tokens["approver"])
        code, maker, maker_payload = _create_proposal(
            maker_case,
            tokens["approver"],
            _functional_idempotency_key(checkpoint, "maker_checker", "proposal", maker_case),
        )
        if code != 201:
            raise FunctionalUatError("maker-checker proposal creation failed")
        persist(
            "maker_checker",
            {
                "control": "maker_checker",
                "status": "PROPOSAL_CREATED",
                "proposal_id": maker.get("proposal_id"),
            },
        )
        code, _ = _decide_proposal(
            maker,
            maker_payload,
            tokens["approver"],
            _functional_idempotency_key(checkpoint, "maker_checker", "decision", maker_case),
            "approve",
        )
        if code != 409:
            raise FunctionalAssertionFailure("maker-checker was not rejected with HTTP 409")
        maker_after = _hitl_authority(maker_case, tokens["approver"])
        maker_id = str(maker.get("proposal_id"))
        maker_record = maker_after["proposals"].get(maker_id)
        if (
            maker_after["case_revision"] != maker_before["case_revision"]
            or maker_after["review_run_ids"] != maker_before["review_run_ids"]
            or not isinstance(maker_record, dict)
            or maker_record.get("status") != "pending"
            or maker_record.get("child_job_id") is not None
            or "revision_proposal.created" not in maker_after["audit_actions"]
            or "correction_proposal.decided" in (
                maker_after["audit_actions"][len(maker_before["audit_actions"]):]
            )
        ):
            raise FunctionalAssertionFailure("maker-checker changed authoritative HITL state")
        persist(
            "maker_checker",
            {
                "control": "maker_checker",
                "status": "PASS",
                "http_status": 409,
                "proposal_id": maker_id,
                "proposal_status": "pending",
                "child_run_count": 0,
            },
        )

    # Explicit rejection has no Child Run.
    reject_case = cases["quantity_mismatch"]
    if controls.get("rejection", {}).get("status") != "PASS":
        rejection_before = _hitl_authority(reject_case, tokens["reviewer"])
        code, rejected, rejected_payload = _create_proposal(
            reject_case,
            tokens["reviewer"],
            _functional_idempotency_key(checkpoint, "rejection", "proposal", reject_case),
        )
        if code != 201:
            raise FunctionalUatError("rejection proposal creation failed")
        persist(
            "rejection",
            {
                "control": "rejection",
                "status": "PROPOSAL_CREATED",
                "proposal_id": rejected.get("proposal_id"),
            },
        )
        code, rejected_decision = _decide_proposal(
            rejected,
            rejected_payload,
            tokens["approver"],
            _functional_idempotency_key(checkpoint, "rejection", "decision", reject_case),
            "reject",
        )
        if code != 201 or rejected_decision.get("job") is not None:
            raise FunctionalAssertionFailure("rejected proposal created a Child Run")
        rejection_after = _hitl_authority(reject_case, tokens["reviewer"])
        rejected_id = str(rejected.get("proposal_id"))
        rejected_record = rejection_after["proposals"].get(rejected_id)
        new_audit_actions = rejection_after["audit_actions"][
            len(rejection_before["audit_actions"]):
        ]
        if (
            rejection_after["case_revision"] != rejection_before["case_revision"]
            or rejection_after["review_run_ids"] != rejection_before["review_run_ids"]
            or not isinstance(rejected_record, dict)
            or rejected_record.get("status") != "rejected"
            or rejected_record.get("child_job_id") is not None
            or "revision_proposal.created" not in new_audit_actions
            or "correction_proposal.decided" not in new_audit_actions
        ):
            raise FunctionalAssertionFailure("rejection authoritative state differs")
        persist(
            "rejection",
            {
                "control": "rejection",
                "status": "PASS",
                "proposal_id": rejected_id,
                "proposal_status": "rejected",
                "child_run_count": 0,
                "audit_event_count": len(new_audit_actions),
            },
        )

    # Independent approval produces one incremental Child Run.
    approved_case = cases["clean_text_no_requirement_uat"]
    if controls.get("approval", {}).get("status") == "PASS":
        results = [dict(controls[name]) for name in ("stale", "maker_checker", "rejection", "approval")]
        return {
            "status": "PASS",
            "control_count": 4,
            "controls": results,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    code, approved, approved_payload = _create_proposal(
        approved_case,
        tokens["reviewer"],
        _functional_idempotency_key(checkpoint, "approval", "proposal", approved_case),
    )
    if code != 201:
        raise FunctionalUatError("approval proposal creation failed")
    persist(
        "approval",
        {
            "control": "approval",
            "status": "PROPOSAL_CREATED",
            "proposal_id": approved.get("proposal_id"),
        },
    )
    code, approval = _decide_proposal(
        approved,
        approved_payload,
        tokens["approver"],
        _functional_idempotency_key(checkpoint, "approval", "decision", approved_case),
        "approve",
    )
    job = approval.get("job")
    if code != 201 or not isinstance(job, dict):
        raise FunctionalUatError("approval did not enqueue a Child Run")
    job_id = str(job.get("job_id"))
    persist(
        "approval",
        {
            "control": "approval",
            "status": "CHILD_ENQUEUED",
            "proposal_id": approved.get("proposal_id"),
            "child_job_id": job_id,
        },
    )
    child_job = _wait_job(
        f"/api/v1/jobs/{urllib.parse.quote(job_id, safe='')}",
        tokens["approver"],
        timeout=900,
    )
    child_run_id = str(child_job.get("review_run_id"))
    code, child = _request_json(
        "GET",
        f"/api/v1/review-runs/{urllib.parse.quote(child_run_id, safe='')}",
        token=tokens["approver"],
    )
    if code != 200:
        raise FunctionalUatError("Child Run result is unavailable")
    detail = child.get("result_detail")
    issues = detail.get("validation_issues") if isinstance(detail, dict) else None
    issue_codes = {
        str(item.get("issue_code"))
        for item in (issues if isinstance(issues, list) else [])
        if isinstance(item, dict)
    }
    code, experience = _request_json(
        "GET",
        f"/api/v2/review-runs/{urllib.parse.quote(child_run_id, safe='')}/experience",
        token=tokens["approver"],
    )
    presentation = experience.get("user_presentation") if code == 200 else None
    generation = presentation.get("generation_audit") if isinstance(presentation, dict) else None
    verification = presentation.get("verification_audit") if isinstance(presentation, dict) else None
    if (
        child.get("parent_run_id") != approved_case["review_run_id"]
        or not isinstance(detail, dict)
        or detail.get("actual_document_model_calls") != 0
        or detail.get("inherited_document_model_calls") != 4
        or issue_codes != {"INVOICE_NO_MISMATCH"}
        or not isinstance(generation, dict)
        or generation.get("mode") != "claude"
        or not isinstance(verification, dict)
        or verification.get("status") != "passed"
    ):
        raise FunctionalAssertionFailure("approved incremental Child Run semantics differ")
    code, child_trace = _request_json(
        "GET",
        f"/api/v1/uat/review-runs/{urllib.parse.quote(child_run_id, safe='')}/evidence-trace",
        token=tokens["approver"],
    )
    child_request_id = (
        detail.get("evidence_provenance", {}).get("request_id")
        if isinstance(detail, dict)
        and isinstance(detail.get("evidence_provenance"), dict)
        else None
    )
    if (
        code != 200
        or not isinstance(child_request_id, str)
        or child_trace.get("request_id") != child_request_id
    ):
        raise FunctionalAssertionFailure("Child Run Evidence call is not exactly bound")
    _rerank_trace(child_request_id)
    persist(
        "approval",
        {
            "control": "approval",
            "status": "PASS",
            "proposal_id": approved.get("proposal_id"),
            "child_job_id": job_id,
            "child_review_run_id": child_run_id,
            "actual_document_model_calls": 0,
            "inherited_document_model_calls": 4,
            "evidence_call_count": 1,
            "issue_codes": ["INVOICE_NO_MISMATCH"],
        },
    )
    results = [dict(controls[name]) for name in ("stale", "maker_checker", "rejection", "approval")]
    return {
        "status": "PASS",
        "control_count": 4,
        "controls": results,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def run_adaptive() -> dict[str, Any]:
    state = uat._state()  # noqa: SLF001
    environments = state.get("runtime_environments")
    agent_environment = environments.get("ai_agent") if isinstance(environments, dict) else None
    python = Path(str(agent_environment.get("python"))) if isinstance(agent_environment, dict) else None
    if python is None or not python.is_file():
        raise FunctionalUatError("active Agent environment is unavailable")
    agent = uat._repo("ai_agent")  # noqa: SLF001
    fixture = agent / "tools" / "agent_execution_inspector" / "clean_text_production_candidate_v3.request.json"
    controls_fixture = _read_object(agent / "evals" / "uat_adaptive_controls_v1.json")
    fixture_unsigned = {
        key: value for key, value in controls_fixture.items() if key != "dataset_sha256"
    }
    if (
        controls_fixture.get("schema_version") != "customs-uat-adaptive-controls/v1"
        or controls_fixture.get("dataset_sha256") != _canonical_sha256(fixture_unsigned)
    ):
        raise FunctionalUatError("adaptive control fixture identity is invalid")
    expected_controls = controls_fixture.get("controls")
    limits = controls_fixture.get("limits")
    if not isinstance(expected_controls, list) or not isinstance(limits, dict):
        raise FunctionalUatError("adaptive control fixture is incomplete")
    command = [
        str(python),
        "-B",
        "evals/run_uat_adaptive_live.py",
        "--live",
        "--manifest",
        str(uat.AGENT_MANIFEST),
        "--env-file",
        str(uat.AGENT_ENV),
        "--request",
        str(fixture),
        "--output-summary",
        "-",
    ]
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=agent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
        check=False,
    )
    if completed.returncode:
        raise FunctionalAssertionFailure("adaptive controls failed")
    try:
        private = json.loads(completed.stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        raise FunctionalUatError("adaptive control runner returned invalid JSON") from exc
    private_unsigned = (
        {key: value for key, value in private.items() if key != "summary_sha256"}
        if isinstance(private, dict)
        else {}
    )
    controls = private.get("controls") if isinstance(private, dict) else None
    if (
        not isinstance(private, dict)
        or private.get("schema_version") != "customs-uat-adaptive-live-private-summary/v2"
        or private.get("summary_sha256") != _canonical_sha256(private_unsigned)
        or not isinstance(controls, list)
        or len(controls) != 3
    ):
        raise FunctionalAssertionFailure("adaptive control count differs")
    expected_by_id = {
        str(item.get("control_id")): item
        for item in expected_controls
        if isinstance(item, dict) and isinstance(item.get("control_id"), str)
    }
    actual_ids = [
        str(item.get("control_id"))
        for item in controls
        if isinstance(item, dict) and isinstance(item.get("control_id"), str)
    ]
    if len(expected_by_id) != 3 or actual_ids != list(expected_by_id):
        raise FunctionalAssertionFailure("adaptive control IDs differ from the signed fixture")
    safe_controls: list[dict[str, Any]] = []
    for control in controls:
        if not isinstance(control, dict):
            raise FunctionalAssertionFailure("adaptive control result is invalid")
        result = control.get("canonical_result")
        traces = result.get("action_trace") if isinstance(result, dict) else None
        required_actions = result.get("required_actions") if isinstance(result, dict) else None
        if not isinstance(traces, list) or not isinstance(required_actions, list):
            raise FunctionalAssertionFailure("adaptive control public trace is incomplete")
        max_tool_calls = int(limits.get("max_tool_calls", -1))
        max_cycles = int(limits.get("max_cycles", -1))
        if (
            max_tool_calls != 6
            or max_cycles != 2
            or len(traces) > max_tool_calls
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("cycle"), int)
                or int(item["cycle"]) > max_cycles
                for item in traces
            )
            or result.get("review_disposition") == "auto_review_complete"
        ):
            raise FunctionalAssertionFailure("adaptive Tool budget or safe outcome differs")
        expected = expected_by_id[str(control["control_id"])]
        expected_tool = str(expected.get("allowed_tool"))
        observed_tools = [str(item.get("tool_name")) for item in traces]
        action_codes = {
            str(item.get("action_code")) for item in required_actions if isinstance(item, dict)
        }
        tool_semantics_observed = expected_tool in observed_tools or (
            expected_tool == "human.request/v1"
            and "agent_budget_exhausted" in action_codes
            and result.get("review_disposition") == "human_review_required"
        )
        if not tool_semantics_observed or expected.get("outcome_write_authority") is not False:
            raise FunctionalAssertionFailure("adaptive expected Tool semantics differ")
        safe_controls.append(
            {
                "control_id": control.get("control_id"),
                "status": "PASS",
                "observed_tools": observed_tools,
                "review_disposition": (
                    result.get("review_disposition") if isinstance(result, dict) else None
                ),
            }
        )
    # The private bundle (including synthetic source text/model prose) dies here.
    return {
        "status": "PASS",
        "control_count": 3,
        "control_dataset_sha256": controls_fixture["dataset_sha256"],
        "controls": safe_controls,
        "private_bundle_persisted": False,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def run_ui(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    cases = _scenario_case_records(checkpoint)
    chat = checkpoint.get("scopes", {}).get("chat", {}).get("result", {})
    hitl = checkpoint.get("scopes", {}).get("hitl", {}).get("result", {})
    chat_conversations = chat.get("conversations", [])
    chat_control = next(
        (
            item
            for item in chat_conversations
            if isinstance(item, dict) and item.get("name") == "mt-issue"
        ),
        None,
    )
    if not isinstance(chat_control, dict):
        raise FunctionalUatError("functional Browser requires one completed three-turn Chat")
    hitl_controls = hitl.get("controls", [])
    approval = next(
        (
            item
            for item in hitl_controls
            if isinstance(item, dict) and item.get("control") == "approval"
        ),
        None,
    )
    if not isinstance(approval, dict):
        raise FunctionalUatError("functional Browser requires one approved Child Run")
    clean_case = cases["clean_text_no_requirement_uat"]
    chat_case = cases["quantity_mismatch"]
    state_file = STATE_ROOT / "functional-browser-bindings.json"
    _write_object(
        state_file,
        {
            "schema_version": "customs-browser-scenario-bindings/v1",
            "evidence_profile": PROFILE,
            "scenario_count": 9,
            "scenarios": [
                {
                    "scenario_id": scenario_id,
                    "case_id": item.get("case_id"),
                    "review_run_id": item.get("review_run_id"),
                    "external_reference": item.get("external_reference"),
                    "manifest_sha256": item.get("scenario_manifest_sha256"),
                    "result_sha256": item.get("provider_result_sha256"),
                }
                for scenario_id, item in cases.items()
            ],
        },
    )
    binding_sha256 = uat._sha256(state_file)  # noqa: SLF001
    frontend = uat._repo("frontend")  # noqa: SLF001
    dependency_link: Path | None = None
    environment = dict(os.environ)
    environment.update(
        {
            "CUSTOMS_E2E_LIVE": "true",
            "CUSTOMS_UAT_BASE_URL": FRONTEND_ORIGIN,
            "CUSTOMS_E2E_CREDENTIALS_FILE": str(uat.CREDENTIALS.resolve()),
            "CUSTOMS_E2E_SCENARIO_BINDINGS_FILE": str(state_file.resolve()),
            "CUSTOMS_E2E_SCENARIO_BINDINGS_SHA256": binding_sha256,
            "CUSTOMS_E2E_FUNCTIONAL_CHAT_CASE_ID": str(chat_case["case_id"]),
            "CUSTOMS_E2E_FUNCTIONAL_CHAT_RUN_ID": str(
                chat_control["review_run_id"]
            ),
            "CUSTOMS_E2E_FUNCTIONAL_HITL_CASE_ID": str(clean_case["case_id"]),
            "NODE_OPTIONS": "--max-old-space-size=512",
            "npm_config_cache": str(uat.CACHE_ROOT / "npm"),
        }
    )
    environment["PATH"] = str(uat._npm().parent) + os.pathsep + environment.get("PATH", "")  # noqa: SLF001
    started = time.monotonic()
    calls_before = _external_call_counters()
    try:
        dependency_link = uat._link_frontend_dependencies(  # noqa: SLF001
            uat._ensure_frontend_dependencies()  # noqa: SLF001
        )
        completed = subprocess.run(
            [str(uat._npm()), "exec", "playwright", "test", "--", "--project=functional-chromium"],  # noqa: SLF001
            cwd=frontend,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
            check=False,
        )
        if completed.returncode:
            raise FunctionalAssertionFailure("functional Chromium projection failed")
    finally:
        if dependency_link is not None:
            uat._unlink_frontend_dependencies(dependency_link)  # noqa: SLF001
        state_file.unlink(missing_ok=True)
        for name in ("test-results", "playwright-report", "dist", "coverage"):
            path = frontend / name
            if path.is_dir():
                import shutil

                shutil.rmtree(path, ignore_errors=True)
    calls_after = _external_call_counters()
    if calls_after != calls_before:
        raise FunctionalAssertionFailure(
            "read-only functional Browser created a new Agent or Evidence call"
        )
    return {
        "status": "PASS",
        "browser": "Chromium",
        "browser_run_count": 1,
        "new_claude_or_rag_call_count": sum(
            after - before for before, after in zip(calls_before, calls_after, strict=True)
        ),
        "duration_seconds": round(time.monotonic() - started, 3),
    }


RUNNERS = {
    "scenarios": run_scenarios,
    "chat": run_chat,
    "hitl": run_hitl,
    "adaptive": run_adaptive,
    "ui": run_ui,
}


def _scope_prerequisites(scope: str) -> tuple[str, ...]:
    return {
        "scenarios": (),
        "chat": ("scenarios",),
        "hitl": ("scenarios",),
        "adaptive": (),
        "ui": ("scenarios", "chat", "hitl"),
    }[scope]


def _run_serialized(scope: Scope, *, checkpoint_path: Path = CHECKPOINT) -> dict[str, Any]:
    identity_before = _safe_identity()
    store = CheckpointStore(checkpoint_path)
    checkpoint = store.prepare(identity_before)
    requested = list(SCOPES) if scope == "all" else [scope]
    blocked_functional = [
        name
        for name in requested
        if checkpoint["scopes"].get(name, {}).get("status") == "FAIL_FUNCTIONAL"
    ]
    if blocked_functional:
        raise FunctionalUatError(
            f"{blocked_functional[0]} has a functional failure and is not auto-rerun; "
            "change the affected product identity before rerun"
        )
    pending_provider_scopes = [
        name
        for name in requested
        if name in PROVIDER_DEPENDENT_SCOPES
        and checkpoint["scopes"].get(name, {}).get("status") != "PASS"
    ]
    if pending_provider_scopes:
        try:
            probe = _provider_preflight()
        except ProviderCircuitOpen as exc:
            _invalidate_provider_blocked_progress(checkpoint, store, exc.error_code)
            store.set_scope(
                checkpoint,
                pending_provider_scopes[0],
                status="INTERRUPTED",
                error_code=exc.error_code,
            )
            raise
        _record_provider_preflight(checkpoint, store, probe)
    tokens = _role_tokens()
    started = time.monotonic()
    for current in requested:
        existing = checkpoint["scopes"].get(current)
        if isinstance(existing, dict) and existing.get("status") == "PASS":
            continue
        missing = [
            prerequisite
            for prerequisite in _scope_prerequisites(current)
            if checkpoint["scopes"].get(prerequisite, {}).get("status") != "PASS"
        ]
        if missing:
            store.set_scope(
                checkpoint,
                current,
                status="PREREQUISITE_FAILED",
                error_code="prerequisite_failed",
            )
            continue
        store.set_scope(checkpoint, current, status="RUNNING")
        try:
            if current == "scenarios":
                result = run_scenarios(checkpoint, store, tokens)
            elif current == "chat":
                result = run_chat(checkpoint, store, tokens)
            elif current == "hitl":
                result = run_hitl(checkpoint, store, tokens)
            elif current == "adaptive":
                result = run_adaptive()
            else:
                result = run_ui(checkpoint)
        except JobTerminalFailure as exc:
            try:
                probe = _provider_preflight()
            except ProviderCircuitOpen as circuit:
                _invalidate_provider_blocked_progress(checkpoint, store, circuit.error_code)
                store.set_scope(
                    checkpoint,
                    current,
                    status="INTERRUPTED",
                    error_code=circuit.error_code,
                )
                raise circuit
            _record_provider_preflight(checkpoint, store, probe)
            store.set_scope(
                checkpoint,
                current,
                status="FAIL_FUNCTIONAL",
                error_code=exc.error_code,
            )
            raise
        except FunctionalAssertionFailure:
            store.set_scope(
                checkpoint,
                current,
                status="FAIL_FUNCTIONAL",
                error_code="functional_assertion_failed",
            )
            raise
        except ProviderCircuitOpen as exc:
            _invalidate_provider_blocked_progress(checkpoint, store, exc.error_code)
            store.set_scope(
                checkpoint,
                current,
                status="INTERRUPTED",
                error_code=exc.error_code,
            )
            raise
        except FunctionalUatError:
            store.set_scope(
                checkpoint,
                current,
                status="INTERRUPTED",
                error_code="infrastructure_or_harness_interrupted",
            )
            raise
        store.set_scope(checkpoint, current, status="PASS", result=result)
    identity_after = _safe_identity()
    if identity_after != identity_before:
        raise FunctionalAssertionFailure("runtime identity or three-law Store changed during UAT")
    status_by_scope = {
        name: checkpoint["scopes"].get(name, {}).get("status", "NOT_RUN")
        for name in SCOPES
    }
    selected_passed = all(status_by_scope[name] == "PASS" for name in requested)
    return {
        "schema_version": "customs-functional-uat-safe-summary/v1",
        "task_id": TASK_ID,
        "status": "PASS" if selected_passed else "PARTIAL",
        "requested_scope": scope,
        "identity": identity_after,
        "scope_status": status_by_scope,
        "scope_results": {
            name: checkpoint["scopes"][name].get("result")
            for name in SCOPES
            if checkpoint["scopes"].get(name, {}).get("status") == "PASS"
        },
        "mock_fallback_count": 0,
        "auto_completion_count": 0,
        "production_release": False,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def run(scope: Scope, *, checkpoint_path: Path = CHECKPOINT) -> dict[str, Any]:
    lock_path = RUN_LOCK if checkpoint_path == CHECKPOINT else checkpoint_path.with_name("run.lock")
    with FunctionalRunLock(lock_path):
        return _run_serialized(scope, checkpoint_path=checkpoint_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, choices=(TASK_ID,))
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--scope", required=True, choices=(*SCOPES, "all"))
    args = parser.parse_args(argv)
    try:
        if not args.live:
            raise FunctionalUatError("functional UAT requires explicit --live")
        result = run(args.scope)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["status"] == "PASS" else 1
    except ProviderCircuitOpen as exc:
        print(
            json.dumps(
                {"status": "BLOCKED_EXTERNAL", "error_code": exc.error_code},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    except (FunctionalUatError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
        print(
            json.dumps(
                {"status": "FAIL", "error": str(exc)},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
