"""Resolve immutable developer tools from the LocalLayoutV2 external cache.

The module never downloads or deletes tools.  It binds one logical tool name
to an exact version, executable path, and SHA-256 from ``versions.lock.toml``.
Installers and callers can therefore share one path contract without treating
an Artifact directory as a dependency cache.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    from harness import local_layout
except ModuleNotFoundError:  # Direct module execution/import in local tests.
    import local_layout  # type: ignore[no-redef]


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$")
TREE_INVENTORY_SCHEMA = "relative-path-size-file-sha256-lines-v1"


class ToolCacheError(RuntimeError):
    """A pinned tool identity or managed path could not be proven safe."""


@dataclass(frozen=True)
class PinnedTool:
    name: str
    version: str
    executable: Path
    executable_sha256: str
    tools_root: Path
    install_root: Path
    distribution_inventory_schema: str | None
    distribution_tree_sha256: str | None
    distribution_file_count: int | None
    distribution_bytes: int | None


def _read_lock(engineering: Path) -> dict[str, Any]:
    path = engineering / "config" / "versions.lock.toml"
    if not path.is_file():
        raise ToolCacheError("config/versions.lock.toml is required")
    try:
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ToolCacheError("config/versions.lock.toml is unreadable") from exc
    if not isinstance(payload, dict):
        raise ToolCacheError("versions lock must be a TOML table")
    return payload


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ToolCacheError(f"{label} must be a TOML table")
    return value


def _locked_string(table: Mapping[str, Any], key: str, label: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolCacheError(f"{label}.{key} must be a non-empty string")
    return value.strip()


def _locked_nonnegative_int(table: Mapping[str, Any], key: str, label: str) -> int:
    value = table.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ToolCacheError(f"{label}.{key} must be a non-negative integer")
    return value


def _identity(
    lock: Mapping[str, Any],
    name: str,
) -> tuple[str, str, Path, str | None, int | None, int | None]:
    tools = _mapping(lock.get("tools"), "tools")
    reproducibility = _mapping(lock.get("reproducibility"), "reproducibility")
    scanners = _mapping(lock.get("security_scanners"), "security_scanners")

    if name == "uv":
        version = _locked_string(lock, "uv", "root")
        table = _mapping(tools.get("uv_windows_x64"), "tools.uv_windows_x64")
        digest = _locked_string(table, "executable_sha256", "tools.uv_windows_x64")
        relative = Path("uv") / f"v{version}" / "uv.exe"
        tree_sha256 = None
        tree_file_count = None
        tree_bytes = None
    elif name in {"node", "npm"}:
        version = _locked_string(reproducibility, "frontend_node", "reproducibility")
        table = _mapping(tools.get("node_windows_x64"), "tools.node_windows_x64")
        digest_key = "executable_sha256" if name == "node" else "npm_cmd_sha256"
        digest = _locked_string(table, digest_key, "tools.node_windows_x64")
        filename = "node.exe" if name == "node" else "npm.cmd"
        relative = Path("node") / f"v{version}" / filename
        inventory_schema = _locked_string(
            table,
            "distribution_inventory_schema",
            "tools.node_windows_x64",
        )
        if inventory_schema != TREE_INVENTORY_SCHEMA:
            raise ToolCacheError("unsupported Node distribution inventory schema")
        tree_sha256 = _locked_string(
            table,
            "distribution_tree_sha256",
            "tools.node_windows_x64",
        ).lower()
        tree_file_count = _locked_nonnegative_int(
            table,
            "distribution_file_count",
            "tools.node_windows_x64",
        )
        tree_bytes = _locked_nonnegative_int(
            table,
            "distribution_bytes",
            "tools.node_windows_x64",
        )
    elif name == "gitleaks":
        version = _locked_string(scanners, "gitleaks", "security_scanners")
        digest = _locked_string(scanners, "gitleaks_exe_sha256", "security_scanners")
        relative = Path("gitleaks") / f"v{version}" / "bin" / "gitleaks.exe"
        tree_sha256 = None
        tree_file_count = None
        tree_bytes = None
    else:
        raise ToolCacheError(f"unsupported managed tool: {name}")

    if not VERSION_RE.fullmatch(version):
        raise ToolCacheError(f"{name} version is not a safe path component")
    digest = digest.lower()
    if not SHA256_RE.fullmatch(digest):
        raise ToolCacheError(f"{name} executable SHA-256 is invalid")
    if tree_sha256 is not None and not SHA256_RE.fullmatch(tree_sha256):
        raise ToolCacheError(f"{name} distribution tree SHA-256 is invalid")
    return version, digest, relative, tree_sha256, tree_file_count, tree_bytes


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse(stat_result: os.stat_result) -> bool:
    attributes = int(getattr(stat_result, "st_file_attributes", 0))
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return bool(flag and attributes & flag)


def tool_tree_inventory(root: Path) -> tuple[str, int, int]:
    """Hash sorted ``relative-path|size|file-sha`` records for one tool tree."""

    # Do not resolve first: resolving a junction/symlink would hide the object
    # that this inventory must reject.
    root = Path(os.path.abspath(root))
    if not root.is_dir():
        raise ToolCacheError("managed tool distribution root is missing")
    records: list[str] = []
    total_bytes = 0

    def visit(current: Path) -> None:
        nonlocal total_bytes
        try:
            current_stat = current.lstat()
        except OSError as exc:
            raise ToolCacheError("managed tool distribution is unreadable") from exc
        if stat.S_ISLNK(current_stat.st_mode) or _is_reparse(current_stat):
            raise ToolCacheError("managed tool distribution contains a link or reparse point")
        if stat.S_ISREG(current_stat.st_mode):
            relative = current.relative_to(root).as_posix()
            if not relative.isascii() or any(character in relative for character in "|\r\n"):
                raise ToolCacheError("managed tool path is outside the canonical inventory format")
            size = current_stat.st_size
            try:
                digest = file_sha256(current)
            except OSError as exc:
                raise ToolCacheError("managed tool distribution file is unreadable") from exc
            records.append(f"{relative}|{size}|{digest}")
            total_bytes += size
            return
        if not stat.S_ISDIR(current_stat.st_mode):
            raise ToolCacheError("managed tool distribution contains an unsupported object")
        try:
            children = sorted(current.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise ToolCacheError("managed tool distribution cannot be enumerated") from exc
        for child in children:
            visit(child)

    visit(root)
    records.sort()
    payload = ("\n".join(records) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest(), len(records), total_bytes


def resolve_pinned_tool(
    engineering: Path,
    workspace: Path,
    name: str,
    *,
    environment: Mapping[str, str] | None = None,
    require_installed: bool = True,
    verify_sha256: bool = True,
    verify_distribution: bool = True,
    versions_lock: Mapping[str, Any] | None = None,
) -> PinnedTool:
    """Return a path/hash-bound tool or fail closed.

    ``require_installed=False`` is intended only for bootstrappers that need to
    calculate the canonical destination before the executable exists.
    """

    engineering = engineering.resolve(strict=False)
    workspace = workspace.resolve(strict=False)
    try:
        layout = local_layout.load_local_layout(
            engineering,
            workspace,
            environment=os.environ if environment is None else environment,
        )
        (
            version,
            expected_sha256,
            relative,
            distribution_tree_sha256,
            distribution_file_count,
            distribution_bytes,
        ) = _identity(
            _read_lock(engineering) if versions_lock is None else versions_lock,
            name,
        )
        executable = local_layout.guard_output_path(
            layout,
            layout.tools_root / relative,
            purpose=f"{name} executable",
            allowed_roots=(layout.tools_root,),
        )
    except local_layout.LocalLayoutError as exc:
        raise ToolCacheError(str(exc)) from exc

    if executable.exists() and not executable.is_file():
        raise ToolCacheError(f"managed {name} executable is not a regular file")
    if require_installed and not executable.is_file():
        raise ToolCacheError(f"managed {name} executable is missing")
    if executable.is_file() and verify_sha256:
        try:
            actual_sha256 = file_sha256(executable)
        except OSError as exc:
            raise ToolCacheError(f"managed {name} executable is unreadable") from exc
        if actual_sha256 != expected_sha256:
            raise ToolCacheError(f"managed {name} executable checksum mismatch")

    install_root = executable.parents[0] if name in {"uv", "node", "npm"} else executable.parents[1]
    if executable.is_file() and distribution_tree_sha256 is not None and verify_distribution:
        actual_tree_sha256, actual_file_count, actual_bytes = tool_tree_inventory(install_root)
        if (
            actual_tree_sha256 != distribution_tree_sha256
            or actual_file_count != distribution_file_count
            or actual_bytes != distribution_bytes
        ):
            raise ToolCacheError(f"managed {name} distribution inventory mismatch")

    return PinnedTool(
        name=name,
        version=version,
        executable=executable,
        executable_sha256=expected_sha256,
        tools_root=layout.tools_root,
        install_root=install_root,
        distribution_inventory_schema=(
            TREE_INVENTORY_SCHEMA if distribution_tree_sha256 is not None else None
        ),
        distribution_tree_sha256=distribution_tree_sha256,
        distribution_file_count=distribution_file_count,
        distribution_bytes=distribution_bytes,
    )
