# Validation Report 생성

기업 도입 시 요구되는 "무엇을, 어느 소스 버전에서, 어떤 절차로 검증했고 결과가 무엇인가"를 한 문서로
남기기 위한 명령이다. 제품 저장소가 이미 소유한 검사(`config/workspace.toml`의 `quick`·`contract`)를
그대로 실행하고, 저장소 간 계약 바이트를 대조하고, Git 밖에 보존된 UAT·Gold 증거를 인벤토리한 뒤
Markdown과 JSON으로 쓴다.

```powershell
python -B harness/validation_report.py                 # 4개 저장소 전체 검사 후 보고서 (수 분 소요)
python -B harness/validation_report.py --repo backend  # 한 저장소만
python -B harness/validation_report.py --skip-run      # 검사 없이 소스 식별·보유 증거만 기록
python -B harness/validation_report.py --output ..\_artifacts\validation\release-2026-09.md
```

기본 출력은 `../_artifacts/validation/validation-report-<날짜>.md`와 같은 이름의 `.json`이다.

## 보고서 구성

| 절 | 내용 | 출처 |
|---|---|---|
| 1 검증 대상 | 저장소별 브랜치·커밋·미커밋 변경 여부·원격 | `git` |
| 2 검증 절차 | L1 정적·계약 / L2 단위·제품 테스트 / L3 저장소 간 계약 / L4 시나리오·UAT 증거 / L5 추출·OCR Gold | 고정 정의 |
| 3 실행 결과 | 명령별 PASS/FAIL, 출력 요약(예: `336 passed, 4 skipped`), 소요 시간 | 실제 실행 출력 |
| 4 저장소 간 계약 | 제공자·소비자 계약 파일 SHA-256 일치 여부 | 파일 바이트 |
| 5 시나리오·UAT 증거 | `_local/state/scenario-runs` transcript 수, `_artifacts` UAT manifest 수 | 파일 시스템 |
| 6 추출·OCR Gold | 활성 pointer 상태, Gold 평가 문서 존재 | `_local/data/document-preview/approvals`, Agent docs |
| 7 제한 사항 | 워크스테이션 기록임, L4·L5는 존재·상태만 기록, 자동 판단 정확성 비주장 | 고정 문구 |
| 8 승인 | 검증 수행자·기술 검토자·품질 승인자 서명란 | 사람이 기입 |

## 경계

- 의존성을 설치하지 않고, 비밀정보를 읽지 않으며, 서비스를 시작하지 않는다. 하네스의 다른 명령과 같다.
- 실패한 검사는 예외가 아니라 FAIL 행으로 기록한다. 보고서는 "무엇이 실패했는가"를 남기는 문서이기 때문이다.
- 하네스 `setup` 마커가 없어도 저장소의 `.venv`가 있으면 실행한다. 이 경우에도 결과는 같은 명령의 출력이다.
- Functional-UAT 매트릭스와 장애 주입 매트릭스는 실제 서비스 조합이 필요하므로 자동 실행하지 않는다. 별도로 실행하면 5절의 transcript 수로 반영된다.
- 이 문서는 릴리스 승인이 아니다. 미커밋 변경이 있는 저장소는 커밋 후 다시 생성해야 커밋 해시로 재현 가능한 기록이 된다.
