"""Small loopback launcher for the representative customs user UAT.

This module starts the already implemented product services and runs one
commercial-style user journey.  It deliberately contains no scenario matrix,
Production projection, RAG benchmark, or duplicated product assertion logic.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4


ENGINEERING_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ENGINEERING_ROOT.parent
CONFIG = ENGINEERING_ROOT / "config" / "local-user-uat.toml"
COMPOSE = ENGINEERING_ROOT / "harness" / "uat" / "compose.yaml"
DATA_COMPOSE_OVERRIDE = ENGINEERING_ROOT / "harness" / "uat" / "three-law-logs-tmpfs.override.yaml"
LOCAL_ROOT = WORKSPACE_ROOT / "_local"
STATE_ROOT = LOCAL_ROOT / "state" / "local-user-uat"
STATE_FILE = STATE_ROOT / "state.json"
LOG_ROOT = STATE_ROOT / "logs"
DATA_CONTEXT = STATE_ROOT / "data-context"
DATA_IDENTITY = STATE_ROOT / "data-evidence-v3-identity.json"
DATA_MANIFEST = STATE_ROOT / "three-law-scenario-eval-manifest.json"
DATA_TRACE = STATE_ROOT / "three-law-scenario-eval-trace.jsonl"
AGENT_MANIFEST = STATE_ROOT / "agent-uat-manifest.json"
RUNTIME_BINDING = STATE_ROOT / "runtime-binding-v1.json"
CANDIDATE_CHECKPOINT = STATE_ROOT / "candidate-checkpoint.json"
CANDIDATE_BROWSER_CHECKPOINT = STATE_ROOT / "candidate-browser-checkpoint.json"
SECRETS = LOCAL_ROOT / "secrets" / "uat"
SOURCE_SECRET = LOCAL_ROOT / "secrets" / ".env"
DATA_ENVIRONMENT = SECRETS / "data-runtime-environment.json"
CREDENTIALS = SECRETS / "credentials.json"
AGENT_ENV = SECRETS / "agent.env"
AGENT_CAPABILITY = SECRETS / "agent-uat-capability.json"
ASSERTION_TRUST = SECRETS / "assertion-trust-store.json"
ASSERTION_SIGNING = SECRETS / "assertion-signing-key-registry.json"
REQUIREMENT_POLICY = SECRETS / "scenario-eval-requirement-policy.json"
COMPOSE_ENV = SECRETS / "compose.env"
REALM_FILE = SECRETS / "customs-uat-realm.json"
DATA_TOKEN = SECRETS / "evidence-service.token"
ENV_ROOT = Path(os.environ["LOCALAPPDATA"]) / "CustomsUAT" / "envs" / "uat"
CACHE_ROOT = Path(os.environ["LOCALAPPDATA"]) / "CustomsUAT" / "cache"
FRONTEND_ORIGIN = "http://127.0.0.1:18088"
DATA_READY = "http://127.0.0.1:18013/internal/rag/v3/health/scenario-eval-ready"
DATA_LIVE = "http://127.0.0.1:18013/internal/rag/v3/health/live"
AGENT_READY = "http://127.0.0.1:18080/health/uat-ready"
AGENT_LIVE = "http://127.0.0.1:18080/health/live"
AGENT_METADATA_V3 = "http://127.0.0.1:18080/internal/agent/v3/metadata"
AGENT_REVIEW_V3 = "http://127.0.0.1:18080/internal/agent/v3/reviews"
BACKEND_READY = "http://127.0.0.1:18081/health/uat-ready"
BACKEND_LIVE = "http://127.0.0.1:18081/health/live"
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 18081
PROFILE = "preserved-3law-v3-scenario-eval"
POLICY_MODE = "SEALED_TEST_ONLY_ACTUAL_CCCT_HUMAN_ONLY"
ENVIRONMENT_PROFILES = {
    "backend": "system-production",
    "ai_agent": "runtime",
    "data_platform": "rag",
}
PROCESS_REPOSITORIES = {
    "data": "data_platform",
    "agent": "ai_agent",
    "backend": "backend",
    "worker": "backend",
    "outbox": "backend",
}
PROCESS_ORDER = ("data", "agent", "backend", "worker", "outbox")
LEGACY_PROCESS_COMMAND_MARKERS = {
    "data": ("run_three_law_v3_scenario_eval.py", "--port 18013"),
    "agent": ("tools.local_uat_agent_service", "--acknowledge-local-uat"),
    "backend": ("uvicorn customs_backend.main:app", "--port 18081"),
    "worker": ("dramatiq customs_backend.worker",),
    "outbox": ("customs_backend.outbox_dispatcher",),
}
DEPENDENCY_SCOPES = {
    "frontend": frozenset(),
    "backend": frozenset({"backend", "worker", "outbox"}),
    "agent": frozenset({"agent", "backend", "worker", "outbox"}),
    "data": frozenset(PROCESS_ORDER),
}


class UatError(RuntimeError):
    """A safe local-UAT failure."""


def _config() -> dict[str, Any]:
    with CONFIG.open("rb") as handle:
        return tomllib.load(handle)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise UatError(f"required local configuration is invalid: {path.name}") from exc
    if not isinstance(value, dict):
        raise UatError(f"required local configuration is invalid: {path.name}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _repo(name: str) -> Path:
    names = {
        "frontend": "customs-frontend",
        "backend": "customs-backend",
        "ai_agent": "customs-ai-agent",
        "data_platform": "customs-data-platform",
    }
    path = (WORKSPACE_ROOT / names[name]).resolve()
    if not path.is_dir() or not (path / ".git").exists():
        raise UatError(f"repository is unavailable: {name}")
    return path


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        cwd=WORKSPACE_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode:
        raise UatError("Git identity check failed")
    return completed.stdout.strip()


def _source_fingerprint(name: str, *, require_clean: bool = False) -> dict[str, Any]:
    repository = _repo(name)
    dirty = bool(_git(repository, "status", "--porcelain=v1", "--untracked-files=all"))
    if require_clean and dirty:
        raise UatError(f"local UAT requires a clean repository: {name}")
    tracked_index = _git(repository, "ls-files", "-s", "-z").encode("utf-8")
    lock = repository / ("package-lock.json" if name == "frontend" else "uv.lock")
    if not lock.is_file():
        raise UatError(f"dependency lock is unavailable: {name}")
    value: dict[str, Any] = {
        "commit": _git(repository, "rev-parse", "HEAD"),
        "working_tree_sha256": hashlib.sha256(tracked_index).hexdigest(),
        "dependency_lock_sha256": _sha256(lock),
        "profile": "frontend-uat" if name == "frontend" else ENVIRONMENT_PROFILES[name],
        "dirty": dirty,
    }
    value["source_fingerprint_sha256"] = _canonical_sha256(value)
    return value


def _source_fingerprints(*, require_clean: bool = False) -> dict[str, dict[str, Any]]:
    return {
        name: _source_fingerprint(name, require_clean=require_clean)
        for name in ("frontend", "backend", "ai_agent", "data_platform")
    }


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        timeout=timeout,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise UatError(f"required private configuration is missing: {path.name}") from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _http_json(url: str, *, token: str | None = None, timeout: int = 20) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(1024 * 1024)
            try:
                value = json.loads(body.decode("utf-8")) if body else {}
            except (UnicodeError, ValueError, json.JSONDecodeError):
                value = {}
            return int(response.status), value if isinstance(value, dict) else {}
    except urllib.error.HTTPError as exc:
        return int(exc.code), {}
    except (OSError, TimeoutError, UnicodeError, ValueError, json.JSONDecodeError):
        return 0, {}


def _post_json(
    url: str,
    payload: Mapping[str, Any],
    *,
    token: str,
    timeout: int,
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(16 * 1024 * 1024)
            value = json.loads(raw.decode("utf-8")) if raw else {}
            return int(response.status), value if isinstance(value, dict) else {}
    except urllib.error.HTTPError as exc:
        return int(exc.code), {}
    except (OSError, TimeoutError, UnicodeError, ValueError, json.JSONDecodeError):
        return 0, {}


def _wait_json(url: str, *, token: str | None = None, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status_code, payload = _http_json(url, token=token, timeout=min(80, timeout))
        if status_code == 200:
            return payload
        time.sleep(2)
    raise UatError(f"service did not become ready: {url}")


def _loopback_listener_is_present(host: str, port: int, *, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _process_alive(pid: int) -> bool:
    if pid < 1:
        return False
    if os.name == "nt":
        completed = _run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            cwd=WORKSPACE_ROOT,
            capture=True,
        )
        return completed.returncode == 0 and str(pid) in completed.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _process_identity(pid: int) -> dict[str, str] | None:
    if not _process_alive(pid):
        return None
    if os.name == "nt":
        command = (
            "$p=Get-Process -Id "
            + str(int(pid))
            + " -ErrorAction Stop; [ordered]@{executable=$p.Path;"
            "started_at_utc=$p.StartTime.ToUniversalTime().ToString('o')}"
            "|ConvertTo-Json -Compress"
        )
        observed = _run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=WORKSPACE_ROOT,
            capture=True,
            timeout=10,
        )
        if observed.returncode:
            return None
        try:
            value = json.loads(observed.stdout)
        except (ValueError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        executable = value.get("executable")
        started = value.get("started_at_utc")
        if not isinstance(executable, str) or not executable or not isinstance(started, str) or not started:
            return None
        return {"executable": str(Path(executable).resolve()), "started_at_utc": started}
    executable_link = Path("/proc") / str(pid) / "exe"
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        executable = str(executable_link.resolve(strict=True))
        fields = stat_path.read_text(encoding="utf-8").split()
        start_token = fields[21]
    except (OSError, IndexError, UnicodeError):
        return None
    return {"executable": executable, "started_at_utc": f"boot-ticks:{start_token}"}


def _process_command_line(pid: int) -> str | None:
    """Read a command line only to retire the pre-v3 local UAT state safely."""

    if not _process_alive(pid):
        return None
    if os.name == "nt":
        command = (
            "$p=Get-CimInstance Win32_Process -Filter \"ProcessId="
            + str(int(pid))
            + "\" -ErrorAction Stop; $p.CommandLine"
        )
        observed = _run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=WORKSPACE_ROOT,
            capture=True,
            timeout=10,
        )
        value = observed.stdout.strip() if observed.returncode == 0 else ""
        return value or None
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip() or None


def _record_matches_process(record: Mapping[str, Any]) -> bool:
    try:
        pid = int(record.get("pid", 0))
    except (TypeError, ValueError):
        return False
    observed = _process_identity(pid)
    expected_executable = record.get("executable")
    expected_started = record.get("started_at_utc")
    return bool(
        observed
        and isinstance(expected_executable, str)
        and isinstance(expected_started, str)
        and os.path.normcase(observed["executable"])
        == os.path.normcase(str(Path(expected_executable).resolve()))
        and observed["started_at_utc"] == expected_started
    )


def _assert_backend_listener_available(
    state: Mapping[str, Any],
    *,
    timeout: float = 2.0,
    poll_interval: float = 0.1,
) -> None:
    """Refuse to replace an untracked listener or a tracked listener that did not stop."""

    deadline = time.monotonic() + timeout
    while _loopback_listener_is_present(BACKEND_HOST, BACKEND_PORT):
        records = state.get("processes")
        tracked_backend = bool(
            isinstance(records, list)
            and any(
                isinstance(record, dict)
                and record.get("name") == "backend"
                and _record_matches_process(record)
                for record in records
            )
        )
        if not tracked_backend:
            raise UatError(
                "Backend port 127.0.0.1:18081 is occupied by an untracked listener; "
                "refusing to start or terminate it"
            )
        if time.monotonic() >= deadline:
            raise UatError("tracked Backend listener on 127.0.0.1:18081 did not stop")
        time.sleep(poll_interval)


def _backend_group_identity_matches(
    records: list[dict[str, Any]],
    expected_source_fingerprint: str,
) -> bool:
    records_by_name = {str(record.get("name")): record for record in records}
    return all(
        isinstance(records_by_name.get(name), dict)
        and records_by_name[name].get("source_fingerprint") == expected_source_fingerprint
        and _record_matches_process(records_by_name[name])
        for name in ("backend", "worker", "outbox")
    )


def _wait_for_backend_group_identity(
    records: list[dict[str, Any]],
    expected_source_fingerprint: str,
    *,
    timeout: float = 6.0,
    poll_interval: float = 0.1,
    stable_observations: int = 2,
) -> None:
    """Require consecutive identity matches after Backend readiness reports 200."""

    deadline = time.monotonic() + timeout
    stable = 0
    while True:
        if _backend_group_identity_matches(records, expected_source_fingerprint):
            stable += 1
            if stable >= stable_observations:
                return
        else:
            stable = 0
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_interval)
    raise UatError(
        "Backend readiness was reported before backend/worker/outbox process identity became stable"
    )


def _state() -> dict[str, Any]:
    return _read_json(STATE_FILE) if STATE_FILE.is_file() else {}


def _external_python(name: str) -> Path:
    lock_hash = _sha256(_repo(name) / "uv.lock")[:16]
    path = ENV_ROOT / name / f"{lock_hash}-{ENVIRONMENT_PROFILES[name]}"
    python = path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return python


def _uv() -> Path:
    configured = CACHE_ROOT / "tools" / "uv" / "v0.11.28" / "uv.exe"
    if configured.is_file():
        return configured
    located = shutil.which("uv")
    if located:
        return Path(located)
    raise UatError("managed uv executable is unavailable")


def _remove_managed_environment(path: Path, *, allow_root: bool = False) -> None:
    """Remove only an owned UAT environment and never hide a partial failure."""

    root = ENV_ROOT.resolve()
    target = path.resolve()
    if target != root and root not in target.parents:
        raise UatError("refusing to remove an environment outside the managed UAT root")
    if target == root and not allow_root:
        raise UatError("refusing to remove the managed UAT root from a child cleanup")
    if path.is_symlink():
        raise UatError("refusing to remove a linked UAT environment")
    if path.exists() and os.name == "nt":
        # Rust-backed uv safely clears dependency trees whose nested license
        # paths exceed the Win32 legacy path limit.  It replaces each target
        # with a tiny valid venv, which shutil can then remove deterministically.
        candidates = (
            sorted(
                (
                    environment
                    for repository in path.iterdir()
                    if repository.is_dir() and not repository.is_symlink()
                    for environment in repository.iterdir()
                    if environment.is_dir() and not environment.is_symlink()
                ),
                key=lambda item: item.as_posix(),
            )
            if target == root
            else [path]
        )
        for environment in candidates:
            if environment.parent.name == "frontend":
                shutil.rmtree(environment)
                continue
            cleared = _run(
                [
                    str(_uv()),
                    "venv",
                    "--clear",
                    "--python",
                    "3.11",
                    str(environment),
                ],
                cwd=WORKSPACE_ROOT,
                capture=True,
                timeout=300,
            )
            if cleared.returncode:
                raise UatError("managed UAT environment could not be cleared")
            shutil.rmtree(environment)
    if path.exists():
        shutil.rmtree(path)
    if path.exists():
        raise UatError("managed UAT environment cleanup was incomplete")


def _remove_superseded_environments(name: str, keep: Path) -> None:
    repository_root = ENV_ROOT / name
    if not repository_root.is_dir():
        return
    for candidate in repository_root.iterdir():
        if candidate == keep or not candidate.is_dir() or candidate.is_symlink():
            continue
        _remove_managed_environment(candidate)


def _agent_snapshot_source(repository: Path, scratch: Path) -> tuple[Path, str]:
    module_path = repository / "tools" / "verify_distribution.py"
    spec = importlib.util.spec_from_file_location("customs_uat_verify_distribution", module_path)
    if spec is None or spec.loader is None:
        raise UatError("Agent source snapshot helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    commit = _git(repository, "rev-parse", "HEAD")
    try:
        payloads = module._git_archive_payloads(repository, commit)
        snapshot_sha256 = module.source_snapshot_sha256(payloads)
        manifest = module.build_manifest_bytes(
            git_sha=commit,
            source_snapshot_sha256_value=snapshot_sha256,
        )
    except Exception as exc:  # The helper owns its fail-closed error taxonomy.
        raise UatError("Agent source snapshot could not be materialized") from exc
    source = scratch / "source"
    for name, payload in payloads.items():
        destination = source.joinpath(*name.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    manifest_path = source / "src" / "customs_ai_agent" / "build-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(manifest)
    return source, snapshot_sha256


def _build_and_install_project_wheel(name: str, python: Path) -> dict[str, str]:
    repository = _repo(name)
    scratch_root = Path(os.environ.get("TEMP", os.environ.get("TMP", str(Path.cwd()))))
    scratch = scratch_root / "customs-uat" / "r" / f"wheel-{name}-{uuid4().hex[:8]}"
    scratch.mkdir(parents=True, exist_ok=False)
    try:
        build_source = repository
        source_snapshot_sha256 = _source_fingerprint(name, require_clean=True)[
            "working_tree_sha256"
        ]
        if name == "ai_agent":
            build_source, source_snapshot_sha256 = _agent_snapshot_source(repository, scratch)
        built = _run(
            [
                str(_uv()),
                "build",
                "--wheel",
                "--no-sources",
                "--out-dir",
                str(scratch),
                str(build_source),
            ],
            cwd=build_source,
            env=dict(
                os.environ,
                UV_CACHE_DIR=str(CACHE_ROOT / "uv"),
                PYTHONDONTWRITEBYTECODE="1",
            ),
            timeout=600,
            capture=True,
        )
        wheels = sorted(scratch.glob("*.whl"))
        if built.returncode or len(wheels) != 1:
            raise UatError(f"{name} UAT wheel build failed")
        wheel = wheels[0]
        digest = _sha256(wheel)
        installed = _run(
            [
                str(_uv()),
                "pip",
                "install",
                "--python",
                str(python),
                "--no-deps",
                "--reinstall",
                str(wheel),
            ],
            cwd=repository,
            env=dict(os.environ, UV_CACHE_DIR=str(CACHE_ROOT / "uv")),
            timeout=600,
            capture=True,
        )
        if installed.returncode:
            raise UatError(f"{name} UAT wheel installation failed")
        return {"wheel_sha256": digest, "source_snapshot_sha256": source_snapshot_sha256}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _ensure_python_environment(name: str) -> dict[str, Any]:
    python = _external_python(name)
    environment_path = python.parents[1]
    if environment_path.exists() and not python.is_file():
        _remove_managed_environment(environment_path)
    if not python.is_file():
        project = _repo(name)
        environment = dict(os.environ)
        environment.update(
            {
                "UV_PROJECT_ENVIRONMENT": str(environment_path),
                "UV_CACHE_DIR": str(CACHE_ROOT / "uv"),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        arguments = [
            str(_uv()),
            "sync",
            "--locked",
            "--no-editable",
            "--no-dev",
            "--no-install-project",
        ]
        if name == "backend":
            arguments.extend(["--extra", "system", "--extra", "production"])
        elif name == "data_platform":
            arguments.extend(["--extra", "rag"])
        result = _run(arguments, cwd=project, env=environment, timeout=1800)
        if result.returncode or not python.is_file():
            raise UatError(f"{name} UAT environment installation failed")
    source = _source_fingerprint(name, require_clean=True)
    if name == "data_platform":
        # The Data repository intentionally declares `tool.uv.package=false`.
        # Its verified canonical source drives the local read-only runner, while
        # this environment owns dependencies only.
        package = {
            "install_mode": "clean_source",
            "wheel_sha256": None,
            "source_snapshot_sha256": source["working_tree_sha256"],
        }
    else:
        package = {"install_mode": "wheel", **_build_and_install_project_wheel(name, python)}
    _remove_superseded_environments(name, environment_path)
    return {
        "python": str(python),
        "environment": str(environment_path),
        **package,
        "source_fingerprint": source,
    }


def _start_process(
    name: str,
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    source_fingerprint: Mapping[str, Any],
) -> dict[str, Any]:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log = LOG_ROOT / f"{name}.log"
    handle = log.open("w", encoding="utf-8")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
    finally:
        handle.close()
    identity = None
    for _ in range(20):
        identity = _process_identity(process.pid)
        if identity is not None:
            break
        time.sleep(0.05)
    if identity is None:
        process.terminate()
        raise UatError(f"could not bind process identity: {name}")
    return {
        "name": name,
        "pid": process.pid,
        "log": str(log.relative_to(WORKSPACE_ROOT)),
        "executable": identity["executable"],
        "started_at_utc": identity["started_at_utc"],
        "source_fingerprint": str(source_fingerprint["source_fingerprint_sha256"]),
    }


def _docker_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(_dotenv(COMPOSE_ENV))
    frontend = _repo("frontend")
    environment["CUSTOMS_UAT_FRONTEND_CONTEXT"] = str(frontend)
    environment["CUSTOMS_UAT_FRONTEND_REVISION"] = _git(frontend, "rev-parse", "--short=12", "HEAD")
    environment.update(
        {
            "COMPOSE_PROJECT_NAME": "customs-local-user-uat",
            "CUSTOMS_UAT_FRONTEND_CONTEXT": str(_repo("frontend")),
            "CUSTOMS_UAT_FRONTEND_REVISION": _git(_repo("frontend"), "rev-parse", "--short=12", "HEAD"),
            "CUSTOMS_ENHANCED_AGENT_EXPERIENCE": "true",
            "CUSTOMS_UAT_ADAPTIVE_ACTIVE": "true",
        }
    )
    return environment


def _data_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.update({str(key): str(value) for key, value in _read_json(DATA_ENVIRONMENT).items()})
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    return environment


def _compose_up() -> None:
    data_environment = _data_environment()
    data_environment["COMPOSE_PROJECT_NAME"] = "law_repository"
    data_compose = _repo("data_platform") / "law_repository" / "docker-compose.yml"
    result = _run(
        [
            "docker",
            "compose",
            "-p",
            "law_repository",
            "-f",
            str(data_compose),
            "-f",
            str(DATA_COMPOSE_OVERRIDE),
            "up",
            "-d",
        ],
        cwd=data_compose.parent,
        env=data_environment,
        timeout=300,
    )
    if result.returncode:
        raise UatError("three-law Docker stores failed to start")

    docker_environment = _docker_environment()
    tag = f"customs-local-user-uat-frontend:{docker_environment['CUSTOMS_UAT_FRONTEND_REVISION']}"
    inspect = _run(["docker", "image", "inspect", tag], cwd=WORKSPACE_ROOT, capture=True)
    if inspect.returncode:
        build = _run(
            [
                "docker",
                "build",
                "-f",
                str(_repo("frontend") / "deploy" / "uat" / "Dockerfile"),
                "--build-arg",
                "CUSTOMS_ENHANCED_AGENT_EXPERIENCE=true",
                "--build-arg",
                "CUSTOMS_UAT_ADAPTIVE_ACTIVE=true",
                "-t",
                tag,
                str(_repo("frontend")),
            ],
            cwd=_repo("frontend"),
            env=docker_environment,
            timeout=1800,
        )
        if build.returncode:
            raise UatError("Frontend UAT image build failed")
    result = _run(
        ["docker", "compose", "-f", str(COMPOSE), "up", "-d", "--no-build"],
        cwd=ENGINEERING_ROOT,
        env=docker_environment,
        timeout=300,
    )
    if result.returncode:
        raise UatError("UAT infrastructure failed to start")


def _issue_capability(agent_python: Path, evidence_identity: dict[str, Any]) -> dict[str, Any]:
    runtime_sha = evidence_identity.get("runtime_release_v3_sha256")
    if not isinstance(runtime_sha, str) or len(runtime_sha) != 64:
        raise UatError("Evidence runtime identity is invalid")
    model = _dotenv(AGENT_ENV).get("CUSTOMS_AGENT_CLAUDE_MODEL")
    if not model:
        model = _dotenv(SOURCE_SECRET).get("CUSTOMS_AGENT_CLAUDE_MODEL", "claude-sonnet-5")
    issued = dt.datetime.now(dt.UTC).replace(microsecond=0)
    expires = issued + dt.timedelta(hours=23, minutes=55)
    result = _run(
        [
            str(agent_python),
            "-B",
            "-m",
            "tools.issue_local_uat_capability",
            "--output",
            str(AGENT_CAPABILITY),
            "--agent-commit",
            _git(_repo("ai_agent"), "rev-parse", "HEAD"),
            "--model-id",
            model,
            "--evidence-runtime-v3-sha256",
            runtime_sha,
            "--issued-at",
            issued.isoformat().replace("+00:00", "Z"),
            "--expires-at",
            expires.isoformat().replace("+00:00", "Z"),
            "--adaptive-active",
        ],
        cwd=_repo("ai_agent"),
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        timeout=120,
        capture=True,
    )
    if result.returncode:
        raise UatError("Agent UAT capability issuance failed")
    capability = _read_json(AGENT_CAPABILITY)
    capability["file_sha256"] = _sha256(AGENT_CAPABILITY)
    return capability


def _write_agent_manifest(identity: dict[str, Any], capability: dict[str, Any]) -> None:
    trust = _read_json(ASSERTION_TRUST)
    payload = {
        "schema_version": "customs-agent-local-uat-manifest/v3",
        "acknowledgement": "LOCAL_UAT_DIAGNOSTIC_NOT_PRODUCTION",
        "diagnostic_only": True,
        "text_only": True,
        "mock_fallback_allowed": False,
        "auto_completion_allowed": False,
        "evidence_mode": PROFILE,
        "evidence_base_url": "http://127.0.0.1:18013",
        "evidence_runtime_identity": identity,
        "assertion_fixture_mode": "LOOPBACK_TEST_ONLY_HUMAN_APPROVER",
        "assertion_trust_store_path": ASSERTION_TRUST.resolve().as_posix(),
        "assertion_trust_store_sha256": trust["trust_store_sha256"],
        "enhanced_experience_enabled": True,
        "uat_adaptive_active": True,
        "uat_capability_profile_path": AGENT_CAPABILITY.resolve().as_posix(),
        "uat_capability_profile_sha256": capability["file_sha256"],
        "agent_commit": capability["agent_commit"],
    }
    _write_json(AGENT_MANIFEST, payload)


def _required_sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise UatError(f"runtime binding identity is invalid: {label}")
    return value


def _write_runtime_binding(
    *,
    evidence_identity: Mapping[str, Any],
    capability: Mapping[str, Any],
    agent_environment: Mapping[str, Any],
    agent_source: Mapping[str, Any],
    credentials: Mapping[str, Any],
) -> dict[str, Any]:
    code, metadata = _http_json(
        AGENT_METADATA_V3,
        token=str(credentials["agent_service_token"]),
        timeout=30,
    )
    if code != 200:
        raise UatError("Agent V3 metadata is unavailable for runtime binding")
    runtime_release = metadata.get("runtime_release")
    model = metadata.get("model")
    agent_metadata = metadata.get("agent")
    if (
        not isinstance(runtime_release, dict)
        or not isinstance(model, dict)
        or not isinstance(agent_metadata, dict)
    ):
        raise UatError("Agent V3 metadata cannot establish a runtime binding")
    commit = metadata.get("agent_commit")
    if commit != agent_source.get("commit") or capability.get("agent_commit") != commit:
        raise UatError("Agent commit identity drifted before runtime binding")
    code_snapshot = _required_sha256(
        runtime_release.get("agent_code_snapshot_sha256"),
        "agent_code_snapshot_sha256",
    )
    if capability.get("agent_code_snapshot_sha256") != code_snapshot:
        raise UatError("Agent code snapshot drifted before runtime binding")
    source_snapshot = _required_sha256(
        agent_environment.get("source_snapshot_sha256"),
        "agent_source_snapshot_sha256",
    )
    if agent_metadata.get("source_snapshot_sha256") != source_snapshot:
        raise UatError("Agent source snapshot drifted before runtime binding")
    evidence_runtime = _required_sha256(
        evidence_identity.get("runtime_release_v3_sha256"),
        "evidence_runtime_v3_sha256",
    )
    if capability.get("evidence_runtime_v3_sha256") != evidence_runtime:
        raise UatError("Evidence runtime drifted before runtime binding")
    issued_at = capability.get("issued_at")
    expires_at = capability.get("expires_at")
    model_id = model.get("id")
    if not all(isinstance(item, str) and item for item in (issued_at, expires_at, model_id, commit)):
        raise UatError("Agent metadata is incomplete for runtime binding")
    binding: dict[str, Any] = {
        "schema_version": "customs-local-uat-runtime-binding/v1",
        "mode": "development",
        "issued_at_utc": issued_at,
        "expires_at_utc": expires_at,
        "loopback_origin": FRONTEND_ORIGIN,
        "agent": {
            "commit": commit,
            "working_tree_sha256": _required_sha256(
                source_snapshot,
                "agent_working_tree_sha256",
            ),
            "wheel_sha256": _required_sha256(
                agent_environment.get("wheel_sha256"),
                "agent_wheel_sha256",
            ),
            "code_snapshot_sha256": code_snapshot,
            "model_id": model_id,
            "prompt_bundle_sha256": _required_sha256(
                capability.get("prompt_bundle_sha256"),
                "prompt_bundle_sha256",
            ),
            "schema_bundle_sha256": _required_sha256(
                capability.get("schema_bundle_sha256"),
                "schema_bundle_sha256",
            ),
            "tool_registry_id": capability.get("tool_registry_id"),
            "tool_registry_sha256": _required_sha256(
                capability.get("tool_registry_sha256"),
                "tool_registry_sha256",
            ),
            "meaning_catalog_sha256": _required_sha256(
                metadata.get("meaning_catalog_sha256"),
                "meaning_catalog_sha256",
            ),
        },
        "evidence_runtime_v3_sha256": evidence_runtime,
        "capability_sha256": _required_sha256(
            capability.get("capability_sha256"),
            "capability_sha256",
        ),
        "capability_file_sha256": _required_sha256(
            capability.get("file_sha256"),
            "capability_file_sha256",
        ),
        "mock_fallback_allowed": False,
        "auto_completion_allowed": False,
    }
    tool_registry_id = binding["agent"]["tool_registry_id"]
    if not isinstance(tool_registry_id, str) or not tool_registry_id:
        raise UatError("runtime binding identity is invalid: tool_registry_id")
    binding["binding_sha256"] = _canonical_sha256(binding)
    _write_json(RUNTIME_BINDING, binding)
    binding["file_sha256"] = _sha256(RUNTIME_BINDING)
    return binding


def _refresh_requirement_policy_fixture(identity: dict[str, Any]) -> dict[str, Any]:
    payload = _read_json(REQUIREMENT_POLICY)
    runtime_sha = identity.get("runtime_release_v3_sha256")
    required_keys = {
        "schema_version",
        "test_only",
        "production_eligible",
        "evidence_profile",
        "evidence_policy_mode",
        "evidence_runtime_v3_sha256",
        "required_document_code",
        "verification_policy",
        "policy_source_sha256",
        "policy_release",
        "fixture_sha256",
    }
    if (
        set(payload) != required_keys
        or not isinstance(runtime_sha, str)
        or len(runtime_sha) != 64
        or payload.get("test_only") is not True
        or payload.get("production_eligible") is not False
        or payload.get("evidence_profile") != PROFILE
        or payload.get("evidence_policy_mode") != POLICY_MODE
    ):
        raise UatError("test-only requirement policy fixture is invalid")
    payload["evidence_runtime_v3_sha256"] = runtime_sha
    payload.pop("fixture_sha256", None)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload["fixture_sha256"] = hashlib.sha256(canonical).hexdigest()
    _write_json(REQUIREMENT_POLICY, payload)
    return payload


def _backend_environment(
    credentials: dict[str, Any],
    capability: dict[str, Any],
    *,
    role: str,
    runtime_binding: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "CUSTOMS_BACKEND_ENVIRONMENT": "local_uat",
            "CUSTOMS_BACKEND_PROCESS_ROLE": role,
            "CUSTOMS_BACKEND_DATABASE_URL": (
                "postgresql+psycopg://customs_uat:"
                + str(credentials["database_password"])
                + "@127.0.0.1:15432/customs"
            ),
        }
    )
    if role == "migration":
        return environment
    environment["CUSTOMS_BACKEND_REDIS_URL"] = "redis://127.0.0.1:16379/0"
    if role == "outbox":
        return environment
    if runtime_binding is None:
        raise UatError("Backend runtime binding is required")
    policy = _read_json(REQUIREMENT_POLICY)
    trust = _read_json(ASSERTION_TRUST)
    environment.update(
        {
            "CUSTOMS_BACKEND_PUBLIC_BASE_URL": FRONTEND_ORIGIN,
            "CUSTOMS_BACKEND_OIDC_ISSUER": f"{FRONTEND_ORIGIN}/oidc/realms/customs",
            "CUSTOMS_BACKEND_OIDC_AUDIENCE": "customs-workbench-api",
            "CUSTOMS_BACKEND_OIDC_JWKS_URL": f"{FRONTEND_ORIGIN}/oidc/realms/customs/protocol/openid-connect/certs",
            "CUSTOMS_BACKEND_OIDC_CLIENT_ID": "customs-workbench-spa",
            "CUSTOMS_BACKEND_OIDC_AUTHORIZATION_ENDPOINT": f"{FRONTEND_ORIGIN}/oidc/realms/customs/protocol/openid-connect/auth",
            "CUSTOMS_BACKEND_OIDC_TOKEN_ENDPOINT": f"{FRONTEND_ORIGIN}/oidc/realms/customs/protocol/openid-connect/token",
            "CUSTOMS_BACKEND_OIDC_END_SESSION_ENDPOINT": f"{FRONTEND_ORIGIN}/oidc/realms/customs/protocol/openid-connect/logout",
            "CUSTOMS_BACKEND_AGENT_BASE_URL": "http://127.0.0.1:18080",
            "CUSTOMS_BACKEND_AGENT_SERVICE_TOKEN": str(credentials["agent_service_token"]),
            "CUSTOMS_BACKEND_AGENT_TIMEOUT_SECONDS": "810",
            "CUSTOMS_BACKEND_REVIEW_ACTOR_TIME_LIMIT_SECONDS": "840",
            "CUSTOMS_BACKEND_JOB_LEASE_SECONDS": "900",
            "CUSTOMS_BACKEND_MINIO_ENDPOINT": "127.0.0.1:19000",
            "CUSTOMS_BACKEND_MINIO_ACCESS_KEY": str(credentials["minio_access_key"]),
            "CUSTOMS_BACKEND_MINIO_SECRET_KEY": str(credentials["minio_secret_key"]),
            "CUSTOMS_BACKEND_MINIO_BUCKET": "customs-uat-documents",
            "CUSTOMS_BACKEND_MINIO_SECURE": "false",
            "CUSTOMS_BACKEND_UAT_EVIDENCE_PROFILE": PROFILE,
            "CUSTOMS_BACKEND_UAT_EVIDENCE_POLICY_MODE": POLICY_MODE,
            "CUSTOMS_BACKEND_UAT_EVIDENCE_DATA_SOURCE": "ACTUAL_POSTGRESQL_QDRANT_NEO4J_READ_ONLY",
            "CUSTOMS_BACKEND_UAT_EVIDENCE_EXPECTED_PRODUCTION_LAW_COUNT": "58",
            "CUSTOMS_BACKEND_UAT_EVIDENCE_OBSERVED_LAW_COUNT": "3",
            "CUSTOMS_BACKEND_UAT_EVIDENCE_OBSERVED_ARTICLE_COUNT": "1007",
            "CUSTOMS_BACKEND_UAT_EVIDENCE_QDRANT_POINT_COUNT": "1007",
            "CUSTOMS_BACKEND_UAT_EVIDENCE_NEO4J_ARTICLE_COUNT": "1007",
            "CUSTOMS_BACKEND_UAT_EVIDENCE_RAG_QUALITY_EVALUATED": "false",
            "CUSTOMS_BACKEND_UAT_PLANNER_MODE": "diagnostic_active",
            "CUSTOMS_BACKEND_UAT_CLAUDE_MODE": "actual",
            "CUSTOMS_BACKEND_AUTO_COMPLETION_ENABLED": "false",
            "CUSTOMS_BACKEND_ENHANCED_AGENT_EXPERIENCE": "true",
            "CUSTOMS_BACKEND_UAT_ADAPTIVE_ACTIVE": "true",
            "CUSTOMS_BACKEND_UAT_EXPECTED_CAPABILITY_SHA256": str(capability["capability_sha256"]),
            "CUSTOMS_BACKEND_UAT_EXPECTED_CAPABILITY_FILE_SHA256": str(capability["file_sha256"]),
            "CUSTOMS_BACKEND_UAT_ASSERTION_SIGNING_KEY_REGISTRY_PATH": ASSERTION_SIGNING.resolve().as_posix(),
            "CUSTOMS_BACKEND_UAT_ASSERTION_TRUST_STORE_SHA256": str(trust["trust_store_sha256"]),
            "CUSTOMS_BACKEND_UAT_REQUIREMENT_POLICY_FIXTURE_PATH": REQUIREMENT_POLICY.resolve().as_posix(),
            "CUSTOMS_BACKEND_UAT_REQUIREMENT_POLICY_FIXTURE_SHA256": str(policy["fixture_sha256"]),
            "CUSTOMS_BACKEND_UAT_EVIDENCE_RUNTIME_V3_SHA256": str(policy["evidence_runtime_v3_sha256"]),
            "CUSTOMS_BACKEND_UAT_EVIDENCE_TRACE_PATH": DATA_TRACE.resolve().as_posix(),
            "CUSTOMS_BACKEND_UAT_RUNTIME_BINDING_PATH": RUNTIME_BINDING.resolve().as_posix(),
            "CUSTOMS_BACKEND_UAT_RUNTIME_BINDING_SHA256": str(runtime_binding["file_sha256"]),
        }
    )
    return environment


def _parse_utc(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _capability_is_current(state: Mapping[str, Any], sources: Mapping[str, Any]) -> bool:
    if not AGENT_CAPABILITY.is_file():
        return False
    try:
        capability = _read_json(AGENT_CAPABILITY)
    except UatError:
        return False
    expires = _parse_utc(capability.get("expires_at"))
    agent = sources.get("ai_agent")
    return bool(
        expires
        and expires > dt.datetime.now(dt.UTC) + dt.timedelta(minutes=2)
        and isinstance(agent, dict)
        and capability.get("agent_commit") == agent.get("commit")
        and capability.get("capability_sha256") == state.get("capability_sha256")
    )


def status() -> dict[str, Any]:
    state = _state()
    if not state:
        return {
            "status": "IDLE",
            "url": FRONTEND_ORIGIN,
            "process_count": 0,
            "not_ready_reasons": ["state_absent"],
            "source_identity_match": False,
            "recommended_action": "up",
        }
    raw_records = state.get("processes", [])
    process_records = [item for item in raw_records if isinstance(item, dict)] if isinstance(raw_records, list) else []
    records_by_name = {str(item.get("name")): item for item in process_records}
    process_states: dict[str, dict[str, Any]] = {}
    for name in PROCESS_ORDER:
        record = records_by_name.get(name)
        process_states[name] = {
            "present": record is not None,
            "identity_match": bool(record and _record_matches_process(record)),
        }
    try:
        current_sources = _source_fingerprints(require_clean=False)
    except UatError:
        current_sources = {}
    expected_sources = state.get("source_fingerprints")
    source_identity_match = bool(
        isinstance(expected_sources, dict)
        and current_sources
        and expected_sources == current_sources
        and not any(bool(item.get("dirty")) for item in current_sources.values())
    )
    data_code, _ = _http_json(DATA_LIVE, timeout=5)
    agent_code, _ = _http_json(AGENT_LIVE, timeout=5)
    backend_code, _ = _http_json(BACKEND_LIVE, timeout=5)
    front_code, _ = _http_json(f"{FRONTEND_ORIGIN}/healthz", timeout=10)
    manifest = _read_json(DATA_MANIFEST) if DATA_MANIFEST.is_file() else {}
    law_count = manifest.get("observed_target_law_count")
    article_count = manifest.get("observed_article_count")
    mock_fallback_allowed = state.get("mock_fallback_allowed", False)
    auto_completion_enabled = state.get("auto_completion_enabled", False)
    capability_valid = _capability_is_current(state, current_sources)
    binding_match = bool(
        RUNTIME_BINDING.is_file()
        and state.get("runtime_binding_file_sha256") == _sha256(RUNTIME_BINDING)
    )
    reasons: list[str] = []
    for name, value in process_states.items():
        if not value["identity_match"]:
            reasons.append(f"process_{name}_unavailable_or_stale")
    for name, code in {
        "data": data_code,
        "agent": agent_code,
        "backend": backend_code,
        "frontend": front_code,
    }.items():
        if code != 200:
            reasons.append(f"component_{name}_not_live")
    if law_count != 3 or article_count != 1007:
        reasons.append("three_law_manifest_mismatch")
    if not source_identity_match:
        reasons.append("source_identity_mismatch")
    if not capability_valid:
        reasons.append("capability_invalid_or_expiring")
    if not binding_match:
        reasons.append("runtime_binding_mismatch")
    if mock_fallback_allowed is not False:
        reasons.append("mock_fallback_policy_invalid")
    if auto_completion_enabled is not False:
        reasons.append("auto_completion_policy_invalid")
    ready = not reasons
    if ready:
        recommended_action = "none"
    elif reasons == ["component_frontend_not_live"]:
        recommended_action = "up"
    else:
        recommended_action = "restart"
    return {
        "status": "READY" if ready else "NOT_READY",
        "url": FRONTEND_ORIGIN,
        "evidence_profile": PROFILE,
        "actual_law_count": law_count,
        "article_count": article_count,
        "mock_fallback_allowed": mock_fallback_allowed,
        "auto_completion_enabled": auto_completion_enabled,
        "process_count": len(process_records),
        "processes_by_name": process_states,
        "components": {
            "data": data_code,
            "agent": agent_code,
            "backend": backend_code,
            "frontend": front_code,
        },
        "source_identity_match": source_identity_match,
        "capability_valid": capability_valid,
        "runtime_binding_match": binding_match,
        "not_ready_reasons": reasons,
        "recommended_action": recommended_action,
    }


def _stop_recorded_processes(names: set[str] | None = None) -> list[dict[str, Any]]:
    state = _state()
    records = state.get("processes", []) if isinstance(state, dict) else []
    if not isinstance(records, list):
        return []
    retained: list[dict[str, Any]] = []
    for record in reversed(records):
        if not isinstance(record, dict):
            continue
        name = str(record.get("name", ""))
        if names is not None and name not in names:
            retained.insert(0, record)
            continue
        pid = int(record.get("pid", 0))
        if not _process_alive(pid):
            continue
        if not _record_matches_process(record):
            raise UatError(f"refusing to stop a stale or reused PID: {name}")
        command = ["taskkill", "/PID", str(pid), "/T", "/F"] if os.name == "nt" else ["kill", str(pid)]
        _run(command, cwd=WORKSPACE_ROOT, timeout=30, capture=True)
    return retained


def _adopt_legacy_process_state(
    state: dict[str, Any],
    sources: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if state.get("schema_version") != "customs-local-user-uat-state/v2":
        return state
    repositories = state.get("repositories")
    records = state.get("processes")
    started = _parse_utc(state.get("started_at_utc"))
    if (
        not isinstance(repositories, dict)
        or not isinstance(records, list)
        or started is None
        or any(
            not isinstance(repositories.get(name), str)
            or len(str(repositories[name])) != 40
            or any(character not in "0123456789abcdef" for character in str(repositories[name]))
            for name in sources
        )
    ):
        raise UatError("legacy UAT state cannot be safely adopted")
    upgraded: list[dict[str, Any]] = []
    for raw in records:
        if not isinstance(raw, dict) or str(raw.get("name")) not in PROCESS_REPOSITORIES:
            raise UatError("legacy UAT process state is invalid")
        name = str(raw["name"])
        identity = _process_identity(int(raw.get("pid", 0)))
        repository_name = PROCESS_REPOSITORIES[name]
        if identity is None:
            raise UatError("legacy UAT process is no longer identifiable")
        executable = Path(identity["executable"]).resolve()
        repository_env_root = (ENV_ROOT / repository_name).resolve()
        observed_started = _parse_utc(identity["started_at_utc"])
        command_line = _process_command_line(int(raw.get("pid", 0)))
        markers = LEGACY_PROCESS_COMMAND_MARKERS[name]
        if (
            repository_env_root not in executable.parents
            or executable.name.casefold() not in {"python", "python.exe"}
            or observed_started is None
            or not isinstance(command_line, str)
            or any(marker not in command_line for marker in markers)
        ):
            raise UatError("legacy UAT PID identity cannot be proven")
        upgraded.append(
            {
                **raw,
                "executable": identity["executable"],
                "started_at_utc": identity["started_at_utc"],
                "source_fingerprint": sources[repository_name]["source_fingerprint_sha256"],
            }
        )
    state.update(
        {
            "schema_version": "customs-local-user-uat-state/v3",
            "processes": upgraded,
            "source_fingerprints": dict(sources),
            "runtime_environments": {},
        }
    )
    _write_json(STATE_FILE, state)
    return state


def _runtime_environment(
    name: str,
    state: Mapping[str, Any],
    source: Mapping[str, Any],
) -> dict[str, Any]:
    records = state.get("runtime_environments")
    record = records.get(name) if isinstance(records, dict) else None
    expected_python = _external_python(name)
    if not isinstance(record, dict):
        package_identity_valid = False
    elif name == "data_platform":
        package_identity_valid = (
            record.get("install_mode") == "clean_source"
            and record.get("wheel_sha256") is None
        )
    else:
        package_identity_valid = (
            record.get("install_mode") == "wheel"
            and isinstance(record.get("wheel_sha256"), str)
            and len(str(record["wheel_sha256"])) == 64
        )
    if (
        isinstance(record, dict)
        and record.get("source_fingerprint") == source
        and record.get("python") == str(expected_python)
        and expected_python.is_file()
        and package_identity_valid
        and isinstance(record.get("source_snapshot_sha256"), str)
        and len(str(record["source_snapshot_sha256"])) == 64
    ):
        return dict(record)
    return _ensure_python_environment(name)


def _reconcile_scope(
    state: Mapping[str, Any],
    observed: Mapping[str, Any],
    sources: Mapping[str, Mapping[str, Any]],
    *,
    force_restart: bool,
) -> str:
    if force_restart or not state:
        return "data"
    expected = state.get("source_fingerprints")
    if not isinstance(expected, dict):
        return "data"
    changed = {name for name, value in sources.items() if expected.get(name) != value}
    environments = state.get("runtime_environments")
    runtime_environments = environments if isinstance(environments, dict) else {}
    process_states = observed.get("processes_by_name")
    processes = process_states if isinstance(process_states, dict) else {}
    components = observed.get("components")
    component_codes = components if isinstance(components, dict) else {}
    if (
        "data_platform" in changed
        or not isinstance(runtime_environments.get("data_platform"), dict)
        or component_codes.get("data") != 200
        or not bool(processes.get("data", {}).get("identity_match"))
        or observed.get("actual_law_count") != 3
        or observed.get("article_count") != 1007
    ):
        return "data"
    if (
        "ai_agent" in changed
        or not isinstance(runtime_environments.get("ai_agent"), dict)
        or component_codes.get("agent") != 200
        or not bool(processes.get("agent", {}).get("identity_match"))
        or observed.get("capability_valid") is not True
    ):
        return "agent"
    if (
        "backend" in changed
        or not isinstance(runtime_environments.get("backend"), dict)
        or component_codes.get("backend") != 200
        or observed.get("runtime_binding_match") is not True
        or any(
            not bool(processes.get(name, {}).get("identity_match"))
            for name in ("backend", "worker", "outbox")
        )
    ):
        return "backend"
    if "frontend" in changed or component_codes.get("frontend") != 200:
        return "frontend"
    return "data"


def up(*, force_restart: bool = False) -> dict[str, Any]:
    current = status()
    if current["status"] == "READY" and not force_restart:
        return current
    required = (
        DATA_CONTEXT,
        DATA_ENVIRONMENT,
        CREDENTIALS,
        AGENT_ENV,
        ASSERTION_TRUST,
        ASSERTION_SIGNING,
        REQUIREMENT_POLICY,
        COMPOSE_ENV,
        REALM_FILE,
        DATA_TOKEN,
    )
    if any(not path.exists() for path in required):
        raise UatError("portable UAT configuration is incomplete")
    sources = _source_fingerprints(require_clean=True)
    previous_state = _adopt_legacy_process_state(_state(), sources)
    if previous_state:
        current = status()
    scope = _reconcile_scope(previous_state, current, sources, force_restart=force_restart)
    _compose_up()
    selected = set(DEPENDENCY_SCOPES[scope])
    retained = _stop_recorded_processes(selected) if selected else [
        item
        for item in previous_state.get("processes", [])
        if isinstance(item, dict)
    ]
    if "backend" in selected:
        _assert_backend_listener_available(previous_state)
    runtime_environments = dict(previous_state.get("runtime_environments", {}))
    for name in ("data_platform", "ai_agent", "backend"):
        process_name = {"data_platform": "data", "ai_agent": "agent", "backend": "backend"}[name]
        if process_name in selected:
            runtime_environments[name] = _runtime_environment(name, previous_state, sources[name])
    processes: list[dict[str, Any]] = list(retained)
    try:
        credentials = _read_json(CREDENTIALS)
        if "data" in selected:
            data_python = Path(str(runtime_environments["data_platform"]["python"]))
            for generated_output in (DATA_IDENTITY, DATA_MANIFEST, DATA_TRACE):
                generated_output.unlink(missing_ok=True)
            processes.append(
                _start_process(
                    "data",
                    [
                        str(data_python),
                        "-B",
                        str(_repo("data_platform") / "tools" / "run_three_law_v3_scenario_eval.py"),
                        "--scenario-eval-only-three-law",
                        "--provider-context",
                        str(DATA_CONTEXT),
                        "--service-token-file",
                        str(DATA_TOKEN),
                        "--identity-output",
                        str(DATA_IDENTITY),
                        "--scenario-manifest-output",
                        str(DATA_MANIFEST),
                        "--trace-output",
                        str(DATA_TRACE),
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "18013",
                    ],
                    cwd=_repo("data_platform"),
                    env=_data_environment(),
                    source_fingerprint=sources["data_platform"],
                )
            )
        _wait_json(
            DATA_READY,
            token=str(credentials["evidence_service_token"]),
            timeout=int(_config()["limits"]["actual_three_law_data_startup_timeout_seconds"]),
        )
        identity = _read_json(DATA_IDENTITY)
        _refresh_requirement_policy_fixture(identity)
        agent_environment = runtime_environments.get("ai_agent")
        if not isinstance(agent_environment, dict):
            raise UatError("Agent runtime environment identity is unavailable")
        agent_python = Path(str(agent_environment["python"]))
        if "agent" in selected:
            capability = _issue_capability(agent_python, identity)
            _write_agent_manifest(identity, capability)
            processes.append(
                _start_process(
                    "agent",
                    [
                        str(agent_python),
                        "-B",
                        "-m",
                        "tools.local_uat_agent_service",
                        "--manifest",
                        str(AGENT_MANIFEST),
                        "--env-file",
                        str(AGENT_ENV),
                        "--acknowledge-local-uat",
                    ],
                    cwd=_repo("ai_agent"),
                    env=dict(
                        os.environ,
                        PYTHONDONTWRITEBYTECODE="1",
                        CUSTOMS_AGENT_RUN_TIMEOUT_SECONDS="780",
                        CUSTOMS_AGENT_BUILD_SHA=str(sources["ai_agent"]["commit"]),
                        CUSTOMS_AGENT_WHEEL_SHA256=str(
                            agent_environment["wheel_sha256"]
                        ),
                    ),
                    source_fingerprint=sources["ai_agent"],
                )
            )
        else:
            capability = _read_json(AGENT_CAPABILITY)
            capability["file_sha256"] = _sha256(AGENT_CAPABILITY)
        _wait_json(AGENT_READY, timeout=180)
        if "backend" in selected:
            binding = _write_runtime_binding(
                evidence_identity=identity,
                capability=capability,
                agent_environment=agent_environment,
                agent_source=sources["ai_agent"],
                credentials=credentials,
            )
            backend_environment = runtime_environments.get("backend")
            if not isinstance(backend_environment, dict):
                raise UatError("Backend runtime environment identity is unavailable")
            backend_python = Path(str(backend_environment["python"]))
            expected_sources = previous_state.get("source_fingerprints", {})
            if not previous_state or not isinstance(expected_sources, dict) or expected_sources.get("backend") != sources["backend"]:
                migration = _run(
                    [str(backend_python), "-B", "-m", "alembic", "upgrade", "head"],
                    cwd=_repo("backend"),
                    env=_backend_environment(credentials, capability, role="migration"),
                    timeout=180,
                )
                if migration.returncode:
                    raise UatError("Backend migration failed")
            runtime_environment = _backend_environment(
                credentials,
                capability,
                role="runtime",
                runtime_binding=binding,
            )
            processes.extend(
                [
                    _start_process(
                        "backend",
                        [
                            str(backend_python), "-B", "-m", "uvicorn", "customs_backend.main:app",
                            "--host", "127.0.0.1", "--port", "18081", "--no-access-log", "--no-server-header",
                        ],
                        cwd=_repo("backend"), env=runtime_environment,
                        source_fingerprint=sources["backend"],
                    ),
                    _start_process(
                        "worker",
                        [
                            str(backend_python), "-B", "-m", "dramatiq", "customs_backend.worker",
                            "--processes", "1", "--threads", "2",
                        ],
                        cwd=_repo("backend"), env=runtime_environment,
                        source_fingerprint=sources["backend"],
                    ),
                    _start_process(
                        "outbox",
                        [
                            str(backend_python), "-B", "-c",
                            "from customs_backend.outbox_dispatcher import main; main()",
                        ],
                        cwd=_repo("backend"),
                        env=_backend_environment(credentials, capability, role="outbox"),
                        source_fingerprint=sources["backend"],
                    ),
                ]
            )
        else:
            binding = _read_json(RUNTIME_BINDING)
            binding["file_sha256"] = _sha256(RUNTIME_BINDING)
        processes.sort(key=lambda item: PROCESS_ORDER.index(str(item["name"])))
        _write_json(
            STATE_FILE,
            {
                "schema_version": "customs-local-user-uat-state/v3",
                "status": "running",
                "started_at_utc": previous_state.get("started_at_utc")
                or dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                "processes": processes,
                "source_fingerprints": sources,
                "runtime_environments": runtime_environments,
                "evidence_profile": PROFILE,
                "evidence_runtime_v3_sha256": identity.get("runtime_release_v3_sha256"),
                "capability_sha256": capability.get("capability_sha256"),
                "runtime_binding_file_sha256": binding["file_sha256"],
                "production_release": False,
                "mock_fallback_allowed": False,
                "auto_completion_enabled": False,
            },
        )
        _wait_json(BACKEND_READY, timeout=240)
        _wait_for_backend_group_identity(
            processes,
            str(sources["backend"]["source_fingerprint_sha256"]),
        )
        final = status()
        if final["status"] != "READY":
            raise UatError("UAT startup completed without a READY boundary")
        return final
    except BaseException:
        # Preserve an already healthy upstream boundary.  A downstream Agent
        # or Backend configuration failure must not force the expensive Data
        # models to reload from the beginning on the next reconcile.
        live_codes = {
            "data": _http_json(DATA_LIVE, timeout=5)[0],
            "agent": _http_json(AGENT_LIVE, timeout=5)[0],
        }
        keep_names = {
            name
            for name, code in live_codes.items()
            if code == 200
            and any(
                str(item.get("name")) == name and _record_matches_process(item)
                for item in processes
            )
        }
        failure_state = {
            "schema_version": "customs-local-user-uat-state/v3",
            "processes": processes,
            "source_fingerprints": sources,
            "runtime_environments": runtime_environments,
            "evidence_runtime_v3_sha256": (
                identity.get("runtime_release_v3_sha256")
                if isinstance(locals().get("identity"), dict)
                else None
            ),
            "capability_sha256": (
                capability.get("capability_sha256")
                if isinstance(locals().get("capability"), dict)
                else None
            ),
            "runtime_binding_file_sha256": (
                _sha256(RUNTIME_BINDING) if RUNTIME_BINDING.is_file() else None
            ),
            "mock_fallback_allowed": False,
            "auto_completion_enabled": False,
        }
        _write_json(STATE_FILE, failure_state)
        stopped_names = {str(item["name"]) for item in processes} - keep_names
        preserved = _stop_recorded_processes(stopped_names)
        _write_json(STATE_FILE, {**failure_state, "processes": preserved})
        raise


def down() -> dict[str, Any]:
    previous_state = _state()
    if previous_state.get("schema_version") == "customs-local-user-uat-state/v2":
        previous_state = _adopt_legacy_process_state(
            previous_state,
            _source_fingerprints(require_clean=False),
        )
    _stop_recorded_processes()
    docker_environment = _docker_environment() if COMPOSE_ENV.is_file() else dict(os.environ)
    _run(
        ["docker", "compose", "-f", str(COMPOSE), "down", "--remove-orphans"],
        cwd=ENGINEERING_ROOT,
        env=docker_environment,
        timeout=180,
        capture=True,
    )
    data_compose = _repo("data_platform") / "law_repository" / "docker-compose.yml"
    data_environment = _data_environment() if DATA_ENVIRONMENT.is_file() else dict(os.environ)
    _run(
        [
            "docker",
            "compose",
            "-p",
            "law_repository",
            "-f",
            str(data_compose),
            "-f",
            str(DATA_COMPOSE_OVERRIDE),
            "down",
            "--remove-orphans",
        ],
        cwd=data_compose.parent,
        env=data_environment,
        timeout=180,
        capture=True,
    )
    if previous_state:
        previous_state.update({"status": "stopped", "processes": []})
        _write_json(STATE_FILE, previous_state)
    return {
        "status": "STOPPED",
        "volumes_preserved": True,
        "runtime_environments_removed": False,
        "runtime_environments_preserved": True,
    }


def hibernate() -> dict[str, Any]:
    stopped = down()
    _remove_managed_environment(ENV_ROOT, allow_root=True)
    STATE_FILE.unlink(missing_ok=True)
    RUNTIME_BINDING.unlink(missing_ok=True)
    return {
        **stopped,
        "status": "HIBERNATED",
        "runtime_environments_removed": True,
        "runtime_environments_preserved": False,
    }


def restart() -> dict[str, Any]:
    return up(force_restart=True)


def _npm() -> Path:
    managed = CACHE_ROOT / "tools" / "node" / "v22.23.1" / "npm.cmd"
    if managed.is_file():
        return managed
    located = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if located:
        return Path(located)
    raise UatError("npm is unavailable")


def _frontend_dependency_root() -> Path:
    lock_hash = _sha256(_repo("frontend") / "package-lock.json")[:16]
    return ENV_ROOT / "frontend" / f"{lock_hash}-browser"


def _ensure_frontend_dependencies() -> Path:
    frontend = _repo("frontend")
    root = _frontend_dependency_root()
    node_modules = root / "node_modules"
    marker = root / ".customs-uat-dependencies.json"
    lock_sha256 = _sha256(frontend / "package-lock.json")
    if node_modules.is_dir() and marker.is_file():
        try:
            value = _read_json(marker)
        except UatError:
            value = {}
        if value.get("package_lock_sha256") == lock_sha256:
            _remove_superseded_environments("frontend", root)
            return node_modules
    if root.exists():
        _remove_managed_environment(root)
    (root / "scripts").mkdir(parents=True, exist_ok=False)
    shutil.copy2(frontend / "package.json", root / "package.json")
    shutil.copy2(frontend / "package-lock.json", root / "package-lock.json")
    shutil.copy2(
        frontend / "scripts" / "patch-redocly-minimatch.mjs",
        root / "scripts" / "patch-redocly-minimatch.mjs",
    )
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": str(_npm().parent) + os.pathsep + environment.get("PATH", ""),
            "npm_config_cache": str(CACHE_ROOT / "npm"),
        }
    )
    installed = _run(
        [str(_npm()), "ci", "--prefer-offline"],
        cwd=root,
        env=environment,
        timeout=900,
        capture=True,
    )
    if installed.returncode or not node_modules.is_dir():
        _remove_managed_environment(root)
        raise UatError("candidate Browser dependencies could not be installed")
    _write_json(marker, {"package_lock_sha256": lock_sha256})
    _remove_superseded_environments("frontend", root)
    return node_modules


def _is_reparse_point(path: Path) -> bool:
    try:
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return path.is_symlink()


def _link_frontend_dependencies(target: Path) -> Path:
    link = _repo("frontend") / "node_modules"
    if link.exists() or _is_reparse_point(link):
        raise UatError("repository node_modules must be absent before candidate execution")
    if os.name == "nt":
        created = _run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            cwd=link.parent,
            capture=True,
            timeout=30,
        )
        if created.returncode or not _is_reparse_point(link):
            raise UatError("frontend dependency junction could not be created")
    else:
        link.symlink_to(target, target_is_directory=True)
    return link


def _unlink_frontend_dependencies(link: Path) -> None:
    if not _is_reparse_point(link):
        if link.exists():
            raise UatError("refusing to remove a non-managed node_modules directory")
        return
    if os.name == "nt":
        os.rmdir(link)
    else:
        link.unlink()


def smoke() -> dict[str, Any]:
    ready = status()
    if ready["status"] != "READY":
        raise UatError("LOCAL UAT is not ready; run `eng uat up` first")
    fixture = (
        _repo("ai_agent")
        / "tools"
        / "agent_execution_inspector"
        / "clean_text_production_candidate_v3.request.json"
    )
    payload = _read_json(fixture)
    identifier = uuid4().hex[:16]
    payload["case_id"] = f"uat-smoke-case-{identifier}"
    payload["review_run_id"] = f"uat-smoke-run-{identifier}"
    credentials = _read_json(CREDENTIALS)
    started = time.monotonic()
    code, envelope = _post_json(
        AGENT_REVIEW_V3,
        payload,
        token=str(credentials["agent_service_token"]),
        timeout=int(_config()["limits"]["claude_review_timeout_seconds"]),
    )
    if code != 200:
        raise UatError(f"Agent smoke review failed before a verified result: HTTP {code}")
    canonical = envelope.get("canonical_result")
    presentation = envelope.get("user_presentation")
    capability = envelope.get("capability_attestation")
    if not isinstance(canonical, dict) or not isinstance(presentation, dict) or not isinstance(capability, dict):
        raise UatError("Agent smoke response is not a complete experience envelope")
    expected_sections = [
        "conclusion",
        "process",
        "document_comparison",
        "issues_and_causes",
        "regulatory_meaning",
        "business_impact",
        "next_actions",
        "scope_and_limits",
    ]
    sections = presentation.get("sections")
    section_codes = [item.get("section_code") for item in sections if isinstance(item, dict)] if isinstance(sections, list) else []
    generation = presentation.get("generation_audit")
    verification = presentation.get("verification_audit")
    report_mode = generation.get("mode") if isinstance(generation, dict) else None
    verification_status = (
        verification.get("status") if isinstance(verification, dict) else None
    )
    safe_report_completed = (report_mode, verification_status) in {
        ("claude", "passed"),
        ("deterministic_fallback", "fallback"),
    }
    runtime_release = canonical.get("runtime_release")
    model_calls = canonical.get("model_calls")
    rag_evidence = canonical.get("rag_evidence")
    observed_methods = (
        {
            method
            for item in rag_evidence
            if isinstance(item, dict)
            for method in item.get("retrieval_methods", [])
            if isinstance(method, str)
        }
        if isinstance(rag_evidence, list)
        else set()
    )
    evidence_sha = _state().get("evidence_runtime_v3_sha256")
    conditions = (
        canonical.get("retrieval_status") == "ok",
        canonical.get("retrieval_coverage") == "complete",
        canonical.get("review_disposition") != "auto_review_complete",
        section_codes == expected_sections,
        safe_report_completed,
        isinstance(model_calls, list)
        and len(model_calls) == 4
        and all(isinstance(item, dict) and item.get("provider") == "anthropic" for item in model_calls),
        isinstance(rag_evidence, list)
        and all(
            isinstance(item, dict)
            and "mock" not in item.get("retrieval_methods", [])
            for item in rag_evidence
        ),
        bool(observed_methods & {"exact_hs", "dense", "bm25", "graph"}),
        isinstance(runtime_release, dict)
        and runtime_release.get("evidence_runtime_v3_sha256") == evidence_sha,
        capability.get("auto_completion_enabled") is False,
        _state().get("mock_fallback_allowed") is False,
    )
    if not all(conditions):
        raise UatError("Agent smoke result did not satisfy the bounded live criteria")
    return {
        "status": "PASS",
        "review_run_id": str(canonical.get("review_run_id")),
        "result_sha256": str(canonical.get("result_sha256")),
        "envelope_sha256": str(envelope.get("envelope_sha256")),
        "actual_claude": True,
        "actual_three_law_rag": True,
        "report_section_count": 8,
        "report_generation_mode": report_mode,
        "mock_fallback_count": 0,
        "auto_completion_count": 0,
        "duration_seconds": round(time.monotonic() - started, 3),
        "production_release": False,
    }


def _functional_identity_sha256() -> str:
    state = _state()
    runtime_binding = _read_json(RUNTIME_BINDING)
    agent_binding = runtime_binding.get("agent")
    if not isinstance(agent_binding, dict):
        raise UatError("candidate runtime binding is incomplete")
    payload = {
        "source_fingerprints": state.get("source_fingerprints"),
        # Issuance, expiry, capability signature, and the binding file hash are
        # intentionally excluded.  They rotate when the same functional UAT is
        # restarted and must not invalidate a safe Browser resume checkpoint.
        "runtime_binding": {
            "schema_version": runtime_binding.get("schema_version"),
            "mode": runtime_binding.get("mode"),
            "loopback_origin": runtime_binding.get("loopback_origin"),
            "agent": agent_binding,
            "evidence_runtime_v3_sha256": runtime_binding.get(
                "evidence_runtime_v3_sha256"
            ),
            "mock_fallback_allowed": runtime_binding.get("mock_fallback_allowed"),
            "auto_completion_allowed": runtime_binding.get("auto_completion_allowed"),
        },
        "candidate_spec_sha256": _sha256(
            _repo("frontend") / "e2e" / "commercial-workbench.spec.ts"
        ),
        "backend_agent_contract_lock_sha256": _sha256(
            _repo("backend") / "src" / "customs_backend" / "agent_experience_contract.lock.json"
        ),
    }
    return _canonical_sha256(payload)


def _prepare_candidate_checkpoints(
    functional_identity: str,
    environment: dict[str, str],
) -> str:
    checkpoint = _read_json(CANDIDATE_CHECKPOINT) if CANDIDATE_CHECKPOINT.is_file() else {}
    same_identity = checkpoint.get("functional_identity_sha256") == functional_identity
    if not same_identity:
        CANDIDATE_BROWSER_CHECKPOINT.unlink(missing_ok=True)
    candidate_id = (
        checkpoint.get("candidate_id")
        if same_identity and isinstance(checkpoint.get("candidate_id"), str)
        else f"candidate-{uuid4().hex}"
    )
    _write_json(
        CANDIDATE_CHECKPOINT,
        {
            "candidate_id": candidate_id,
            "functional_identity_sha256": functional_identity,
        },
    )
    environment.update(
        {
            "CUSTOMS_E2E_CANDIDATE_ID": str(candidate_id),
            "CUSTOMS_CANDIDATE_STATE_FILE": str(
                CANDIDATE_BROWSER_CHECKPOINT.resolve()
            ),
            "CUSTOMS_CANDIDATE_IDENTITY_SHA256": functional_identity,
        }
    )
    return str(candidate_id)


def _clear_candidate_checkpoints() -> None:
    CANDIDATE_CHECKPOINT.unlink(missing_ok=True)
    CANDIDATE_BROWSER_CHECKPOINT.unlink(missing_ok=True)


def _validate_candidate_static_pin() -> None:
    backend = _repo("backend")
    lock = _read_json(backend / "src" / "customs_backend" / "agent_experience_contract.lock.json")
    agent_commit = _git(_repo("ai_agent"), "rev-parse", "HEAD")
    if lock.get("provider_commit") != agent_commit:
        raise UatError("Backend Agent provider commit is stale")
    state = _state()
    environments = state.get("runtime_environments")
    backend_environment = environments.get("backend") if isinstance(environments, dict) else None
    python = Path(str(backend_environment.get("python"))) if isinstance(backend_environment, dict) else None
    if python is None or not python.is_file():
        raise UatError("Backend contract validator environment is unavailable")
    verified = _run(
        [str(python), "-B", "tools/verify_contract_snapshots.py"],
        cwd=backend,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        timeout=180,
        capture=True,
    )
    if verified.returncode:
        raise UatError("Backend Agent experience contract validation failed")


def candidate() -> dict[str, Any]:
    ready = status()
    if ready["status"] != "READY":
        raise UatError("LOCAL UAT is not ready; run `eng uat up` first")
    _validate_candidate_static_pin()
    frontend = _repo("frontend")
    environment = dict(os.environ)
    environment.update(
        {
            "CUSTOMS_E2E_LIVE": "true",
            "CUSTOMS_UAT_BASE_URL": FRONTEND_ORIGIN,
            "CUSTOMS_E2E_CREDENTIALS_FILE": str(CREDENTIALS.resolve()),
            # Keep the Browser runner from competing with the resident dense
            # and reranker models for the entire machine commit limit.
            "NODE_OPTIONS": "--max-old-space-size=512",
            "npm_config_cache": str(CACHE_ROOT / "npm"),
        }
    )
    environment["PATH"] = str(_npm().parent) + os.pathsep + environment.get("PATH", "")
    functional_identity = _functional_identity_sha256()
    _prepare_candidate_checkpoints(functional_identity, environment)
    started = time.monotonic()
    dependency_link: Path | None = None
    try:
        dependency_link = _link_frontend_dependencies(_ensure_frontend_dependencies())
        # Prime the Agent's short admission cache before Chromium starts.  The
        # actual review still performs its own dated, fail-closed Evidence
        # preflight; this only avoids repeating the expensive admission probe
        # while Browser memory is resident.
        _wait_json(AGENT_READY, timeout=100)
        candidate = _run(
            [str(_npm()), "run", "test:e2e"],
            cwd=frontend,
            env=environment,
            timeout=1800,
            capture=True,
        )
        if candidate.returncode:
            raise UatError("representative candidate user journey failed")
    finally:
        if dependency_link is not None:
            _unlink_frontend_dependencies(dependency_link)
        for name in ("test-results", "playwright-report", "dist", "coverage"):
            shutil.rmtree(frontend / name, ignore_errors=True)
    after = status()
    if after["status"] != "READY":
        raise UatError("UAT lost readiness after the candidate journey")
    _clear_candidate_checkpoints()
    return {
        "status": "PASS",
        "journey_count": 1,
        "review_run_maximum": 2,
        "chat_turn_count": 2,
        "browser": "Chromium",
        "actual_claude": True,
        "actual_three_law_rag": True,
        "report_section_count": 8,
        "independent_approval_and_child_run": True,
        "mock_fallback_count": 0,
        "auto_completion_count": 0,
        "duration_seconds": round(time.monotonic() - started, 3),
        "production_release": False,
    }


def open_workbench() -> dict[str, Any]:
    if status()["status"] != "READY":
        raise UatError("LOCAL UAT is not ready")
    opened = webbrowser.open(FRONTEND_ORIGIN, new=2)
    return {"status": "OPENED" if opened else "OPEN_REQUESTED", "url": FRONTEND_ORIGIN}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("up", "status", "open", "down", "smoke", "candidate", "restart", "hibernate"),
    )
    args = parser.parse_args(argv)
    try:
        if args.action == "up":
            result = up()
        elif args.action == "status":
            result = status()
        elif args.action == "open":
            result = open_workbench()
        elif args.action == "down":
            result = down()
        elif args.action == "candidate":
            result = candidate()
        elif args.action == "restart":
            result = restart()
        elif args.action == "hibernate":
            result = hibernate()
        else:
            result = smoke()
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (UatError, OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
