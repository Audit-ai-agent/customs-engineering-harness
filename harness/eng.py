"""Small, local coordinator for the four independent product repositories.

The harness owns only workspace validation, explicit dependency setup, and
delegation to repository-owned checks.  It never builds a deployment, starts
services, creates durable evidence, or copies product source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


HARNESS_ROOT = Path(__file__).resolve().parent.parent
CONFIG_ROOT = HARNESS_ROOT / "config"
REPOSITORIES = ("frontend", "backend", "ai_agent", "data_platform")
REPOSITORY_SELECTORS = {
    "frontend": "frontend",
    "customs-frontend": "frontend",
    "backend": "backend",
    "customs-backend": "backend",
    "ai_agent": "ai_agent",
    "customs-ai-agent": "ai_agent",
    "data_platform": "data_platform",
    "customs-data-platform": "data_platform",
}
_MARKER_NAME = ".customs-harness-setup.json"
_GITHUB_HOST = "github.com"
_SCP_GITHUB_REMOTE = re.compile(
    r"^(?P<user>git)@github\.com:(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?$",
    re.IGNORECASE,
)


class HarnessError(RuntimeError):
    """A bounded, user-facing harness error."""


def _emit(value: Mapping[str, Any]) -> None:
    try:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    except UnicodeEncodeError:
        print(json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True))


def _load_toml(name: str) -> dict[str, Any]:
    path = CONFIG_ROOT / name
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise HarnessError(f"invalid configuration: {name}") from exc
    if not isinstance(value, dict):
        raise HarnessError(f"invalid configuration: {name}")
    return value


def _required_string(record: Mapping[str, Any], key: str, label: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HarnessError(f"{label} is invalid")
    return value.strip()


def _workspace_config() -> dict[str, Any]:
    return _load_toml("workspace.toml")


def _workspace_root() -> Path:
    config = _workspace_config()
    raw = _required_string(config, "workspace_root", "workspace_root")
    root = (HARNESS_ROOT / raw).resolve()
    if not root.is_dir():
        raise HarnessError("workspace root is unavailable")
    try:
        HARNESS_ROOT.relative_to(root)
    except ValueError as exc:
        raise HarnessError("workspace root must contain this harness") from exc
    return root


def _definitions() -> dict[str, dict[str, Any]]:
    records = _workspace_config().get("repos")
    if not isinstance(records, dict) or set(records) != set(REPOSITORIES):
        raise HarnessError("workspace repository configuration is invalid")
    if not all(isinstance(value, dict) for value in records.values()):
        raise HarnessError("workspace repository configuration is invalid")
    return {name: dict(records[name]) for name in REPOSITORIES}


def _canonical_repository(value: str) -> str:
    parts = value.strip().split("/")
    if (
        len(parts) != 2
        or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part or "") for part in parts)
    ):
        raise HarnessError("configured GitHub repository identity is invalid")
    return "/".join(part.casefold() for part in parts)


def normalize_github_origin(value: str) -> str:
    """Return the case-insensitive ``owner/repository`` GitHub identity.

    Only normal HTTPS and SSH GitHub clone forms are accepted.  In particular,
    URLs containing credentials or a non-GitHub host are not silently accepted.
    """

    raw = value.strip()
    scp_match = _SCP_GITHUB_REMOTE.fullmatch(raw)
    if scp_match:
        return _canonical_repository(f"{scp_match.group('owner')}/{scp_match.group('repo')}")

    parsed = urlsplit(raw)
    if parsed.scheme == "https":
        if parsed.hostname is None or parsed.hostname.casefold() != _GITHUB_HOST:
            raise HarnessError("origin must use github.com")
        if parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443):
            raise HarnessError("origin URL is not an allowed GitHub HTTPS clone URL")
    elif parsed.scheme == "ssh":
        if (
            parsed.hostname is None
            or parsed.hostname.casefold() != _GITHUB_HOST
            or parsed.username != "git"
            or parsed.password is not None
            or parsed.port not in (None, 22)
        ):
            raise HarnessError("origin URL is not an allowed GitHub SSH clone URL")
    else:
        raise HarnessError("origin must use HTTPS or SSH GitHub clone syntax")

    if parsed.query or parsed.fragment:
        raise HarnessError("origin URL is not an allowed GitHub clone URL")
    path = parsed.path.strip("/")
    if path.casefold().endswith(".git"):
        path = path[:-4]
    return _canonical_repository(path)


def _repo(name: str) -> Path:
    if name not in REPOSITORIES:
        raise HarnessError(f"unknown repository: {name}")
    definition = _definitions()[name]
    directory = _required_string(definition, "directory", f"{name} directory")
    if Path(directory).name != directory or directory in {".", ".."}:
        raise HarnessError(f"{name} directory must be a sibling directory name")
    workspace = _workspace_root()
    path = (workspace / directory).resolve()
    if path.parent != workspace or not path.is_dir() or not (path / ".git").exists():
        raise HarnessError(f"repository is unavailable: {name}")
    return path


def _repository_name(selector: str | None) -> str | None:
    """Normalize concise and GitHub repository selectors to one config key."""

    if selector is None:
        return None
    name = REPOSITORY_SELECTORS.get(selector)
    if name is None:
        raise HarnessError(f"unknown repository: {selector}")
    return name


def _run(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    capture: bool = False,
) -> dict[str, Any]:
    command = list(argv)
    if not command:
        raise HarnessError("empty command is not allowed")
    executable = shutil.which(command[0], path=(env or os.environ).get("PATH"))
    if executable is not None:
        command[0] = executable
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture,
    )
    result: dict[str, Any] = {
        "command": command,
        "duration_seconds": round(time.monotonic() - started, 3),
        "exit_code": completed.returncode,
    }
    if capture:
        result["stdout"] = completed.stdout.strip()
        result["stderr"] = completed.stderr.strip()
    return result


def _git(repository: Path, *arguments: str, check: bool = True) -> str:
    result = _run(["git", "-C", str(repository), *arguments], cwd=_workspace_root(), capture=True)
    if check and result["exit_code"] != 0:
        raise HarnessError("Git command failed")
    return str(result.get("stdout", "")).strip()


def _origin_identity(repository: Path) -> str:
    result = _run(
        ["git", "-C", str(repository), "remote", "get-url", "origin"],
        cwd=_workspace_root(),
        capture=True,
    )
    if result["exit_code"] != 0:
        raise HarnessError(f"{repository.name} does not have an origin remote")
    return normalize_github_origin(str(result.get("stdout", "")))


def _assert_repository_origin(name: str, repository: Path) -> str:
    expected = _canonical_repository(
        _required_string(_definitions()[name], "repository", f"{name} repository")
    )
    actual = _origin_identity(repository)
    if actual != expected:
        raise HarnessError(f"{repository.name} origin does not identify the configured GitHub repository")
    return actual


def _harness_identity() -> str:
    harness = _workspace_config().get("harness")
    if not isinstance(harness, dict):
        raise HarnessError("harness repository configuration is invalid")
    return _canonical_repository(_required_string(harness, "repository", "harness repository"))


def _assert_harness_origin() -> str:
    actual = _origin_identity(HARNESS_ROOT)
    if actual != _harness_identity():
        raise HarnessError("harness origin does not identify the configured GitHub repository")
    return actual


def _repo_state(name: str) -> dict[str, Any]:
    repository = _repo(name)
    return {
        "branch": _git(repository, "branch", "--show-current"),
        "dirty": bool(_git(repository, "status", "--porcelain=v1", "--untracked-files=all")),
        "origin": _assert_repository_origin(name, repository),
        "revision": _git(repository, "rev-parse", "HEAD"),
    }


def _first_line(command: Sequence[str], *, cwd: Path | None = None) -> str | None:
    executable = shutil.which(command[0])
    if executable is None:
        return None
    completed = subprocess.run(
        [executable, *command[1:]],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        cwd=cwd,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.splitlines()[0].strip() if completed.stdout else ""


def _frontend_package_manager() -> str:
    package_manager_path = _repo("frontend") / "package.json"
    try:
        package_manager = json.loads(package_manager_path.read_text(encoding="utf-8")).get("packageManager")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HarnessError("frontend packageManager metadata is unavailable") from exc
    if not isinstance(package_manager, str):
        raise HarnessError("frontend packageManager metadata is invalid")
    return package_manager


def _tool_versions() -> dict[str, str]:
    config = _workspace_config().get("tooling")
    if not isinstance(config, dict):
        raise HarnessError("tooling configuration is invalid")
    expected_python = _required_string(config, "python", "tooling python")
    expected_git_minimum = _required_string(config, "git_minimum", "tooling git_minimum")
    node_major = config.get("node_major")
    if not isinstance(node_major, int) or node_major < 1:
        raise HarnessError("tooling node_major is invalid")
    node_minimum = _required_string(config, "node_minimum", "tooling node_minimum")
    npm_version = _required_string(config, "npm_version", "tooling npm_version")
    minimum_node_match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", node_minimum)
    if minimum_node_match is None:
        raise HarnessError("tooling node_minimum is invalid")
    minimum_node = tuple(int(value) for value in minimum_node_match.groups())
    if minimum_node[0] != node_major:
        raise HarnessError("tooling Node major and minimum are inconsistent")
    if re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", npm_version) is None:
        raise HarnessError("tooling npm_version is invalid")

    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if python_version != expected_python:
        raise HarnessError(f"Python {expected_python} is required")

    git_version = _first_line(("git", "--version"))
    if git_version is None:
        raise HarnessError("Git is required")
    git_match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", git_version)
    minimum_match = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?", expected_git_minimum)
    if git_match is None or minimum_match is None:
        raise HarnessError("Git version could not be validated")
    actual_git = tuple(int(value or 0) for value in git_match.groups())
    minimum_git = tuple(int(value or 0) for value in minimum_match.groups())
    if actual_git < minimum_git:
        raise HarnessError(f"Git {expected_git_minimum} or newer is required")

    node_version = _first_line(("node", "--version"))
    node_match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", node_version or "")
    actual_node = tuple(int(value) for value in node_match.groups()) if node_match else None
    if actual_node is None or actual_node[0] != node_major or actual_node < minimum_node:
        raise HarnessError(f"Node.js {node_minimum} or newer within {node_major}.x is required")
    corepack_version = _first_line(("corepack", "--version"))
    if corepack_version is None:
        raise HarnessError(f"Corepack is required for npm {npm_version}")
    if _frontend_package_manager() != f"npm@{npm_version}":
        raise HarnessError(f"frontend must pin npm@{npm_version} for Corepack")
    uv_version = _first_line(("uv", "--version"))
    if uv_version is None:
        raise HarnessError("uv is required for Python repository setup")
    return {
        "git": git_version,
        "node": node_version,
        "corepack": corepack_version,
        "npm": f"{npm_version} (pinned by Corepack)",
        "python": sys.version.split()[0],
        "uv": uv_version,
    }


def doctor() -> dict[str, Any]:
    """Validate the shareable workspace without requiring a personal upstream."""

    return {
        "artifact_created": False,
        "harness": {
            "directory": HARNESS_ROOT.name,
            "dirty": bool(_git(HARNESS_ROOT, "status", "--porcelain=v1", "--untracked-files=all")),
            "origin": _assert_harness_origin(),
            "revision": _git(HARNESS_ROOT, "rev-parse", "HEAD"),
        },
        "repositories": {name: _repo_state(name) for name in REPOSITORIES},
        "status": "PASS",
        "tools": _tool_versions(),
        "workspace": str(_workspace_root()),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _marker_path(repository: Path, runtime: str) -> Path:
    if runtime == "npm":
        return repository / "node_modules" / _MARKER_NAME
    if runtime == "uv":
        return repository / ".venv" / _MARKER_NAME
    raise HarnessError("repository runtime is invalid")


def _setup_fingerprint(repository: Path, runtime: str) -> dict[str, str]:
    lock_name = "package-lock.json" if runtime == "npm" else "uv.lock"
    lock = repository / lock_name
    if not lock.is_file():
        raise HarnessError(f"{repository.name} is missing {lock_name}")
    # Bump this whenever setup semantics change.  The marker then cannot claim
    # an older dependency selection is ready for a newer check configuration.
    fingerprint = {"lock_file": lock_name, "lock_sha256": _sha256(lock), "schema_version": "3"}
    if runtime == "npm":
        tooling = _workspace_config().get("tooling")
        if not isinstance(tooling, dict):
            raise HarnessError("tooling configuration is invalid")
        fingerprint["npm_version"] = _required_string(tooling, "npm_version", "tooling npm_version")
    return fingerprint


def _read_marker(repository: Path, runtime: str) -> dict[str, str] | None:
    marker = _marker_path(repository, runtime)
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or not all(isinstance(item, str) for item in value.values()):
        return None
    return {str(key): str(item) for key, item in value.items()}


def _write_marker(repository: Path, runtime: str) -> None:
    marker = _marker_path(repository, runtime)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(_setup_fingerprint(repository, runtime), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _runtime(name: str) -> str:
    runtime = _definitions()[name].get("runtime")
    if runtime not in {"npm", "uv"}:
        raise HarnessError(f"{name} runtime is invalid")
    return str(runtime)


def _configured_commands(name: str, field: str) -> list[list[str]]:
    value = _definitions()[name].get(field)
    if not isinstance(value, list) or not value:
        raise HarnessError(f"no {field} command configured for {name}")
    commands: list[list[str]] = []
    for command in value:
        if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
            raise HarnessError(f"invalid {field} command configured for {name}")
        commands.append(list(command))
    return commands


def _frontend_tree_is_healthy(repository: Path) -> bool:
    if not (repository / "node_modules").is_dir():
        return False
    result = _run(["corepack", "npm", "ls", "--depth=0", "--ignore-scripts"], cwd=repository, capture=True)
    return result["exit_code"] == 0


def _setup_frontend(repository: Path) -> str:
    fingerprint = _setup_fingerprint(repository, "npm")
    if _read_marker(repository, "npm") == fingerprint and _frontend_tree_is_healthy(repository):
        return "reused"
    # Corepack runs the exact checked-in npm version against the lockfile while
    # synchronizing the existing directory in place. Unlike ``npm ci``, an
    # explicit repeated setup does not begin by deleting a healthy node_modules
    # tree.
    result = _run(
        ["corepack", "npm", "install", "--package-lock=true", "--prefer-offline"], cwd=repository
    )
    if result["exit_code"] != 0:
        raise HarnessError("frontend dependency setup failed")
    _write_marker(repository, "npm")
    return "synchronized"


def _setup_python(name: str, repository: Path) -> str:
    commands = _configured_commands(name, "setup")
    for command in commands:
        result = _run(command, cwd=repository)
        if result["exit_code"] != 0:
            raise HarnessError(f"{name} dependency setup failed")
    python = _venv_python(repository)
    if python is None:
        raise HarnessError(f"{name} setup did not create a local .venv")
    _write_marker(repository, "uv")
    return "reused_or_synchronized"


def setup(name: str | None) -> dict[str, Any]:
    selected = _repository_name(name)
    names = REPOSITORIES if selected is None else (selected,)
    results: dict[str, dict[str, str]] = {}
    for selected in names:
        repository = _repo(selected)
        _assert_repository_origin(selected, repository)
        runtime = _runtime(selected)
        action = _setup_frontend(repository) if runtime == "npm" else _setup_python(selected, repository)
        results[selected] = {"path": str(repository), "state": action}
    return {"artifact_created": False, "repositories": results, "status": "PASS"}


def _venv_python(repository: Path) -> Path | None:
    candidates = (
        repository / ".venv" / "Scripts" / "python.exe",
        repository / ".venv" / "bin" / "python",
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _check_environment(name: str, repository: Path) -> Mapping[str, str]:
    runtime = _runtime(name)
    fingerprint = _setup_fingerprint(repository, runtime)
    if _read_marker(repository, runtime) != fingerprint:
        raise HarnessError(f"{name} dependencies are not prepared for the current lock; run setup")
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if runtime == "npm":
        if not _frontend_tree_is_healthy(repository):
            raise HarnessError("frontend dependencies are incomplete; run setup")
        return environment
    python = _venv_python(repository)
    if python is None:
        raise HarnessError(f"{name} local .venv is unavailable; run setup")
    environment["PATH"] = str(python.parent) + os.pathsep + environment.get("PATH", "")
    return environment


def _relative_contract_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise HarnessError(f"{label} is invalid")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts or candidate.name in {".", ".."}:
        raise HarnessError(f"{label} is invalid")
    return candidate


def _cross_contract_files(name: str, repository: Path) -> list[dict[str, str]]:
    """Compare only configured public contract bytes across sibling repos.

    This is intentionally opt-in.  A regular local contract check stays local;
    ``--cross`` is used when a provider API/schema changes and checks the exact
    bytes retained by its consumer.  It has no release, commit, signature, or
    runtime provenance meaning.
    """

    raw = _definitions()[name].get("cross_contract")
    if not isinstance(raw, dict) or not isinstance(raw.get("files"), list) or not raw["files"]:
        raise HarnessError(f"{name} has no configured cross-repository contract check")
    results: list[dict[str, str]] = []
    for index, item in enumerate(raw["files"]):
        if not isinstance(item, dict):
            raise HarnessError(f"{name} cross-repository contract configuration is invalid")
        provider = item.get("provider")
        if not isinstance(provider, str) or provider not in REPOSITORIES or provider == name:
            raise HarnessError(f"{name} cross-repository contract provider is invalid")
        provider_path = _relative_contract_path(
            item.get("provider_path"), f"{name} cross-repository provider path"
        )
        consumer_path = _relative_contract_path(
            item.get("consumer_path"), f"{name} cross-repository consumer path"
        )
        provider_repository = _repo(provider)
        _assert_repository_origin(provider, provider_repository)
        provider_file = provider_repository / provider_path
        consumer_file = repository / consumer_path
        if not provider_file.is_file() or not consumer_file.is_file():
            raise HarnessError(f"cross-repository contract file is unavailable: {name}[{index}]")
        provider_digest = _sha256(provider_file)
        consumer_digest = _sha256(consumer_file)
        if provider_digest != consumer_digest:
            raise HarnessError(f"cross-repository contract byte drift: {name}[{index}]")
        results.append(
            {
                "consumer_file": str(consumer_file),
                "provider": provider,
                "provider_file": str(provider_file),
                "sha256": provider_digest,
            }
        )
    return results


def check(level: str, name: str | None, *, cross: bool = False) -> dict[str, Any]:
    if level not in {"quick", "contract"}:
        raise HarnessError("check supports only quick and contract")
    if cross and level != "contract":
        raise HarnessError("--cross is available only for a contract check")
    selected = _repository_name(name)
    if selected is None:
        raise HarnessError(f"{level} requires one explicit --repo")
    repository = _repo(selected)
    _assert_repository_origin(selected, repository)
    environment = _check_environment(selected, repository)
    cross_result: list[dict[str, str]] | None = None
    if cross:
        cross_result = _cross_contract_files(selected, repository)
    results: list[dict[str, Any]] = []
    for command in _configured_commands(selected, level):
        result = _run(command, cwd=repository, env=environment)
        results.append(result)
        if result["exit_code"] != 0:
            raise HarnessError(f"{selected} {level} check failed")
    response: dict[str, Any] = {
        "artifact_created": False,
        "level": "L1" if level == "quick" else "L2",
        "repository": selected,
        "results": results,
        "status": "PASS",
    }
    if cross_result is not None:
        response["cross_repository_contracts"] = cross_result
    return response


def _tracked_files_sha256(repository: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), "ls-files", "-s", "-z"],
        cwd=_workspace_root(),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise HarnessError("could not read tracked file inventory")
    return hashlib.sha256(completed.stdout).hexdigest()


def _snapshot_definitions() -> dict[str, dict[str, str]]:
    snapshot = _load_toml("project-snapshot.toml")
    if snapshot.get("schema_version") != "customs-project-snapshot/v2":
        raise HarnessError("project snapshot schema is unsupported")
    records = snapshot.get("repositories")
    if not isinstance(records, dict) or set(records) != set(REPOSITORIES):
        raise HarnessError("project snapshot repository configuration is invalid")
    validated: dict[str, dict[str, str]] = {}
    for name in REPOSITORIES:
        record = records[name]
        if not isinstance(record, dict):
            raise HarnessError("project snapshot repository configuration is invalid")
        validated[name] = {
            "branch": _required_string(record, "branch", f"snapshot {name} branch"),
            "repository": _canonical_repository(
                _required_string(record, "repository", f"snapshot {name} repository")
            ),
            "revision": _required_string(record, "revision", f"snapshot {name} revision"),
            "tracked_files_sha256": _required_string(
                record, "tracked_files_sha256", f"snapshot {name} tracked_files_sha256"
            ),
        }
        if not re.fullmatch(r"[0-9a-f]{40}", validated[name]["revision"]):
            raise HarnessError(f"snapshot {name} revision is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", validated[name]["tracked_files_sha256"]):
            raise HarnessError(f"snapshot {name} tracked_files_sha256 is invalid")
    return validated


def snapshot_verify() -> dict[str, Any]:
    records = _snapshot_definitions()
    results: dict[str, dict[str, Any]] = {}
    for name in REPOSITORIES:
        repository = _repo(name)
        expected = records[name]
        configured_identity = _canonical_repository(
            _required_string(_definitions()[name], "repository", f"{name} repository")
        )
        if expected["repository"] != configured_identity:
            raise HarnessError(f"snapshot {name} repository does not match workspace configuration")
        state = _repo_state(name)
        if state["dirty"]:
            raise HarnessError(f"snapshot verify requires a clean repository: {name}")
        if state["branch"] != expected["branch"]:
            raise HarnessError(f"snapshot verify requires {name} on {expected['branch']}")
        actual_digest = _tracked_files_sha256(repository)
        if actual_digest != expected["tracked_files_sha256"]:
            raise HarnessError(f"snapshot tracked-file digest differs: {name}")
        results[name] = {
            "branch": state["branch"],
            "revision": state["revision"],
            "revision_matches_provenance": state["revision"] == expected["revision"],
            "tracked_files_sha256": actual_digest,
        }
    return {
        "artifact_created": False,
        "repositories": results,
        "status": "PASS",
        "verification": "clean main branch plus git ls-files -s -z SHA-256",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")

    setup_parser = commands.add_parser("setup")
    setup_parser.add_argument("--repo", choices=tuple(REPOSITORY_SELECTORS))

    check_parser = commands.add_parser("check")
    check_parser.add_argument("level", choices=("quick", "contract"))
    check_parser.add_argument("--repo", choices=tuple(REPOSITORY_SELECTORS), required=True)
    check_parser.add_argument("--cross", action="store_true")

    snapshot_parser = commands.add_parser("snapshot")
    snapshot_commands = snapshot_parser.add_subparsers(dest="snapshot_command", required=True)
    snapshot_commands.add_parser("verify")
    return parser


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "doctor":
        return doctor()
    if args.command == "setup":
        return setup(args.repo)
    if args.command == "check":
        return check(args.level, args.repo, cross=args.cross)
    if args.command == "snapshot" and args.snapshot_command == "verify":
        return snapshot_verify()
    raise HarnessError("unsupported command")


def main(argv: list[str] | None = None) -> int:
    try:
        _emit(dispatch(build_parser().parse_args(argv)))
        return 0
    except (HarnessError, OSError, subprocess.SubprocessError, ValueError) as exc:
        _emit({"error": str(exc), "status": "FAIL"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
