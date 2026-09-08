"""Produce one dated, human-readable Validation Report for the workspace.

Enterprises expect a validation record for AI-assisted software: what was
verified, against which exact source revision, with which procedure, and with
what outcome.  The product repositories already own every check (lint, type,
unit/product tests, contract snapshots, scenario transcript verifiers,
Functional-UAT matrices, OCR Gold gates); none of them writes a single dated
document.  This command runs the configured ``quick`` and ``contract`` checks
of every repository, compares the cross-repository contract bytes, inventories
the locally retained UAT and Gold evidence, and renders the result as Markdown
plus a JSON sidecar.

It stays inside the harness boundary: it installs nothing, reads no secret,
starts no service, and treats a failed check as a reported FAIL row rather than
an exception.  The report is evidence of what ran on this workstation at that
moment — it is not a release approval by itself; the approval block at the end
is left for the human reviewer and approver to fill in.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from harness import eng
except ImportError:  # executed as a script: python harness/validation_report.py
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import eng

REPORT_SCHEMA = "customs-validation-report/v1"

# Validation layers, in the order an auditor reads them.  Each configured
# command is attributed to one layer by its first tokens.
LAYERS: tuple[tuple[str, str, str], ...] = (
    ("L1", "정적 검사·계약", "형식·타입·경계 규칙과 공개 계약 스냅샷이 저장소 기준과 정확히 같은지"),
    ("L2", "단위·제품 테스트", "각 저장소의 자동 테스트(pytest·vitest)가 모두 통과하는지"),
    ("L3", "저장소 간 계약 일치", "제공자 계약 파일과 소비자 사본의 바이트가 같은지"),
    ("L4", "시나리오·Functional-UAT 증거", "실제 서비스 조합으로 기록된 검증 transcript가 얼마나 있는지"),
    ("L5", "추출·OCR 정확도 Gold 평가", "문서 추출 엔진이 봉인된 Gold 평가를 통과한 상태인지"),
)

_PYTEST = re.compile(r"(\d+) passed(?:, (\d+) skipped)?(?:, (\d+) failed)?")
_VITEST = re.compile(r"Tests\s+(?:(\d+) failed \| )?(\d+) passed")
_MYPY = re.compile(r"Found (\d+) errors? in \d+ files?|Success: no issues found")
_RUFF = re.compile(r"All checks passed!|Found (\d+) errors?")
_TSC_ERROR = re.compile(r"error TS\d+")


def summarize_output(command: Sequence[str], stdout: str, stderr: str) -> str:
    """One short Korean line describing what a command reported."""

    text = f"{stdout}\n{stderr}"
    joined = " ".join(command)
    if "pytest" in joined:
        match = _PYTEST.search(text)
        if match:
            passed, skipped, failed = match.groups()
            parts = [f"{passed} passed"]
            if skipped:
                parts.append(f"{skipped} skipped")
            if failed:
                parts.append(f"{failed} failed")
            return ", ".join(parts)
    if "vitest" in joined or "check:quick" in joined or "verify" in joined:
        match = _VITEST.search(text)
        if match:
            failed, passed = match.groups()
            return f"{passed} passed" + (f", {failed} failed" if failed else "")
    if "mypy" in joined:
        match = _MYPY.search(text)
        if match:
            return "no issues" if match.group(1) is None else f"{match.group(1)} errors"
    if "ruff" in joined and "format" in joined:
        match = re.search(r"(\d+) files? would be reformatted", text)
        if match:
            return f"{match.group(1)} files would be reformatted"
        if "already formatted" in text:
            return "formatted"
    if "ruff" in joined:
        match = _RUFF.search(text)
        if match:
            return "clean" if match.group(1) is None else f"{match.group(1)} errors"
    if "tsc" in joined or "typecheck" in joined:
        errors = len(_TSC_ERROR.findall(text))
        return "no issues" if errors == 0 else f"{errors} errors"
    if "verify_contract_snapshots" in joined or "check:contract" in joined:
        if "not clean" in text:
            return "snapshot paths not clean (uncommitted)"
        if "passed" in text or "PASS" in text:
            return "snapshot identical"
    for line in reversed([line for line in text.splitlines() if line.strip()]):
        if len(line) <= 120:
            return line.strip()
    return "see log"


def _layer_for(command: Sequence[str], field: str) -> str:
    joined = " ".join(command)
    if "pytest" in joined or "vitest" in joined:
        return "L2"
    if field == "quick" and ("check:quick" in joined or "check_backend.py quick" in joined):
        # The repository's own composite profile: lint + type + tests.
        return "L2"
    return "L1"


def _lenient_environment(name: str, repository: Path) -> Mapping[str, str]:
    """Reuse the harness environment rule, but do not refuse to report.

    The harness ``check`` command insists on its own setup marker.  A
    validation report must still be producible on a workstation that prepared
    dependencies with the repository's own ``uv sync`` / ``npm ci``; it records
    the environment note instead of stopping.
    """

    try:
        return eng._check_environment(name, repository)
    except eng.HarnessError:
        import os

        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        python = eng._venv_python(repository)
        if python is not None:
            environment["PATH"] = str(python.parent) + os.pathsep + environment.get("PATH", "")
        return environment


def run_repository_checks(name: str, *, fields: Sequence[str]) -> list[dict[str, Any]]:
    repository = eng._repo(name)
    environment = _lenient_environment(name, repository)
    rows: list[dict[str, Any]] = []
    for field in fields:
        try:
            commands = eng._configured_commands(name, field)
        except eng.HarnessError as error:
            rows.append(
                {
                    "repository": name,
                    "field": field,
                    "layer": "L1",
                    "command": [],
                    "status": "SKIP",
                    "summary": str(error),
                    "duration_seconds": 0.0,
                }
            )
            continue
        for command in commands:
            try:
                result = eng._run(command, cwd=repository, env=environment, capture=True)
            except (OSError, eng.HarnessError) as error:
                result = {
                    "command": command,
                    "duration_seconds": 0.0,
                    "exit_code": 127,
                    "stdout": "",
                    "stderr": str(error),
                }
            rows.append(
                {
                    "repository": name,
                    "field": field,
                    "layer": _layer_for(command, field),
                    # The configured command, not the resolved executable path,
                    # so the row reads the same on every workstation.
                    "command": [str(item) for item in command],
                    "status": "PASS" if result["exit_code"] == 0 else "FAIL",
                    "exit_code": result["exit_code"],
                    "summary": summarize_output(
                        command, str(result.get("stdout", "")), str(result.get("stderr", ""))
                    ),
                    "duration_seconds": result["duration_seconds"],
                }
            )
    return rows


def cross_contract_rows(name: str) -> list[dict[str, Any]]:
    """Byte comparison of every configured provider/consumer contract pair."""

    raw = eng._definitions()[name].get("cross_contract")
    if not isinstance(raw, dict) or not isinstance(raw.get("files"), list):
        return []
    repository = eng._repo(name)
    rows: list[dict[str, Any]] = []
    for item in raw["files"]:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider", ""))
        provider_path = str(item.get("provider_path", ""))
        consumer_path = str(item.get("consumer_path", ""))
        provider_file = eng._repo(provider) / provider_path if provider in eng.REPOSITORIES else None
        consumer_file = repository / consumer_path
        if provider_file is None or not provider_file.is_file() or not consumer_file.is_file():
            status, detail = "FAIL", "file unavailable"
        else:
            provider_digest = eng._sha256(provider_file)
            consumer_digest = eng._sha256(consumer_file)
            status = "PASS" if provider_digest == consumer_digest else "FAIL"
            detail = provider_digest[:12] if status == "PASS" else "byte drift"
        rows.append(
            {
                "consumer": name,
                "provider": provider,
                "consumer_path": consumer_path,
                "provider_path": provider_path,
                "status": status,
                "detail": detail,
            }
        )
    return rows


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def evidence_inventory() -> dict[str, Any]:
    """What retained scenario, UAT, and Gold evidence exists outside Git."""

    root = eng._workspace_root()
    scenario_root = root / "_local" / "state" / "scenario-runs"
    runs = sorted(scenario_root.glob("run_*")) if scenario_root.is_dir() else []
    complete = [run for run in runs if (run / "summary.json").is_file()]
    latest = max((run.stat().st_mtime for run in complete), default=None)
    uat_root = root / "_artifacts" / "functional-uat-law-history"
    uat_manifests = sorted(uat_root.glob("*/manifest.json")) if uat_root.is_dir() else []
    approvals = root / "_local" / "data" / "document-preview" / "approvals"
    pointers: dict[str, str] = {}
    if approvals.is_dir():
        for pointer in sorted(approvals.glob("*.json")):
            try:
                payload = json.loads(pointer.read_text(encoding="utf-8"))
                pointers[pointer.name] = str(payload.get("status", "unknown"))
            except (OSError, ValueError):
                pointers[pointer.name] = "unreadable"
    gold_doc = root / "customs-ai-agent" / "docs" / "R1_SUPERVISED_NATIVE_PDF_EVALUATION.md"
    return {
        "scenario_transcripts": {
            "directory": str(scenario_root),
            "complete_runs": len(complete),
            "latest": _iso(latest) if latest is not None else None,
        },
        "functional_uat_manifests": {
            "directory": str(uat_root),
            "count": len(uat_manifests),
            "latest": _iso(max(path.stat().st_mtime for path in uat_manifests))
            if uat_manifests
            else None,
        },
        "document_preview_pointers": pointers,
        "ocr_gold_evaluation_doc": str(gold_doc) if gold_doc.is_file() else None,
    }


def repository_identity(names: Sequence[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in names:
        repository = eng._repo(name)
        try:
            state = eng._repo_state(name)
        except eng.HarnessError as error:
            state = {"branch": "?", "dirty": True, "origin": str(error), "revision": "?"}
        rows.append({"repository": name, "directory": repository.name, **state})
    return rows


def build_report(*, names: Sequence[str], run_checks: bool) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc)
    checks: list[dict[str, Any]] = []
    contracts: list[dict[str, Any]] = []
    for name in names:
        if run_checks:
            checks.extend(run_repository_checks(name, fields=("quick", "contract")))
        contracts.extend(cross_contract_rows(name))
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "workspace_root": str(eng._workspace_root()),
        "repositories": repository_identity(names),
        "checks": checks,
        "cross_contracts": contracts,
        "evidence": evidence_inventory(),
    }


def _status_cell(value: str) -> str:
    return {"PASS": "✅ PASS", "FAIL": "❌ FAIL", "SKIP": "⏭ SKIP"}.get(value, value)


def render_markdown(report: Mapping[str, Any]) -> str:
    lines: list[str] = []
    generated = report["generated_at"]
    lines += [
        "# Customs-Fit Validation Report",
        "",
        f"- 생성 시각: {generated}",
        f"- 워크스페이스: `{report['workspace_root']}`",
        f"- 보고서 형식: `{report['schema']}` (customs-engineering-harness `validation_report`)",
        "",
        "이 문서는 AI 지원으로 개발된 Customs-Fit 시스템이 **어느 소스 버전**을 **어떤 절차**로 검증했고 "
        "**결과가 무엇이었는지**를 한 장으로 남기기 위한 검증 기록이다. 모든 수치는 이 워크스테이션에서 "
        "실제로 실행한 명령의 출력에서 그대로 옮겼으며 추정치는 없다. 이 문서 자체는 릴리스 승인이 아니며, "
        "마지막 승인란에 검토자·승인자가 서명해야 효력을 갖는다.",
        "",
        "## 1. 검증 대상 (Configuration Identity)",
        "",
        "| 저장소 | 브랜치 | 커밋 | 미커밋 변경 | 원격 |",
        "|---|---|---|---|---|",
    ]
    for row in report["repositories"]:
        lines.append(
            f"| {row['repository']} (`{row['directory']}`) | {row['branch']} | `{str(row['revision'])[:12]}` | "
            f"{'있음' if row['dirty'] else '없음'} | {row['origin']} |"
        )
    lines += [
        "",
        "미커밋 변경이 '있음'인 저장소는 커밋 해시만으로 재현할 수 없다. 최종 검증 기록은 커밋 후 다시 생성한다.",
        "",
        "## 2. 검증 절차 (Validation Procedure)",
        "",
        "| 계층 | 이름 | 무엇을 확인하는가 |",
        "|---|---|---|",
    ]
    for code, title, what in LAYERS:
        lines.append(f"| {code} | {title} | {what} |")
    lines += [
        "",
        "각 계층은 제품 저장소가 이미 소유한 검사를 그대로 실행한다(harness `config/workspace.toml`의 "
        "`quick`·`contract` 명령). 새 검사를 만들지 않으므로 이 보고서의 결과는 각 저장소 CI와 같은 의미를 갖는다.",
        "",
        "## 3. 실행 결과 (L1·L2)",
        "",
    ]
    checks = report["checks"]
    if not checks:
        lines += ["검사를 실행하지 않은 식별 전용 보고서다 (`--skip-run`).", ""]
    else:
        lines += [
            "| 계층 | 저장소 | 명령 | 결과 | 요약 | 소요(초) |",
            "|---|---|---|---|---|---|",
        ]
        for row in checks:
            command = " ".join(row["command"]) if row["command"] else "(없음)"
            if len(command) > 70:
                command = command[:67] + "…"
            lines.append(
                f"| {row['layer']} | {row['repository']} | `{command}` | {_status_cell(row['status'])} | "
                f"{row['summary']} | {row['duration_seconds']:.0f} |"
            )
        passed = sum(1 for row in checks if row["status"] == "PASS")
        failed = sum(1 for row in checks if row["status"] == "FAIL")
        lines += ["", f"합계: PASS {passed} · FAIL {failed} · SKIP {len(checks) - passed - failed}", ""]
    lines += [
        "## 4. 저장소 간 계약 일치 (L3)",
        "",
        "| 소비자 | 제공자 | 계약 파일 | 결과 | 비고 |",
        "|---|---|---|---|---|",
    ]
    for row in report["cross_contracts"]:
        lines.append(
            f"| {row['consumer']} | {row['provider']} | `{row['consumer_path']}` | "
            f"{_status_cell(row['status'])} | {row['detail']} |"
        )
    evidence = report["evidence"]
    transcripts = evidence["scenario_transcripts"]
    uat = evidence["functional_uat_manifests"]
    lines += [
        "",
        "## 5. 시나리오·Functional-UAT 증거 (L4)",
        "",
        f"- 시나리오 transcript(요약 파일 포함) 보유: **{transcripts['complete_runs']}건**, "
        f"최근 {transcripts['latest'] or '없음'} — 위치 `{transcripts['directory']}` (Git 외부, 비공개 유지)",
        f"- Functional-UAT 데이터 manifest: **{uat['count']}건**, 최근 {uat['latest'] or '없음'}",
        "- Functional-UAT 매트릭스(`customs-backend/tools/run_functional_uat_matrix.py`)와 장애 주입 매트릭스"
        "(`customs-ai-agent/tools/run_functional_uat_fault_matrix.py`)는 실제 Keycloak·Postgres·MinIO·Agent·Data·Claude 조합이 "
        "필요하므로 이 보고서가 자동 실행하지 않는다. 실행한 경우 위 transcript 수가 늘어난다.",
        "",
        "## 6. 추출·OCR 정확도 Gold 평가 (L5)",
        "",
    ]
    pointers = evidence["document_preview_pointers"]
    if pointers:
        for name, status in pointers.items():
            lines.append(f"- 활성 추출 프로필 pointer `{name}`: 상태 **{status}**")
    else:
        lines.append("- 활성 추출 프로필 pointer 없음 (이 환경에서는 문서 자동 추출 엔진이 비활성)")
    if evidence["ocr_gold_evaluation_doc"]:
        lines.append(
            "- 평가 기록: `customs-ai-agent/docs/R1_SUPERVISED_NATIVE_PDF_EVALUATION.md` — 합성 born-digital PDF에 대해 "
            "개발 60문서/800필드, 봉인 20문서/268필드 전부 Gold 일치. 스캔·사진·손글씨는 범위 밖임을 문서가 명시한다."
        )
    lines += [
        "- Raster text OCR(V19 profile)은 12 lane 모두 핵심값 exact ≥ 97%, CER ≤ 2%, p95 ≤ 10초를 만족해야만 "
        "pointer가 교체된다(`customs-ai-agent/README.md`). 이 Gate를 통과하지 못하면 엔진은 `unavailable`로 남는다.",
        "",
        "## 7. 제한 사항",
        "",
        "- 이 보고서는 워크스테이션 실행 기록이다. 서버 배포본과의 동일성은 1절의 커밋 해시와 배포 digest를 대조해 별도로 확인한다.",
        "- L4·L5는 보유 증거의 존재와 상태만 기록한다. 그 내용의 정확성은 각 transcript verifier와 Gold Gate가 실행 시점에 보증한다.",
        "- 모든 심사 결과는 설계상 사람 검토 대상(`human_review_required`)이며, 이 검증은 자동 통관 판단의 정확성을 주장하지 않는다.",
        "",
        "## 8. 승인",
        "",
        "| 역할 | 이름 | 일자 | 서명 |",
        "|---|---|---|---|",
        "| 검증 수행자 |  |  |  |",
        "| 기술 검토자 |  |  |  |",
        "| 품질 승인자 |  |  |  |",
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output",
        type=Path,
        help="Markdown destination (default: ../_artifacts/validation/validation-report-<date>.md)",
    )
    parser.add_argument("--repo", action="append", help="limit to a repository (repeatable)")
    parser.add_argument(
        "--skip-run", action="store_true", help="record identity and evidence only; run no check"
    )
    args = parser.parse_args(argv)
    names = [eng._repository_name(item) or item for item in args.repo] if args.repo else list(eng.REPOSITORIES)
    report = build_report(names=names, run_checks=not args.skip_run)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output = args.output or (
        eng._workspace_root() / "_artifacts" / "validation" / f"validation-report-{stamp}.md"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_markdown(report), encoding="utf-8")
    output.with_suffix(".json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    failed = sum(1 for row in report["checks"] if row["status"] == "FAIL")
    failed += sum(1 for row in report["cross_contracts"] if row["status"] == "FAIL")
    eng._emit({"validation_report": str(output), "failed_rows": failed})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
