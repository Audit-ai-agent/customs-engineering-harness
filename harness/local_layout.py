"""Fail-closed local runtime layout and scratch ownership controls.

This module intentionally does not delete anything. It validates the V2 local
layout, creates explicitly owned per-run scratch directories, constructs an
isolated child-process environment, and produces a read-only expiry plan. A
separate, reviewed integration layer may consume that plan later.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "2.0"
MARKER_KIND = "customs-uat-managed-scratch"
EXPIRY_PLAN_KIND = "customs-uat-scratch-expiry-plan"
MARKER_NAME = ".customs-uat-scratch.json"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ENV_TEMPLATE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
VALID_OUTCOMES = {"succeeded", "failed"}
MANAGED_ENVIRONMENT_NAMES = {
    "CONDA_PREFIX",
    "COVERAGE_FILE",
    "HF_HOME",
    "HUGGINGFACE_HUB_CACHE",
    "HYPOTHESIS_STORAGE_DIRECTORY",
    "MYPY_CACHE_DIR",
    "NPM_CONFIG_CACHE",
    "PADDLEOCR_HOME",
    "PADDLE_HOME",
    "PIP_CACHE_DIR",
    "PLAYWRIGHT_BROWSERS_PATH",
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONPYCACHEPREFIX",
    "PYTEST_ADDOPTS",
    "RUFF_CACHE_DIR",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TORCH_HOME",
    "TRANSFORMERS_CACHE",
    "UV_CACHE_DIR",
    "UV_PROJECT_ENVIRONMENT",
    "VIRTUAL_ENV",
    "XDG_CACHE_HOME",
}


class LocalLayoutError(RuntimeError):
    """The local runtime layout could not be proven safe."""


@dataclass(frozen=True)
class LocalLayout:
    """Resolved, validated LocalLayoutV2 paths."""

    engineering: Path
    workspace: Path
    config_path: Path
    config_sha256: str
    local_root: Path
    artifacts_root: Path
    data: Path
    state: Path
    worktrees: Path
    transfers: Path
    secrets: Path
    cache_root: Path
    tools_root: Path
    scratch_root: Path
    failed_scratch_ttl_hours: int
    workspace_id: str

    @property
    def managed_output_roots(self) -> tuple[Path, ...]:
        return (
            self.artifacts_root,
            self.state,
            self.transfers,
            self.cache_root,
            self.scratch_root,
        )


@dataclass(frozen=True)
class ManagedScratch:
    """One run-owned scratch directory and its stable subpaths."""

    root: Path
    marker: Path
    run_id: str
    run_key: str
    workspace_id: str
    temporary: Path
    python_pycache: Path
    pytest_basetemp: Path
    pytest_cache: Path
    ruff_cache: Path
    mypy_cache: Path
    coverage: Path
    hypothesis: Path
    uv_project_environment: Path
    clean_install: Path
    source_staging: Path


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _native_path(path: Path) -> str:
    """Return a Win32 extended-length path for an already validated local path."""

    value = str(path.absolute())
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return f"\\\\?\\UNC\\{value[2:]}"
    return f"\\\\?\\{value}"


def _clear_windows_readonly_and_retry(
    function: Any,
    path: str,
    error: tuple[type[BaseException], BaseException, Any],
) -> None:
    """Retry removal only for a read-only entry inside a prevalidated scratch tree."""

    if os.name != "nt" or not isinstance(error[1], PermissionError):
        raise error[1]
    os.chmod(path, stat.S_IWRITE)
    function(path)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        raise LocalLayoutError("timestamps must be timezone-aware")
    return value.astimezone(dt.UTC)


def _utc_iso(value: dt.datetime) -> str:
    return _as_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(raw: object, label: str) -> dt.datetime:
    if not isinstance(raw, str) or not raw.endswith("Z"):
        raise LocalLayoutError(f"{label} must be a UTC timestamp ending in Z")
    try:
        parsed = dt.datetime.fromisoformat(raw.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise LocalLayoutError(f"{label} is not a valid timestamp") from exc
    return parsed.astimezone(dt.UTC)


def _filesystem_root(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    return Path(absolute.anchor)


def _is_filesystem_root(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    return absolute == _filesystem_root(absolute)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _is_reparse(stat_result: os.stat_result) -> bool:
    attributes = int(getattr(stat_result, "st_file_attributes", 0))
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return bool(flag and attributes & flag)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        result = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise LocalLayoutError(f"cannot inspect path safety: {path}") from exc
    return stat.S_ISLNK(result.st_mode) or _is_reparse(result)


def _assert_no_existing_link_between(path: Path, boundary: Path) -> None:
    current = Path(os.path.abspath(path))
    boundary_absolute = Path(os.path.abspath(boundary))
    if not _is_within(current, boundary_absolute):
        raise LocalLayoutError("path escaped its managed boundary")
    while True:
        if _is_link_or_reparse(current):
            raise LocalLayoutError(f"managed path contains a link or reparse point: {current}")
        if current == boundary_absolute:
            return
        parent = current.parent
        if parent == current:
            raise LocalLayoutError("managed path boundary could not be reached")
        current = parent


def _resolve_workspace_path(engineering: Path, workspace: Path, raw: object, label: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise LocalLayoutError(f"{label} must be a non-empty relative path")
    literal = Path(raw)
    if literal.is_absolute():
        raise LocalLayoutError(f"{label} must be relative to the harness checkout")
    candidate = (engineering / literal).resolve(strict=False)
    if not _is_within(candidate, workspace):
        raise LocalLayoutError(f"{label} escaped the workspace")
    if _is_filesystem_root(candidate):
        raise LocalLayoutError(f"{label} resolved to a filesystem root")
    return candidate


def _resolve_local_child(local_root: Path, raw: object, label: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise LocalLayoutError(f"{label} must be a non-empty relative path")
    literal = Path(raw)
    if literal.is_absolute() or ".." in literal.parts:
        raise LocalLayoutError(f"{label} must stay below workspace.local_root")
    candidate = (local_root / literal).resolve(strict=False)
    if not _is_within(candidate, local_root) or candidate == local_root:
        raise LocalLayoutError(f"{label} escaped workspace.local_root")
    return candidate


def _expand_environment_template(raw: object, environment: Mapping[str, str], label: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise LocalLayoutError(f"{label} must be a non-empty path template")

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = environment.get(name)
        if not value:
            raise LocalLayoutError(f"{label} requires environment variable {name}")
        return value

    expanded = ENV_TEMPLATE_RE.sub(replace, raw)
    if "${" in expanded:
        raise LocalLayoutError(f"{label} contains an invalid environment template")
    candidate = Path(expanded)
    if not candidate.is_absolute():
        raise LocalLayoutError(f"{label} must resolve to an absolute path")
    candidate = candidate.resolve(strict=False)
    if _is_filesystem_root(candidate):
        raise LocalLayoutError(f"{label} must not resolve to a filesystem root")
    return candidate


def _workspace_identity(workspace: Path) -> str:
    normalized = os.path.normcase(str(workspace.resolve(strict=False)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def load_local_layout(
    engineering: Path,
    workspace: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> LocalLayout:
    """Load and validate ``config/local-layout.toml`` without creating paths."""

    engineering = engineering.resolve(strict=False)
    workspace = workspace.resolve(strict=False)
    if not _is_within(engineering, workspace):
        raise LocalLayoutError("the harness checkout must be inside the workspace")
    config_path = engineering / "config" / "local-layout.toml"
    if not config_path.is_file():
        raise LocalLayoutError("config/local-layout.toml is required")
    with config_path.open("rb") as handle:
        config = tomllib.load(handle)
    if config.get("schema_version") != SCHEMA_VERSION:
        raise LocalLayoutError("unsupported local layout schema")

    workspace_config = config.get("workspace")
    external_config = config.get("external")
    retention_config = config.get("retention")
    if not isinstance(workspace_config, dict) or not isinstance(external_config, dict):
        raise LocalLayoutError("local layout workspace and external sections are required")
    if not isinstance(retention_config, dict):
        raise LocalLayoutError("local layout retention section is required")

    local_root = _resolve_workspace_path(
        engineering, workspace, workspace_config.get("local_root"), "workspace.local_root"
    )
    artifacts_root = _resolve_workspace_path(
        engineering,
        workspace,
        workspace_config.get("artifacts_root"),
        "workspace.artifacts_root",
    )
    if (
        local_root == artifacts_root
        or _is_within(local_root, artifacts_root)
        or _is_within(artifacts_root, local_root)
    ):
        raise LocalLayoutError("local_root and artifacts_root must not overlap")

    children = {
        name: _resolve_local_child(local_root, workspace_config.get(name), f"workspace.{name}")
        for name in ("data", "state", "worktrees", "transfers", "secrets")
    }
    if len(set(children.values())) != len(children):
        raise LocalLayoutError("workspace local child paths must be distinct")

    environment = os.environ if environment is None else environment
    cache_root = _expand_environment_template(
        external_config.get("cache_root"), environment, "external.cache_root"
    )
    tools_root = _expand_environment_template(
        external_config.get("tools_root"), environment, "external.tools_root"
    )
    scratch_root = _expand_environment_template(
        external_config.get("scratch_root"), environment, "external.scratch_root"
    )
    if tools_root == cache_root or not _is_within(tools_root, cache_root):
        raise LocalLayoutError("tools_root must be strictly below cache_root")
    _assert_no_existing_link_between(tools_root, cache_root)
    if (
        cache_root == scratch_root
        or _is_within(cache_root, scratch_root)
        or _is_within(scratch_root, cache_root)
    ):
        raise LocalLayoutError("cache_root and scratch_root must not overlap")
    for label, external_root in (("cache_root", cache_root), ("scratch_root", scratch_root)):
        if _is_within(external_root, workspace) or _is_within(workspace, external_root):
            raise LocalLayoutError(f"{label} must be outside the workspace")

    ttl = retention_config.get("failed_scratch_ttl_hours")
    if not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= 168:
        raise LocalLayoutError("failed_scratch_ttl_hours must be an integer from 1 to 168")

    return LocalLayout(
        engineering=engineering,
        workspace=workspace,
        config_path=config_path,
        config_sha256=file_sha256(config_path),
        local_root=local_root,
        artifacts_root=artifacts_root,
        data=children["data"],
        state=children["state"],
        worktrees=children["worktrees"],
        transfers=children["transfers"],
        secrets=children["secrets"],
        cache_root=cache_root,
        tools_root=tools_root,
        scratch_root=scratch_root,
        failed_scratch_ttl_hours=ttl,
        workspace_id=_workspace_identity(workspace),
    )


def guard_output_path(
    layout: LocalLayout,
    path: Path | str,
    *,
    purpose: str,
    allowed_roots: Sequence[Path] | None = None,
    allow_managed_root: bool = False,
) -> Path:
    """Return a safe absolute output path or reject it before any write occurs."""

    if not purpose.strip():
        raise LocalLayoutError("output purpose is required")
    raw = Path(path)
    if ".." in raw.parts:
        raise LocalLayoutError(f"{purpose} output path contains parent traversal")
    candidate = raw if raw.is_absolute() else layout.workspace / raw
    candidate = Path(os.path.abspath(candidate))
    if _is_filesystem_root(candidate):
        raise LocalLayoutError(f"{purpose} output must not be a filesystem or drive root")

    roots = layout.managed_output_roots if allowed_roots is None else tuple(allowed_roots)
    if not roots:
        raise LocalLayoutError(f"{purpose} has no approved output roots")
    for root in roots:
        resolved_root = root.resolve(strict=False)
        if _is_filesystem_root(resolved_root):
            raise LocalLayoutError(f"{purpose} approved root must not be a filesystem root")
        if not any(
            _is_within(resolved_root, managed_root) for managed_root in layout.managed_output_roots
        ):
            raise LocalLayoutError(f"{purpose} approved root is outside LocalLayoutV2")
    matching = [root.resolve(strict=False) for root in roots if _is_within(candidate, root)]
    if not matching:
        raise LocalLayoutError(f"{purpose} output is outside managed output roots")
    boundary = max(matching, key=lambda item: len(item.parts))
    if candidate.resolve(strict=False) == boundary and not allow_managed_root:
        raise LocalLayoutError(f"{purpose} output must be below, not equal to, its managed root")
    _assert_no_existing_link_between(candidate, boundary)
    return candidate.resolve(strict=False)


def _scratch_paths(root: Path, run_id: str, run_key: str, workspace_id: str) -> ManagedScratch:
    return ManagedScratch(
        root=root,
        marker=root / MARKER_NAME,
        run_id=run_id,
        run_key=run_key,
        workspace_id=workspace_id,
        temporary=root / "tmp",
        python_pycache=root / "python-pycache",
        pytest_basetemp=root / "pytest" / "basetemp",
        pytest_cache=root / "pytest" / "cache",
        ruff_cache=root / "ruff",
        mypy_cache=root / "mypy",
        coverage=root / "coverage",
        hypothesis=root / "hypothesis",
        uv_project_environment=root / "uv-project-environment",
        clean_install=root / "clean-install",
        source_staging=root / "source-staging",
    )


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_marker(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalLayoutError(f"managed scratch marker is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise LocalLayoutError("managed scratch marker must be a JSON object")
    return value


def _validate_marker(layout: LocalLayout, root: Path, marker: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "kind",
        "workspace_id",
        "run_id",
        "run_key",
        "status",
        "created_at",
        "expires_at",
    }
    if set(marker) != required:
        raise LocalLayoutError("managed scratch marker fields are not exact")
    if marker["schema_version"] != SCHEMA_VERSION or marker["kind"] != MARKER_KIND:
        raise LocalLayoutError("managed scratch marker identity is invalid")
    if marker["workspace_id"] != layout.workspace_id:
        raise LocalLayoutError("managed scratch belongs to a different workspace")
    run_id = marker["run_id"]
    run_key = marker["run_key"]
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        raise LocalLayoutError("managed scratch run_id is invalid")
    expected_key = hashlib.sha256(f"{layout.workspace_id}\0{run_id}".encode("utf-8")).hexdigest()[
        :8
    ]
    if run_key != expected_key or root.name != expected_key:
        raise LocalLayoutError("managed scratch run identity does not match its directory")
    if marker["status"] not in {"active", *VALID_OUTCOMES}:
        raise LocalLayoutError("managed scratch status is invalid")
    created = _parse_utc(marker["created_at"], "created_at")
    expires = _parse_utc(marker["expires_at"], "expires_at")
    if expires < created:
        raise LocalLayoutError("managed scratch expiry precedes creation")


def create_managed_scratch(
    layout: LocalLayout,
    run_id: str,
    *,
    now: dt.datetime | None = None,
) -> ManagedScratch:
    """Create a collision-resistant scratch tree with an exact ownership marker."""

    if RUN_ID_RE.fullmatch(run_id) is None:
        raise LocalLayoutError(
            "run_id must contain only letters, digits, dot, underscore, or hyphen"
        )
    now = _as_utc(now or _utc_now())
    run_key = hashlib.sha256(f"{layout.workspace_id}\0{run_id}".encode("utf-8")).hexdigest()[:8]
    root = guard_output_path(
        layout,
        layout.scratch_root / run_key,
        purpose="managed scratch",
        allowed_roots=(layout.scratch_root,),
    )
    layout.scratch_root.mkdir(parents=True, exist_ok=True)
    _assert_no_existing_link_between(layout.scratch_root, layout.scratch_root)
    try:
        root.mkdir()
    except FileExistsError as exc:
        raise LocalLayoutError(
            f"managed scratch run_key collision or active reuse: {run_key}"
        ) from exc

    scratch = _scratch_paths(root, run_id, run_key, layout.workspace_id)
    marker = {
        "schema_version": SCHEMA_VERSION,
        "kind": MARKER_KIND,
        "workspace_id": layout.workspace_id,
        "run_id": run_id,
        "run_key": run_key,
        "status": "active",
        "created_at": _utc_iso(now),
        "expires_at": _utc_iso(now + dt.timedelta(hours=layout.failed_scratch_ttl_hours)),
    }
    try:
        _write_json_atomic(scratch.marker, marker)
        for child in (
            scratch.temporary,
            scratch.python_pycache,
            scratch.pytest_basetemp,
            scratch.pytest_cache,
            scratch.ruff_cache,
            scratch.mypy_cache,
            scratch.coverage,
            scratch.hypothesis,
            scratch.clean_install,
            scratch.source_staging,
        ):
            child.mkdir(parents=True, exist_ok=False)
    except Exception:
        # Deliberately do not recursively delete an incompletely created tree.
        # Its marker, when present, lets a later audited process classify it.
        raise
    return scratch


def mark_managed_scratch_outcome(
    layout: LocalLayout,
    scratch: ManagedScratch,
    outcome: str,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Close an active scratch marker; no files are deleted by this operation."""

    if outcome not in VALID_OUTCOMES:
        raise LocalLayoutError("scratch outcome must be succeeded or failed")
    root = guard_output_path(
        layout,
        scratch.root,
        purpose="managed scratch outcome",
        allowed_roots=(layout.scratch_root,),
    )
    marker = _read_marker(root / MARKER_NAME)
    _validate_marker(layout, root, marker)
    if marker["run_id"] != scratch.run_id or marker["status"] != "active":
        raise LocalLayoutError("managed scratch is not the expected active run")
    now = _as_utc(now or _utc_now())
    marker["status"] = outcome
    marker["expires_at"] = _utc_iso(
        now if outcome == "succeeded" else now + dt.timedelta(hours=layout.failed_scratch_ttl_hours)
    )
    _write_json_atomic(root / MARKER_NAME, marker)
    return dict(marker)


def _portable(path: Path) -> str:
    return path.resolve(strict=False).as_posix()


def build_child_environment(
    layout: LocalLayout,
    scratch: ManagedScratch,
    *,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a child environment whose mutable runtime paths are all managed."""

    root = guard_output_path(
        layout,
        scratch.root,
        purpose="child runtime",
        allowed_roots=(layout.scratch_root,),
    )
    marker = _read_marker(root / MARKER_NAME)
    _validate_marker(layout, root, marker)
    if marker["status"] != "active" or marker["run_id"] != scratch.run_id:
        raise LocalLayoutError("child environment requires the expected active scratch")

    inherited_environment = os.environ if base_environment is None else base_environment
    environment = {
        key: value
        for key, value in inherited_environment.items()
        if key.upper() not in MANAGED_ENVIRONMENT_NAMES
    }

    cache_directories = {
        "UV_CACHE_DIR": layout.cache_root / "uv",
        "PIP_CACHE_DIR": layout.cache_root / "pip",
        "NPM_CONFIG_CACHE": layout.cache_root / "npm",
        "PLAYWRIGHT_BROWSERS_PATH": layout.cache_root / "playwright",
        "XDG_CACHE_HOME": layout.cache_root / "xdg",
        "HF_HOME": layout.cache_root / "huggingface",
        "HUGGINGFACE_HUB_CACHE": layout.cache_root / "huggingface" / "hub",
        "TRANSFORMERS_CACHE": layout.cache_root / "huggingface" / "transformers",
        "PADDLE_HOME": layout.cache_root / "paddle",
        "PADDLEOCR_HOME": layout.cache_root / "paddle" / "ocr",
        "TORCH_HOME": layout.cache_root / "torch",
    }
    layout.cache_root.mkdir(parents=True, exist_ok=True)
    _assert_no_existing_link_between(layout.cache_root, layout.cache_root)
    for directory in cache_directories.values():
        guard_output_path(
            layout,
            directory,
            purpose="dependency cache",
            allowed_roots=(layout.cache_root,),
        ).mkdir(parents=True, exist_ok=True)

    environment.update({name: str(path) for name, path in cache_directories.items()})
    environment.update(
        {
            "TEMP": str(scratch.temporary),
            "TMP": str(scratch.temporary),
            "TMPDIR": str(scratch.temporary),
            "PYTHONPYCACHEPREFIX": str(scratch.python_pycache),
            "RUFF_CACHE_DIR": str(scratch.ruff_cache),
            "MYPY_CACHE_DIR": str(scratch.mypy_cache),
            "COVERAGE_FILE": str(scratch.coverage / ".coverage"),
            "HYPOTHESIS_STORAGE_DIRECTORY": str(scratch.hypothesis),
            "UV_PROJECT_ENVIRONMENT": str(scratch.uv_project_environment),
            "PYTEST_ADDOPTS": " ".join(pytest_isolation_arguments(scratch)),
        }
    )
    return environment


def pytest_isolation_arguments(scratch: ManagedScratch) -> tuple[str, ...]:
    """Return shell-safe pytest options using forward-slash absolute paths."""

    basetemp = _portable(scratch.pytest_basetemp)
    cache = _portable(scratch.pytest_cache)
    return (
        f'--basetemp="{basetemp}"',
        f'--override-ini=cache_dir="{cache}"',
    )


def _tree_inventory_without_links(root: Path) -> dict[str, Any]:
    records: list[str] = []
    total_bytes = 0
    file_count = 0
    directory_count = 0

    def visit(current: Path, relative: Path) -> None:
        nonlocal total_bytes, file_count, directory_count
        try:
            result = os.lstat(_native_path(current))
        except OSError as exc:
            raise LocalLayoutError(f"cannot inventory managed scratch: {current}") from exc
        if stat.S_ISLNK(result.st_mode) or _is_reparse(result):
            raise LocalLayoutError("managed scratch contains a link or reparse point")
        if stat.S_ISREG(result.st_mode):
            file_count += 1
            total_bytes += result.st_size
            records.append(f"F\0{relative.as_posix()}\0{result.st_size}\0{result.st_mtime_ns}")
            return
        if not stat.S_ISDIR(result.st_mode):
            raise LocalLayoutError("managed scratch contains an unsupported filesystem object")
        directory_count += 1
        records.append(f"D\0{relative.as_posix()}\0{result.st_mtime_ns}")
        try:
            with os.scandir(_native_path(current)) as entries:
                child_names = sorted((entry.name for entry in entries), key=str.casefold)
        except OSError as exc:
            raise LocalLayoutError(f"cannot enumerate managed scratch: {current}") from exc
        for child_name in child_names:
            child = current / child_name
            visit(child, child.relative_to(root))

    visit(root, Path("."))
    return {
        "size_bytes": total_bytes,
        "file_count": file_count,
        "directory_count": directory_count,
        "inventory_sha256": hashlib.sha256("\n".join(records).encode("utf-8")).hexdigest(),
    }


def scratch_expiry_plan(
    layout: LocalLayout,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Plan expired owned scratch directories without deleting or changing them."""

    now = _as_utc(now or _utc_now())
    candidates: list[dict[str, Any]] = []
    ignored: list[dict[str, str]] = []
    if layout.scratch_root.exists():
        _assert_no_existing_link_between(layout.scratch_root, layout.scratch_root)
        for root in sorted(layout.scratch_root.iterdir(), key=lambda item: item.name.casefold()):
            if not root.is_dir() or _is_link_or_reparse(root):
                ignored.append({"name": root.name, "reason": "not_a_plain_directory"})
                continue
            marker_path = root / MARKER_NAME
            if not marker_path.is_file() or _is_link_or_reparse(marker_path):
                ignored.append({"name": root.name, "reason": "ownership_marker_missing"})
                continue
            try:
                marker = _read_marker(marker_path)
                _validate_marker(layout, root, marker)
                expiry = _parse_utc(marker["expires_at"], "expires_at")
                if expiry > now:
                    ignored.append({"name": root.name, "reason": "not_expired"})
                    continue
                inventory = _tree_inventory_without_links(root)
            except LocalLayoutError:
                ignored.append({"name": root.name, "reason": "ownership_or_inventory_invalid"})
                continue
            candidates.append(
                {
                    "path": str(root.resolve(strict=False)),
                    "run_id": marker["run_id"],
                    "run_key": marker["run_key"],
                    "status": marker["status"],
                    "expires_at": marker["expires_at"],
                    "marker_sha256": file_sha256(marker_path),
                    **inventory,
                }
            )

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": EXPIRY_PLAN_KIND,
        "generated_at": _utc_iso(now),
        "workspace_id": layout.workspace_id,
        "layout_config_sha256": layout.config_sha256,
        "scratch_root": str(layout.scratch_root),
        "candidates": candidates,
        "ignored": ignored,
        "deletion_performed": False,
    }
    payload["plan_sha256"] = canonical_sha256(payload)
    return payload


def remove_succeeded_scratch(layout: LocalLayout, scratch: ManagedScratch) -> None:
    """Remove only a marker-verified scratch tree already closed as succeeded."""

    root = guard_output_path(
        layout,
        scratch.root,
        purpose="successful scratch cleanup",
        allowed_roots=(layout.scratch_root,),
    )
    marker = _read_marker(root / MARKER_NAME)
    _validate_marker(layout, root, marker)
    if marker["run_id"] != scratch.run_id or marker["status"] != "succeeded":
        raise LocalLayoutError("only the expected succeeded scratch may be removed")
    _tree_inventory_without_links(root)
    shutil.rmtree(_native_path(root), onerror=_clear_windows_readonly_and_retry)
