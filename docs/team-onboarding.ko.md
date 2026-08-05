# 팀 개발 시작 안내

이 문서는 새 팀원이 무엇을 받아야 하는지, 무엇을 받으면 안 되는지, 그리고
기본 개발과 실제 Functional-UAT를 어떻게 구분하는지를 설명한다. 실행 명령의
정본은 [local setup](local-setup.md)이다.

## 결론

일상적인 전체 서비스 개발에는 GitHub 저장소 접근 권한과 개발 도구만 필요하다.
공유 `.env`, 계약 ZIP, Docker volume, DB dump, 모델 cache, 법령 snapshot은
필요하지도 않고 전달해서도 안 된다.

실제 관세 3법·Claude 기능 UAT를 명시적으로 수행하는 사람에게만 다음 두 외부
입력이 필요하다.

| 목적 | 개인별 외부 입력 | 로컬 보관 위치 |
| --- | --- | --- |
| 관세 3법 수집 | 승인된 `LAW_API_OC` | `_local/profiles/functional-uat-3law/config.env` |
| 실제 Claude 심사 | `ANTHROPIC_API_KEY` 및 사용 가능 크레딧 | `_local/secrets/.env` |

두 값은 승인된 비밀 전달 경로로 개인에게 발급한다. 다른 팀원의 `.env` 파일을
복사하거나 채팅·Git·공유 ZIP으로 전달하지 않는다.

## 하네스와 실행 경계

팀원이 사용하는 유일한 공용 하네스는
`customs-engineering-harness`다. 여기서 `doctor`, `snapshot verify`, `setup`,
`check quick`, `check contract`만 실행한다. 서비스 실행과 `dev-fixture`,
Functional-UAT는 각 제품 저장소 README의 명령이 소유한다.

`_engineering/`은 개인 Task·결정·지식·과거 근거를 보존하는 로컬 디렉터리다.
팀원이 clone하거나 실행할 저장소가 아니며, 옛 개인 UAT 실행 명령은 사용하지
않는다.

Functional-UAT를 실행하면 비밀을 제거한 상세 transcript가 각 실행 ID별로
`_local/state/scenario-runs/`에만 남는다. `scenario.json`, public API,
Backend→Agent, Agent→Data, Claude, summary 파일을 포함할 수 있으나 Git, PR,
채팅, 공유 ZIP에는 절대 넣지 않는다.

## 팀원에게 전달할 것

### GitHub 접근 권한

아래 다섯 private 저장소에 접근 권한을 부여한다.

```text
Audit-ai-agent/customs-engineering-harness
Audit-ai-agent/customs-frontend
Audit-ai-agent/customs-backend
Audit-ai-agent/customs-ai-agent
Audit-ai-agent/customs-data-platform
```

- 모든 팀원은 다섯 저장소를 읽고 clone할 수 있어야 한다.
- 수정 담당 저장소에만 `Write` 권한을 부여하면 된다. 관리자·배포 권한은 필요 없다.
- GitHub HTTPS credential/PAT 또는 SSH 키는 각 팀원이 자신의 계정으로 준비한다.
  다른 사람의 GitHub credential을 공유하지 않는다.

### 개발 도구

각 PC에 다음을 한 번 설치한다.

```text
Git 2.40 이상
Python 3.11
uv
Node.js >=22.12, <23
Corepack
최신 Browser
```

Node 설치 후 Corepack이 비활성화되어 있으면 한 번만 실행한다.

```powershell
corepack enable
```

기본 개발에는 Docker Desktop, Law API, Claude 키, Claude 크레딧, 모델 cache가
필요 없다. Docker Desktop은 Data store 구현 작업이나 실제 Functional-UAT를
수행하는 담당자만 필요하다.

## 절대 전달하지 않을 것

다음은 Git 밖의 개인·PC별 상태이므로 팀원에게 전달하거나 저장소에 올리지 않는다.

```text
_local/ 전체와 그 안의 .env, config.env, backend.env, service token
Docker volume, Docker image archive, docker_data.vhdx
DB dump, 법령 원문·snapshot, 모델 cache
_artifacts/, _engineering/, 과거 UAT bundle·결과
```

특히 과거 `law_repository_*` Docker volume은 Legacy UAT 기록일 뿐이다. 현재
Functional-UAT profile의 입력이나 팀원 전달물로 사용하지 않는다.

계약 자체는 없어지지 않는다. Frontend–Backend, Backend–Agent, Agent–Data의 HTTP
schema와 lockfile은 각 제품 저장소에 추적되어 있으며 Harness가 변경 시 검증한다.
따라서 별도의 계약 파일 묶음, commit pin, 서명·승인 파일을 전달할 필요가 없다.

## 기본 개발 시작

다섯 저장소를 같은 상위 폴더의 형제 디렉터리로 clone한다. HTTPS와 SSH URL 모두
가능하다. 예시는 HTTPS다.

```powershell
mkdir project
Set-Location project

git clone https://github.com/Audit-ai-agent/customs-engineering-harness.git
git clone https://github.com/Audit-ai-agent/customs-frontend.git
git clone https://github.com/Audit-ai-agent/customs-backend.git
git clone https://github.com/Audit-ai-agent/customs-ai-agent.git
git clone https://github.com/Audit-ai-agent/customs-data-platform.git
```

필수 구조는 다음과 같다.

```text
project/
  customs-engineering-harness/
  customs-frontend/
  customs-backend/
  customs-ai-agent/
  customs-data-platform/
```

처음에는 Harness에서 기준선과 의존성을 준비한다.

```powershell
Set-Location .\customs-engineering-harness
.\harness\eng.ps1 doctor
.\harness\eng.ps1 snapshot verify
.\harness\eng.ps1 setup
```

`snapshot verify`는 모든 저장소가 깨끗한 `main`일 때, 작업 브랜치를 만들기 전에
한 번 실행한다. `setup`만 의존성을 설치·재사용한다. `check`는 설치·삭제·cache
정리를 하지 않는다.

일상적인 전체 서비스 실행은 `dev-fixture`를 사용한다. 이 흐름은 실제
Frontend → Backend → Agent → Data HTTP 연결을 실행하지만, 합성 Evidence와
결정론 Agent를 사용한다. 각 팀원이 자신의 PC에서 Backend profile 초기화와 Data
첫 실행을 하면 loopback token이 자동 생성된다. 이 값은 복사·공유하지 않는다.

정확한 네 터미널 실행 순서와 Browser 확인 방법은
[기본 fixture stack 실행](local-setup.md#start-the-complete-fixture-stack)을 따른다.

작업할 때는 영향을 받는 저장소에서 브랜치를 만들고 필요한 검사만 실행한다.

```powershell
.\harness\eng.ps1 setup --repo <frontend|backend|ai_agent|data_platform>
.\harness\eng.ps1 check quick --repo <repo>
.\harness\eng.ps1 check contract --repo <repo>
```

공개 API·schema를 바꿀 때만 `check contract`를 사용한다. provider schema의 실제
형제 저장소 bytes까지 비교해야 하면 지원되는 저장소에서 `--cross`를 추가한다.

## 실제 Functional-UAT는 선택 사항

Functional-UAT는 실제 관세 3법, 개인 Law API 권한, 로컬 PostgreSQL/Qdrant/Neo4j,
고정 모델, 개인 Claude 계정으로 대표 사용자 여정 한 번을 확인하는 절차다. 이는
배포 승인, 릴리스 인증, 자동 완료 허용, 품질 인증이 아니다. 결과는 항상
`human_review_required`여야 한다.

이 절차를 수행하도록 승인된 담당자만 자신의 PC에서 다음을 준비한다.

1. 개인별 `LAW_API_OC`를 Data profile에 넣는다.
2. 개인별 `ANTHROPIC_API_KEY` 한 줄을 `_local/secrets/.env`에 넣는다.
3. Docker Desktop을 사용해 자신의 Data·Backend local store를 생성한다.
4. 자신의 Law source, 모델 cache, manifest, UAT 결과를 Git 밖에 생성한다.

기존 Docker volume, 과거 법령 데이터, 다른 사람의 `_local`, 모델 cache는 이
절차의 입력으로 복사하거나 재사용하지 않는다. 세부 명령과 종료·reset 방법은
[실제 3법령 Functional-UAT](local-setup.md#run-the-actual-three-law-functional-uat)를
따른다.

## 빠른 판단표

| 하려는 일 | 필요한 외부 비밀 | Docker | 팀원이 받아야 할 데이터 |
| --- | --- | --- | --- |
| 화면·API·통합 기능 개발 | 없음 | 없음 | 없음 |
| 공개 API/schema 변경 검증 | 없음 | 없음 | 없음 |
| 실제 3법령·Claude 기능 확인 | `LAW_API_OC`, `ANTHROPIC_API_KEY` | 필요 | 없음 — 각자 생성 |

이 경계를 지키면 새 팀원은 당신의 개인 비밀이나 과거 실행 상태에 의존하지 않고,
동일한 프로젝트 구조와 Harness로 개발할 수 있다.
