# 팀 개발 시작 안내

이 문서는 새 팀원이 무엇을 받아야 하는지, 무엇을 받으면 안 되는지, 그리고
기본 개발과 실제 Functional-UAT를 어떻게 구분하는지를 설명한다. 실행 명령의
정본은 [local setup](local-setup.md)이다.

## 결론

일상적인 전체 서비스 개발에는 GitHub 저장소 접근 권한과 개발 도구만 필요하다.
공유 `.env`, 계약 ZIP, Docker volume, DB dump, 모델 cache, 법령 snapshot은
필요하지도 않고 전달해서도 안 된다.

실제 65법령 이력·Claude 기능 UAT를 명시적으로 수행하는 사람에게만 다음 두 외부
입력이 필요하다. 3법 UAT는 이력 프로필의 대체가 아니라 별도 호환 경로다.

| 목적 | 개인별 외부 입력 | 로컬 보관 위치 |
| --- | --- | --- |
| 65법령 기준 snapshot 수집 | 승인된 `LAW_API_OC` | `_local/profiles/functional-uat-law-history/config.env` |
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

## 통관 지식 도우미의 현재 경계

사건별 AI 도우미와 별도 통관 지식 도우미는 모두 제품 기능이다. 공유 Harness는
이 기능을 실행하거나 법령 원문을 내려받지 않고, 제품 간 공개 schema bytes만
확인한다.

- 기본 개발에서는 `dev-fixture`의 결정론 지식 fixture로 화면·API·대화 흐름을
  확인한다. Law API, Claude 키, 모델 cache, 다른 사람의 `_local`은 필요 없다.
- 실제 공식 근거 안내는 Data가 만든 로컬 스냅샷만 사용한다. Agent가 웹 검색이나
  임의 URL을 호출하지 않으며, Harness도 source 수집·검증·실행을 대신하지 않는다.
- 현재 실제 문서 심사 Functional-UAT의 기본 실행 명령과 로컬 상태는
  `functional-uat-law-history`다. 이는 고정 65법령을 `2026-08-05` 기준으로
  수집하고, 이후 변경은 사람의 명시적 refresh로만 누적하는 법령 이력 Evidence
  profile이다. 법령 날짜는 심사일에 맞춰 찾되 HSK·요건 snapshot은 계속
  `2026-08-05` 기준일을 별도 표시한다. 날짜가 다르면 `partial`이며, 현재 조문을
  과거 조문처럼 대체하지 않는다.
- `functional-uat-3law`은 기존 3법 원문·store·manifest·transcript를 보존하는
  명시적 호환 UAT다. 이름을 바꾸거나 이력 Evidence의 입력·fallback으로 쓰지
  않는다. 기본 `18083`은 law-history에 할당되므로 두 profile을 동시에 실행하면
  3법 서버는 반드시 사람이 지정한 다른 loopback 포트를 사용한다.
- `functional-uat-kr-trade`는 보존된 V1/r3 일반 안내 전용 로컬 snapshot이다.
  Data README의 `init` → 명시적 source `build` → `verify` 절차로 hash가 확인된
  자료만 사용할 수 있다.
- `functional-uat-kr-trade-v2-law-history`는 기본 Evidence + 공식 페이지 단위 V2
  일반 안내 조합이다. 기존 `functional-uat-kr-trade-v2`는 V2 + 3법 호환 조합으로
  남는다. V2는
  `build-v2` → `verify-v2`를 통과하고 정의된 `(주제, 세부범위, 한국 적용 방향)`
  묶음이 모두 covered일 때만 실제 Functional-UAT에서 사용한다. 원문·경로·해시는
  계속 `_local`에만 두며, V1/r3와 Evidence 심사를 대체하거나 migration하지 않는다.
- 일반 안내 응답의 `registry_sha256`은 사용한 source registry의 hash이고,
  각 결과의 `registry_source_id`는 해당 공식 자료 계열을 가리킨다. Agent는
  자신이 추적하는 registry 사본과 hash·자료 유형·주제·방향·공식 URI가 모두
  맞을 때만 이를 공식 근거로 사용한다. 개별 결과 ID인 `source_id`만으로는
  출처 계열을 판단하지 않는다. Harness의 `--cross`는 이 schema·registry
  파일의 byte 일치만 확인하며, 실제 응답의 법률 판단을 대신하지 않는다.

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

```powershell
# Agent의 지식 도우미 request/response를 Backend 소비자 사본과 비교
.\harness\eng.ps1 check contract --repo backend --cross

# Data의 general-guidance-query를 Agent 소비자 사본과 비교
.\harness\eng.ps1 check contract --repo ai_agent --cross
```

## 실제 Functional-UAT는 선택 사항

Functional-UAT는 실제 65법령 이력, 개인 Law API 권한, 로컬
PostgreSQL/Qdrant/Neo4j, 고정 모델, 개인 Claude 계정으로 대표 사용자 여정 한 번을
확인하는 절차다. 이는 배포 승인, 릴리스 인증, 자동 완료 허용, 품질 인증이 아니다.
결과는 항상 `human_review_required`여야 한다.

이 절차를 수행하도록 승인된 담당자만 자신의 PC에서 다음을 준비한다.

1. 개인별 `LAW_API_OC`를 Data profile에 넣는다.
2. 개인별 `ANTHROPIC_API_KEY` 한 줄을 `_local/secrets/.env`에 넣는다.
3. Docker Desktop을 사용해 자신의 Data·Backend local store를 생성한다.
4. 자신의 Law source, 모델 cache, manifest, UAT 결과를 Git 밖에 생성한다.

기존 Docker volume, 과거 법령 데이터, 다른 사람의 `_local`, 모델 cache는 이
절차의 입력으로 복사하거나 재사용하지 않는다. 세부 명령과 종료·reset 방법은
[실제 Functional-UAT](local-setup.md)를 따른다.

## 빠른 판단표

| 하려는 일 | 필요한 외부 비밀 | Docker | 팀원이 받아야 할 데이터 |
| --- | --- | --- | --- |
| 화면·API·통합 기능 개발 | 없음 | 없음 | 없음 |
| 공개 API/schema 변경 검증 | 없음 | 없음 | 없음 |
| 실제 65법령 이력·Claude 기능 확인 | `LAW_API_OC`, `ANTHROPIC_API_KEY` | 필요 | 없음 — 각자 생성 |

이 경계를 지키면 새 팀원은 당신의 개인 비밀이나 과거 실행 상태에 의존하지 않고,
동일한 프로젝트 구조와 Harness로 개발할 수 있다.
