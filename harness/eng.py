"""Minimal local control plane for the customs workspace.

The harness coordinates repository-owned checks.  It does not duplicate their
test logic and it never runs a Production certification matrix.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any


ENGINEERING_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ENGINEERING_ROOT.parent
CONFIG_ROOT = ENGINEERING_ROOT / "config"
LOCAL_ROOT = WORKSPACE_ROOT / "_local"
ARTIFACT_ROOT = WORKSPACE_ROOT / "_artifacts" / "tasks"
REPOSITORIES = ("frontend", "backend", "ai_agent", "data_platform")
LOCAL_PACKAGE_NAMES = {
    "backend": "customs-backend",
    "ai_agent": "customs-ai-agent",
    "data_platform": "customs-data-platform",
}
GENERATED_NAMES = frozenset(
    {
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "htmlcov",
        "node_modules",
        "test-results",
    }
)
UAT_ACTIONS = ("up", "status", "open", "down", "smoke", "candidate", "restart", "hibernate")


class HarnessError(RuntimeError):
    """A bounded, user-facing harness error."""


def _load_toml(name: str) -> dict[str, Any]:
    with (CONFIG_ROOT / name).open("rb") as handle:
        return tomllib.load(handle)


def _emit(value: Any) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        print(rendered)
    except UnicodeEncodeError:
        # Windows can return replacement characters in a filesystem error
        # while the parent shell still uses cp949. Keep the failure readable
        # instead of masking it with a second encoding exception.
        print(json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True))


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    capture: bool = False,
    timeout: int | None = None,
) -> dict[str, Any]:
    resolved = list(argv)
    if os.name == "nt" and resolved and not Path(resolved[0]).is_absolute():
        executable = shutil.which(resolved[0], path=(env or os.environ).get("PATH"))
        if executable:
            resolved[0] = executable
    started = time.monotonic()
    completed = subprocess.run(
        resolved,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture,
        timeout=timeout,
        check=False,
    )
    result: dict[str, Any] = {
        "command": argv,
        "exit_code": completed.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    if capture:
        result.update(stdout=completed.stdout.strip(), stderr=completed.stderr.strip())
    return result


def _git(repository: Path, *arguments: str, check: bool = True) -> str:
    result = _run(
        ["git", "-C", str(repository), *arguments],
        cwd=WORKSPACE_ROOT,
        capture=True,
    )
    if check and result["exit_code"] != 0:
        raise HarnessError(str(result.get("stderr") or result.get("stdout") or "git failed"))
    return str(result.get("stdout", "")).strip()


def _definitions() -> dict[str, dict[str, Any]]:
    records = _load_toml("workspace.toml").get("repos")
    if not isinstance(records, dict) or set(records) != set(REPOSITORIES):
        raise HarnessError("workspace repository configuration is invalid")
    return {name: dict(record) for name, record in records.items()}


def _configured_string(record: dict[str, Any], key: str, label: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise HarnessError(f"{label} is invalid")
    return value


def _assert_remote(
    repository: Path,
    remote: str,
    expected: str,
    *,
    push: bool = False,
) -> str:
    arguments = ["remote", "get-url"]
    if push:
        arguments.append("--push")
    arguments.append(remote)
    actual = _git(repository, *arguments, check=False)
    if actual != expected:
        direction = " push" if push else ""
        raise HarnessError(f"{repository.name} {remote}{direction} URL differs from its configuration")
    return actual


def _expected_harness_origin() -> str:
    harness = _load_toml("workspace.toml").get("harness")
    if not isinstance(harness, dict):
        raise HarnessError("harness repository configuration is invalid")
    return _configured_string(dict(harness), "expected_origin", "harness expected_origin")


def _repo(name: str) -> Path:
    if name not in REPOSITORIES:
        raise HarnessError(f"unknown repository: {name}")
    path = (ENGINEERING_ROOT / str(_definitions()[name]["path"])).resolve()
    try:
        path.relative_to(WORKSPACE_ROOT.resolve())
    except ValueError as exc:
        raise HarnessError(f"repository escaped workspace: {name}") from exc
    if not path.is_dir() or not (path / ".git").exists():
        raise HarnessError(f"repository is unavailable: {name}")
    return path


def _repo_state(name: str) -> dict[str, Any]:
    repository = _repo(name)
    definition = _definitions()[name]
    state = {
        "branch": _git(repository, "branch", "--show-current"),
        "revision": _git(repository, "rev-parse", "HEAD"),
        "dirty": bool(_git(repository, "status", "--porcelain=v1", "--untracked-files=all")),
        "origin": _assert_remote(
            repository,
            "origin",
            _configured_string(definition, "expected_origin", f"{name} expected_origin"),
        ),
    }
    if "expected_upstream" in definition:
        state["upstream"] = _assert_remote(
            repository,
            "upstream",
            _configured_string(definition, "expected_upstream", f"{name} expected_upstream"),
        )
        state["upstream_push"] = _assert_remote(
            repository,
            "upstream",
            _configured_string(
                definition, "expected_upstream_push", f"{name} expected_upstream_push"
            ),
            push=True,
        )
    return state


def _directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def doctor() -> dict[str, Any]:
    if (WORKSPACE_ROOT / ".git").exists():
        raise HarnessError("project root must not be a Git repository")
    if _git(ENGINEERING_ROOT, "remote").splitlines() != ["origin"]:
        raise HarnessError("harness must have exactly one origin remote")
    harness_origin = _assert_remote(ENGINEERING_ROOT, "origin", _expected_harness_origin())
    disk = shutil.disk_usage(WORKSPACE_ROOT)
    return {
        "status": "PASS",
        "level": "L0",
        "artifact_created": False,
        "free_bytes": disk.free,
        "repositories": {name: _repo_state(name) for name in REPOSITORIES},
        "harness": {
            "revision": _git(ENGINEERING_ROOT, "rev-parse", "HEAD"),
            "dirty": bool(_git(ENGINEERING_ROOT, "status", "--porcelain=v1")),
            "origin": harness_origin,
        },
        "storage_bytes": {
            "artifacts": _directory_size(WORKSPACE_ROOT / "_artifacts"),
            "local": _directory_size(LOCAL_ROOT),
        },
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _uv() -> Path:
    configured = Path(
        os.environ.get(
            "CUSTOMS_UV",
            str(Path(os.environ["LOCALAPPDATA"]) / "CustomsUAT/cache/tools/uv/v0.11.28/uv.exe"),
        )
    )
    if configured.is_file():
        return configured
    located = shutil.which("uv")
    if located:
        return Path(located)
    raise HarnessError("managed uv executable is unavailable")


def _npm() -> Path:
    configured = Path(os.environ["LOCALAPPDATA"]) / "CustomsUAT/cache/tools/node/v22.23.1/npm.cmd"
    if configured.is_file():
        return configured
    located = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if located:
        return Path(located)
    raise HarnessError("npm is unavailable")


def _managed_environment(name: str, level: str) -> tuple[dict[str, str], Path | None]:
    repository = _repo(name)
    environment = dict(os.environ)
    cache_root = Path(os.environ["LOCALAPPDATA"]) / "CustomsUAT" / "cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.gettempdir()) / "customs-uat" / "r" / f"check-{os.getpid()}"
    scratch.mkdir(parents=True, exist_ok=True)
    environment.update(
        {
            "TEMP": str(scratch),
            "TMP": str(scratch),
            "TMPDIR": str(scratch),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": str(scratch / "pycache"),
            "UV_CACHE_DIR": str(cache_root / "uv"),
            "npm_config_cache": str(cache_root / "npm"),
        }
    )
    if name == "frontend":
        node_root = _npm().parent
        environment["PATH"] = str(node_root) + os.pathsep + environment.get("PATH", "")
        result = _run([str(_npm()), "ci", "--prefer-offline"], cwd=repository, env=environment)
        if result["exit_code"] != 0:
            shutil.rmtree(scratch, ignore_errors=True)
            raise HarnessError("frontend dependency installation failed")
        return environment, None
    lock = repository / "uv.lock"
    lock_hash = _sha256(lock)[:16]
    managed = cache_root.parent / "envs" / "checks" / f"{name}-{lock_hash}-{level}"
    environment["UV_PROJECT_ENVIRONMENT"] = str(managed)
    sync = [str(_uv()), "sync", "--locked", "--no-editable"]
    sync.extend(["--reinstall-package", LOCAL_PACKAGE_NAMES[name]])
    configured_sync = _definitions()[name].get(f"{level}_sync", [])
    if not isinstance(configured_sync, list) or not all(isinstance(item, str) for item in configured_sync):
        raise HarnessError(f"invalid {level}_sync configuration for {name}")
    sync.extend(configured_sync)
    result = _run(sync, cwd=repository, env=environment)
    if result["exit_code"] != 0:
        shutil.rmtree(scratch, ignore_errors=True)
        raise HarnessError(f"{name} dependency installation failed")
    environment["PATH"] = str(managed / ("Scripts" if os.name == "nt" else "bin")) + os.pathsep + environment.get("PATH", "")
    return environment, managed


def _configured_commands(name: str, level: str) -> list[list[str]]:
    value = _definitions()[name].get(level)
    if not isinstance(value, list) or not value:
        raise HarnessError(f"no {level} command configured for {name}")
    commands: list[list[str]] = []
    for command in value:
        if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
            raise HarnessError(f"invalid {level} command for {name}")
        commands.append(list(command))
    return commands


def _compact_summary(
    task_id: str,
    *,
    level: str,
    status: str,
    results: list[dict[str, Any]],
) -> Path:
    task_id = _task_id(task_id)
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    destination = ARTIFACT_ROOT / f"{task_id}.json"
    previous: dict[str, Any] = {}
    if destination.is_file():
        try:
            previous = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            previous = {}
    payload = {
        **previous,
        "schema_version": "customs-task-evidence/v2",
        "task_id": task_id,
        "state": "final" if level == "candidate" and status == "PASS" else "checkpoint",
        "updated_at_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "validation_level": level,
        "status": status,
        "repositories": {name: _repo_state(name) for name in REPOSITORIES},
        "latest_results": [
            {
                "scope": item["scope"],
                "exit_code": item["exit_code"],
                "duration_seconds": item["duration_seconds"],
            }
            for item in results
        ],
        "production_release": False,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    payload["content_sha256"] = hashlib.sha256(canonical).hexdigest()
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    return destination


def _functional_check(task_id: str, scope: str) -> dict[str, Any]:
    started = time.monotonic()
    result = _run(
        [
            sys.executable,
            "-B",
            str(ENGINEERING_ROOT / "harness" / "functional_uat.py"),
            "--task",
            task_id,
            "--live",
            "--scope",
            scope,
        ],
        cwd=ENGINEERING_ROOT,
        capture=True,
        # Nine sequential Claude/RAG Root cases plus the bounded Chat/HITL
        # scopes are intentionally allowed to take up to three hours.
        timeout=3 * 60 * 60,
    )
    try:
        payload = json.loads(str(result.get("stdout", "")))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HarnessError("functional UAT returned invalid output") from exc
    if result["exit_code"] != 0 or not isinstance(payload, dict):
        safe_error = (
            payload.get("error_code") or payload.get("error")
            if isinstance(payload, dict)
            else None
        )
        raise HarnessError(str(safe_error or "functional UAT failed"))
    if payload.get("status") != "PASS":
        raise HarnessError("functional UAT did not pass the requested scope")
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    destination = ARTIFACT_ROOT / f"{task_id}.json"
    artifact = {
        "schema_version": "customs-task-functional-evidence/v1",
        "task_id": task_id,
        "state": "final" if scope == "all" else "checkpoint",
        "status": "PASS",
        "validation_level": "functional",
        "updated_at_utc": dt.datetime.now(dt.UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "repositories": {name: _repo_state(name) for name in REPOSITORIES},
        "functional_uat": payload,
        "production_release": False,
    }
    canonical = json.dumps(
        artifact,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    artifact["content_sha256"] = hashlib.sha256(canonical).hexdigest()
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    return {
        "status": "PASS",
        "level": "L3-FUNCTIONAL",
        "scope": scope,
        "duration_seconds": round(time.monotonic() - started, 3),
        "artifact": str(destination.relative_to(WORKSPACE_ROOT)),
        "production_release": False,
    }


def check(
    level: str,
    repository: str | None,
    task_id: str | None,
    live: bool,
    scope: str | None = None,
) -> dict[str, Any]:
    if level in {"quick", "contract"}:
        if repository not in REPOSITORIES:
            raise HarnessError(f"{level} requires one explicit --repo")
        if level == "contract" and task_id is None:
            raise HarnessError("contract requires --task")
        environment: dict[str, str] | None = None
        managed: Path | None = None
        results: list[dict[str, Any]] = []
        try:
            environment, managed = _managed_environment(repository, level)
            for command in _configured_commands(repository, level):
                result = _run(command, cwd=_repo(repository), env=environment)
                result["scope"] = repository
                results.append(result)
                if result["exit_code"] != 0:
                    break
        finally:
            if repository == "frontend":
                for name in ("node_modules", "dist", "coverage", "test-results"):
                    shutil.rmtree(_repo(repository) / name, ignore_errors=True)
            elif managed is not None:
                shutil.rmtree(managed, ignore_errors=True)
            scratch_root = Path(tempfile.gettempdir()) / "customs-uat" / "r"
            for scratch in scratch_root.glob(f"check-{os.getpid()}") if scratch_root.exists() else ():
                shutil.rmtree(scratch, ignore_errors=True)
        status = "PASS" if results and all(item["exit_code"] == 0 for item in results) else "FAIL"
        artifact = None
        if level == "contract" and task_id:
            artifact = _compact_summary(task_id, level=level, status=status, results=results)
        if status != "PASS":
            raise HarnessError(f"{repository} {level} check failed")
        return {
            "status": status,
            "level": "L1" if level == "quick" else "L2",
            "repository": repository,
            "artifact": str(artifact.relative_to(WORKSPACE_ROOT)) if artifact else None,
        }
    if level == "functional":
        if repository is not None or task_id != "TASK-0033" or not live or scope is None:
            raise HarnessError(
                "functional requires --task TASK-0033 --live --scope <scope> and no --repo"
            )
        return _functional_check(_task_id(task_id), scope)
    if level != "candidate" or repository is not None or not task_id or not live or scope:
        raise HarnessError("candidate requires --task <TASK-ID> --live and no --repo")
    started = time.monotonic()
    uat_result = uat("candidate")
    result = {
        "scope": "representative_user_journey",
        "exit_code": 0,
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    artifact = _compact_summary(task_id, level="candidate", status="PASS", results=[result])
    return {
        "status": "PASS",
        "level": "L3-CANDIDATE",
        "journey_count": 1,
        "uat": uat_result,
        "artifact": str(artifact.relative_to(WORKSPACE_ROOT)),
    }


def _tracked_descendant(path: Path) -> bool:
    for root in (ENGINEERING_ROOT, *(_repo(name) for name in REPOSITORIES)):
        try:
            relative = path.resolve().relative_to(root.resolve())
        except ValueError:
            continue
        return bool(_git(root, "ls-files", "--", relative.as_posix(), check=False))
    return False


def _remove_tree(path: Path) -> None:
    target = str(path.resolve())
    if os.name == "nt" and not target.startswith("\\\\?\\"):
        target = (
            "\\\\?\\UNC\\" + target[2:]
            if target.startswith("\\\\")
            else "\\\\?\\" + target
        )

    def make_writable_and_retry(function: Any, name: str, _error: Any) -> None:
        os.chmod(name, stat.S_IWRITE)
        function(name)

    shutil.rmtree(target, onerror=make_writable_and_retry)


def cleanup_candidates() -> list[dict[str, Any]]:
    candidates: set[Path] = set()
    for root in (ENGINEERING_ROOT, *(_repo(name) for name in REPOSITORIES)):
        for path in root.rglob("*"):
            if path.is_dir() and (path.name in GENERATED_NAMES or path.name.endswith(".egg-info")):
                candidates.add(path)
    scratch_root = Path(tempfile.gettempdir()) / "customs-uat" / "r"
    if scratch_root.is_dir():
        candidates.update(path for path in scratch_root.iterdir() if path.is_dir())
    legacy_uat_runtime = LOCAL_ROOT / "state" / "runtime" / "local-user-uat"
    if legacy_uat_runtime.is_dir():
        candidates.add(legacy_uat_runtime)
    uat_state_root = LOCAL_ROOT / "state" / "local-user-uat"
    if uat_state_root.is_dir():
        candidates.update(
            path
            for path in uat_state_root.iterdir()
            if path.is_dir()
            and path.name.startswith("data-context-")
            and path.name != "data-context"
        )
    result: list[dict[str, Any]] = []
    for path in sorted(candidates, key=lambda item: (len(item.parts), str(item).casefold())):
        if any(parent in candidates for parent in path.parents):
            continue
        if path.is_symlink() or _tracked_descendant(path):
            continue
        result.append({"path": str(path), "bytes": _directory_size(path)})
    return result


def clean(*, apply: bool) -> dict[str, Any]:
    candidates = cleanup_candidates()
    removed = 0
    if apply:
        for record in candidates:
            path = Path(record["path"])
            if path.exists() and not path.is_symlink() and not _tracked_descendant(path):
                _remove_tree(path)
                removed += int(record["bytes"])
    return {
        "status": "PASS",
        "mode": "apply" if apply else "dry-run",
        "candidate_count": len(candidates),
        "candidate_bytes": sum(int(item["bytes"]) for item in candidates),
        "removed_bytes": removed,
        "candidates": candidates,
        "artifact_created": False,
    }


def uat(action: str) -> dict[str, Any]:
    if action not in UAT_ACTIONS:
        raise HarnessError(f"unsupported UAT action: {action}")
    result = _run(
        [sys.executable, "-B", str(ENGINEERING_ROOT / "harness" / "local_user_uat.py"), action],
        cwd=ENGINEERING_ROOT,
        capture=True,
        timeout=2100 if action in {"up", "restart", "smoke", "candidate"} else 180,
    )
    if result["exit_code"] != 0:
        raise HarnessError(str(result.get("stderr") or result.get("stdout") or f"UAT {action} failed"))
    try:
        payload = json.loads(str(result.get("stdout", "")))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HarnessError("UAT returned invalid output") from exc
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")

    check_parser = commands.add_parser("check")
    check_parser.add_argument(
        "level", choices=("quick", "contract", "candidate", "functional")
    )
    check_parser.add_argument("--repo", choices=REPOSITORIES)
    check_parser.add_argument("--task")
    check_parser.add_argument("--live", action="store_true")
    check_parser.add_argument(
        "--scope", choices=("scenarios", "chat", "hitl", "adaptive", "ui", "all")
    )

    uat_parser = commands.add_parser("uat")
    uat_parser.add_argument("action", choices=UAT_ACTIONS)

    clean_parser = commands.add_parser("clean")
    clean_mode = clean_parser.add_mutually_exclusive_group(required=True)
    clean_mode.add_argument("--dry-run", action="store_true")
    clean_mode.add_argument("--apply", action="store_true")

    return parser


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "doctor":
        return doctor()
    if args.command == "check":
        return check(args.level, args.repo, args.task, args.live, args.scope)
    if args.command == "uat":
        return uat(args.action)
    if args.command == "clean":
        return clean(apply=args.apply)
    raise HarnessError("unsupported command")


def main(argv: list[str] | None = None) -> int:
    try:
        _emit(dispatch(build_parser().parse_args(argv)))
        return 0
    except (HarnessError, OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
        _emit({"status": "FAIL", "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
