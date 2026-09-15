# Customs-Fit 코드베이스 지도

5개 저장소를 매번 처음부터 읽지 않도록 한 곳에 정리한 레퍼런스다. 작업을 시작할 때 이 문서로 위치를
잡고, 실제 수정 전에는 해당 파일을 직접 열어 확인한다. 이 문서는 요약이므로 코드와 다르면 코드가 맞다.

- 기준 커밋(2026-09-15, snapshot r24): frontend `1fbbc21`, backend `b712ebc`, ai-agent `45d0c21`,
  data-platform `5e59e55`
- 명령의 정본은 각 저장소 README와 이 저장소의 [local setup](local-setup.md)·[runtime profiles](runtime-profiles.md)다.
- 갱신: 경계·계약·디렉터리 구조가 바뀌는 PR을 병합하면 해당 절을 고친다. 미커밋 WIP나 개인 배포 상태는 여기 쓰지 않는다.

---

## 1. 시스템 한눈에

```text
Browser ──/api/v1──> Backend :18081 ──/internal/agent/v2|v3──> Agent :18090 ──/internal/rag/v3──> Data :18083
 (Frontend :5173)     │  Postgres :15432 / MinIO :19000               │  LLM (functional-uat만)        │  PG / Qdrant / Neo4j
                      └─ Keycloak :18088 (functional-uat OIDC)        │                                └─ guidance V1 :18093 / V2 :18094
```

| 저장소 | 책임 | 언어·도구 |
|---|---|---|
| `customs-frontend` | 심사 작업대 UI. Backend API v1만 호출 | TS, React 19, Vite 8, Vitest, Node 22 + npm 10.9.8(Corepack) |
| `customs-backend` | 인증, Case·문서·업로드·Job, 사람 판단(HITL), 보정·재심사, 운영 지표, HS/원산지/관세 진단 | Python 3.11, FastAPI, uv |
| `customs-ai-agent` | 문서 분석 워크플로, 코드 비교, 근거 조회, 보고서, LLM 어댑터, 문서 미리보기/OCR | Python 3.11, FastAPI, uv |
| `customs-data-platform` | 법령·HSK·CCCT·공식 안내 수집/적재, Evidence V3 API, 하이브리드 검색 | Python 3.11, FastAPI, uv, PG/Qdrant/Neo4j |
| `customs-engineering-harness` | 워크스페이스 검증(doctor/snapshot/setup/check), 계약 바이트 비교, Validation Report | Python 3.11 표준 라이브러리 |

- 호출 방향은 한 줄이다: Frontend → Backend → Agent → Data. **Backend는 Data를 직접 호출하지 않는다.** 법령 검색·카탈로그도
  Agent가 중계한다. 저장소끼리 코드를 import하지 않고, 서비스는 서로의 DB에 붙지 않는다.
- 기동 순서: Data → Agent → Backend → Frontend.
- 모든 내부 주소는 loopback이다. 각 서비스의 settings가 loopback이 아닌 URL을 거부한다.

### 모드와 프로필

서비스 사이의 모드 값은 두 개뿐이고, 모든 내부 요청은 `X-Customs-Service-Mode` 헤더가 양쪽에서 정확히 같아야 한다.

| 모드 | 용도 | Backend | Agent | Data 프로필 |
|---|---|---|---|---|
| `local-development` | 팀 기본 개발. 비밀·Docker 불필요 | 메모리 저장소, `POST /api/v1/dev/session` | 결정론 분석기 | `dev-fixture`(합성 Evidence) |
| `functional-uat` | 실제 법령·LLM으로 한 여정 확인 | Postgres(jsonb 1행)+MinIO+Keycloak | LLM 분석기 | `functional-uat-law-history`(65법령, 기준일 2026-08-05) |

Data 프로필 전체: `dev-fixture`, `dev-store`, `functional-uat-law-history`(기본), `functional-uat-3law`(legacy, 기본 포트가
18083으로 겹침), `functional-uat-kr-trade`(guidance 전용 V1 18093 / V2 18094). Agent의 기본 조합 프로필은
`functional-uat-kr-trade-v2-law-history`(Evidence 18083 + V2 guidance 18094)다.

### 절대 불변식 (모든 저장소 공통)

- **결과는 항상 사람 검토 대상이다.** Agent는 `requires_human_review: true`를 고정하고, Backend는
  `review_disposition=human_review_required`, `assurance_level=diagnostic`, `completion_eligibility.eligible=False`를
  강제한다. 자동 승인·자동 종료 경로는 UI에도 없다.
- **fail-closed**: 누락·모호·장애는 `partial`/`failed`나 명시적 오류 코드로 돌려준다. 빈 결과나 "요건 없음"으로 바꾸지 않는다.
  오류 응답에는 입력값, 예외 문구, 비밀을 넣지 않는다.
- 수량·금액·원산지·식별자 비교는 LLM이 아니라 코드(Agent `service/workflow.py`)가 한다.
- OCR·추출 결과는 초안이다. 사람이 확인한 텍스트만 `confirmed-document`로 문서가 된다.
- 비밀, 법령 원문, 모델, 볼륨, transcript, `_local/`, `_artifacts/`는 Git·PR·채팅·공유 ZIP에 넣지 않는다.
  모든 저장소 CI가 gitleaks 8.30.1로 검사한다.
- 서버 배포·push·재시작은 매번 사용자에게 명시적 허락을 받는다.

---

## 2. 저장소 간 계약

계약은 **provider 저장소가 원본을 소유하고 consumer가 바이트 동일 사본을 추적**하는 방식이다. harness
`check contract --repo <consumer> --cross`가 SHA-256으로 비교한다(`config/workspace.toml`의 `cross_contract`).

| Provider → Consumer | 원본 | 사본 |
|---|---|---|
| Backend → Frontend | `customs-backend/docs/openapi-v1.json`(OpenAPI 3.1.0, 53 operations) | `customs-frontend/contracts/backend-openapi-v1.json` |
| Agent → Backend | `customs-ai-agent/src/customs_ai_agent/contracts/schemas/backend/v3/*.schema.json`(12개) | `customs-backend/src/customs_backend/contracts/agent/v3/`(14개. 2개는 Backend 자체 preview-status schema) |
| Data → Agent | `customs-data-platform/contracts/evidence/v3/`: `rag_retrieval_v3`, `readiness_v3`, `guidance_query_v3` schema + registry manifest v1/v2 | `customs-ai-agent/src/customs_ai_agent/contracts/evidence/v3/` |
| Data → Backend, Frontend | `guidance_registry_v1/v2.manifest.json` | 각 저장소 `contracts/evidence/v3/` |

- Data의 law-search·law-catalog 요청/응답은 계약 파일에 없다(drift 검사 대상 아님).
- 이름 주의: Backend→Agent 심사 경로는 `/internal/agent/v2/reviews`인데 payload는 "V3(.3)" schema다.
- 결과 3.3(2026-09-14): `Engine.kind`는 실제 제공자 `deterministic|anthropic|openai`(`claude`는 3.3 이전 저장 결과 읽기용)이고
  `engine.orchestrator{framework:"langgraph",version}`가 붙는다. 추가 필드는 전부 additive다: `workflow.graph_trace`,
  `confidence`(전체·임계값·review_track), `eligibility_verdict`(advisory 고정 true), `findings[].confidence/classification`,
  `document_analyses[].extraction_source`, 요청의 `documents[].extraction{source,layout_summary}`. 새 중첩 필드는 JSON 계약에서
  required가 아니다(3.2 픽스처 호환). null인 `confidence`/`eligibility_verdict`는 `temporal_*`처럼 result_sha256에서 생략한다.
- Evidence V3 `schema_version`은 `"3.0"` 고정, 필드는 additive로만 추가한다(`temporal_context`, `temporal_coverage`,
  `scope_hint`, `expected_snapshot_id` 등).
- 일반 안내 응답: Data가 `registry_sha256`과 결과별 `registry_source_id`를 주고, Agent는 자기 registry 사본의 hash·출처
  종류·주제·방향·공식 URI가 모두 맞을 때만 근거로 쓴다. `source_id`는 결과 레코드 ID일 뿐 출처 계열 키가 아니다.

---

## 3. customs-frontend

### 구조
```
index.html              유일한 엔트리. CSP meta의 __CUSTOMS_DEV_STYLE_SOURCE__/__CUSTOMS_AUTH_CONNECT_SOURCE__를 빌드 때 치환
vite.config.ts          모드 결정(__CUSTOMS_RUNTIME_MODE__ define), CSP 플러그인, /api/v1 proxy(BACKEND_ORIGIN, 기본 127.0.0.1:18081)
src/main.ts             부트스트랩: /admin→#/admin 정규화, 로그인 화면, dev 세션 또는 OIDC, workbench 동적 import
src/workbench.tsx       WorkbenchStore 생성 + React root (StrictMode 없음)
src/auth.ts             LocalDevelopmentSession(메모리 토큰, reviewer/approver 전환)
src/functional-oidc.ts  OidcSession(oidc-client-ts, Auth Code+PKCE, 토큰 메모리, state만 sessionStorage)
src/login.ts            로그인 화면(React 아님, DOM 직접)
src/api/v1/client.ts    ApiClient: 모든 Backend 호출, 입력 검증, assertIdentity/assertOrg, 타임아웃, Idempotency-Key
src/api/v1/decoders.ts  응답 런타임 디코더(decodeXxx) + ContractError
src/api/v1/types.ts     API 타입(수작업. OpenAPI 생성 코드 아님)
src/api/user-facing-errors.ts  상태/오류 코드 → 한국어 안내문
src/ui/constants.ts     StageView/SitePage, STAGES, SITE_PAGES, HIDDEN_PAGES, parseRoute
src/ui/store.ts         WorkbenchStore: 전체 상태·라우팅·모든 액션(파일 하나, 약 1,800줄). useSyncExternalStore
src/ui/labels.ts        한국어 표시 매핑: display(), RULE_TITLES, FIELD_LABELS, DECISION_REASON_OPTIONS, 조사 교정
src/ui/App.tsx          셸, PageScreen 라우터, CaseScreen(4단계 워크스페이스), 단축키
src/ui/Chrome.tsx       Topbar, GlobalNav, CaseSubNav, Hero(PAGE_HERO)
src/ui/Dashboard.tsx    심사 업무함(상태 그룹 표, 필터, Case 생성·삭제)
src/ui/pages.tsx        Home, Laws, HskReference, Records, Metrics, Guide
src/ui/stages.tsx       1단계 문서(업로드·추출·OCR 확정) / 2단계 교차검증 / 4단계 관세
src/ui/Review.tsx       3단계 통관 심사: FindingList/FindingCard, DecisionPanel, CorrectionProposalPanel, CaseClosurePanel AdvisoryPanel은 약식/상세 트랙 설명·권고별 다음 조치·사유의 확인 사항/법령 근거 링크·분석 엔진을 표시(2026-09-15)
src/ui/trade.tsx        HsClassificationCard(심사 전엔 Case 단위 서류 텍스트 후보 + "서류에서 HSK 후보 추출" 버튼, 심사 뒤엔 run 후보; HsPickerCard 공용. 확정은 사유 입력 없이 한 번에,
                        사유는 선택 방식에서 자동 생성), OriginVerificationCard, DutyDiagnosisStage(세율 사다리)
src/ui/History.tsx      실행 타임라인, 정정 제안 승인/반려, 재심사
src/ui/Assistant.tsx    Case AI 도우미
src/ui/StateFlow.tsx    상태 전이 SVG + 담당자 업무 7단계(2 서류 등록 → 3 HSK 확정 → 4 AI 심사)
src/ui/ReportPrint.tsx  보고서 PDF 저장: 인쇄 전용 보고서 사본을 body 끝에 portal로 붙이고 window.print() (PDF는 브라우저 인쇄 대상)
src/ui/nextSteps.ts     "이어서 할 일" 행 계산(nextStepRows)·comparisonFor. Review.tsx와 ReportPrint.tsx가 공유
src/ui/Admin.tsx        숨김 #/admin LLM 설정
src/*.css               styles.css → components → krds → motion → stages → review → print (import 순서 = 캐스케이드)
                        print.css는 @media print에서 .report-print만 남기고 나머지 body 자식을 숨긴다
scripts/                check-static-boundaries.mjs(=lint), check-contract.mjs, sync-backend-openapi.mjs, doctor.mjs
dev/preview.html+ts     스텁 api로 실제 workbench를 띄우는 미리보기(빌드 제외)
deploy/uat/             정적 nginx smoke 이미지(build:functional-uat 사용)
tests/*.test.ts         Vitest+jsdom 7개(workbench, review, stages, admin, api-client, auth, official-guidance-url)
```

### 실행과 모드
- `corepack npm run dev`(=`dev:local`, local-development) / `dev:functional-uat`
- `build` = local-development, `build:functional-uat` = functional-uat. `VITE_*` 환경변수와 `.env` 파일은 쓰지 않는다.
- Node 쪽 환경변수: `BACKEND_ORIGIN`(proxy 대상), `CUSTOMS_PUBLIC_TUNNEL_HOST`(설정 시 해당 호스트 허용 + `/realms`·`/resources`를
  Keycloak 18088로 proxy하는 터널 옵트인), `BACKEND_OPENAPI_FILE`(계약 스크립트).
- 모드 차이: 세션 방식(dev 세션 또는 OIDC), CSP `connect-src`에 Keycloak `http://127.0.0.1:18088` 포함 여부.
- **함정**: `npm run verify`와 `npm run build`는 모두 `dist/`를 local-development로 덮어쓴다. 배포용 dist는 반드시
  `npm run build:functional-uat`로 다시 만들고 `dist/index.html`에 `18088`이 있는지 확인한다. 그렇지 않으면 브라우저가
  `/api/v1/dev/session`(404)을 불러 "작업을 시작하지 못했습니다"가 뜬다.
- 미리보기(백엔드 없이): `npx vite --port 5199 --mode local-development` → `http://127.0.0.1:5199/dev/preview.html#/cases`.
  `dev/preview.ts`의 스텁은 `as never`로 넘기므로 ApiClient 메서드를 추가해도 타입 오류가 안 난다. 스텁은 손으로 추가한다.

### 라우트
`#/home`, `#/cases`, `#/cases/:id/{documents|crosscheck|review|duty|assistant|history}`, `#/reference/hsk`,
`#/reference/laws`, `#/records`, `#/metrics`, `#/guide`, 숨김 `#/admin`(HIDDEN_PAGES, 링크 없음, 변경은 approver만).
`analysis`/`evidence`는 `review`의 옛 별칭. 알 수 없는 해시는 `#/home`.

### 핵심 동작
- 심사 시작 `POST /cases/{id}/reviews`(Idempotency-Key) → Job 1초 폴링 + `review-progress` 2초 폴링. 완료돼도 화면을 강제로
  3단계로 옮기지 않는다.
- 판단 폼: 상태 `confirmed|needs_information|resolved`(기본 선택 없음), 사유 프리셋 7종, `other`만 자유 텍스트 필수.
- 업무함 그룹 순서 `in_review → open → closed`. 화면 라벨은 검토 중 / 진행 전 / 종료.
- 타임아웃: 기본 30초, AI 워크플로(reviews, revisions, assistant messages, llm-settings/test)는 600초.
- 계약에 있지만 호출하지 않는 것: `/knowledge/conversations*`, `/document-assets/{id}/raster-extractions*`.

### 검증·규약
- `npm run verify` = `check:quick`(lint → typecheck → test) → `check:contract` → `build`. CI(`repository-guard.yml`)는 build를 하지 않는다.
- Browser E2E는 없다. React 전환 때 Playwright와 `functional-uat:browser` 스크립트를 제거했으므로 Backend의
  `tools/run_functional_uat_browser_e2e.py`는 현재 실행할 대상이 없다. `doctor.mjs`와 lint 모두 `@playwright/test`를 retired로 본다.
- lint는 `scripts/check-static-boundaries.mjs`다. 금지 항목은 `innerHTML=`, `dangerouslySetInnerHTML`, `localStorage`,
  `/internal/*` 문자열, 직접 `fetch("http…")`, `/api/v2`, `react-router`, `@vitejs/plugin-react`, `@playwright/test`다.
- 계약 동기화: `BACKEND_OPENAPI_FILE=../customs-backend/docs/openapi-v1.json corepack npm run contract:generate`.
  `.gitattributes`의 `-text`로 바이트를 보존한다.
- store 규칙: 비동기 액션은 `withBusy`로 감싼다(busy 중 호출은 조용히 무시된다). 실행 단위 상태를 추가하면
  `RUN_ATTACHMENT_RESET`과 `INITIAL_STATE`에 모두 넣는다. Case나 실행이 바뀌면 `invalidateCaseAggregates()`를 호출한다.
- 역할 가드는 store `hasRole`과 UI(`RoleBoundaryNote`/disabled) 양쪽에 둔다. 내부 ID와 UUID는 화면 텍스트로 노출하지 않는다.
- `exactOptionalPropertyTypes` 때문에 선택 속성은 `...(x ? { k: x } : {})` 패턴으로 넣는다.

### 변경 색인
| 작업 | 파일 |
|---|---|
| 전역 페이지 추가 | `constants.ts`(SitePage, SITE_PAGES), `App.tsx` PageScreen, `Chrome.tsx` PAGE_HERO(누락 시 타입 오류), `pages.tsx`, 로딩은 `store.followRoute` |
| 숨김 페이지 | 위와 같되 `HIDDEN_PAGES`. 평문 경로는 `main.ts` normalizeTypedPath |
| Case 단계 추가 | `constants.ts`(STAGES, resolveView), `App.tsx`, `Chrome.tsx` CaseSubNav, `store.stageComplete`, `followRoute` |
| 규칙/필드/상태 라벨 | `labels.ts`(RULE_TITLES, FIELD_LABELS, display, workflowLabel) |
| 판단 사유 프리셋 | `labels.ts` DECISION_REASON_OPTIONS + `types.ts` FindingDecisionReasonCode (Backend enum과 함께) |
| 확인 사항 카드 | `Review.tsx`(FindingCard, Collapsible), `review.css`, `tests/review.test.ts` |
| 보고서 PDF 출력 내용·서식 | `ReportPrint.tsx`(ReportPrintView), `print.css`, `tests/review.test.ts` "보고서 PDF 저장". PDF 라이브러리 없음(CSP) |
| 새 API 호출 | `types.ts` → `decoders.ts` → `client.ts` → `store.ts` → `dev/preview.ts` 스텁 → `tests/api-client.test.ts` |
| 오류 문구 | `src/api/user-facing-errors.ts` |
| 업무함 그룹·필터 | `Dashboard.tsx`(GROUP_ORDER, STAT_CARDS), `constants.ts` CASE_FILTERS |
| LLM 관리자 화면 | `Admin.tsx`(MODEL_SUGGESTIONS), `client.ts` validateLlmSettingsUpdate, `tests/admin.test.ts` |
| CSP·proxy | `index.html`, `vite.config.ts`, `deploy/uat/default.conf.template` |

---

## 4. customs-backend

### 구조 (`src/customs_backend/`)
```
main.py                 uvicorn. host 127.0.0.1, port 18081 하드코딩
settings.py             Settings(env_prefix CUSTOMS_BACKEND_, env_file=None), 모드별 loopback 검증
api.py                  create_app, Container, 모든 라우트, ProblemDetails 오류 매핑, body 크기 미들웨어
service.py              WorkbenchService: 비즈니스 규칙 전부(역할 재검사, closed 가드, 심사 실행·검증, 대화, 보정, 지표), RasterExtractionWorker
domain.py               Pydantic 모델·Enum(CaseStatus, JobStatus, FindingDecisionReasonCode…), 업로드 한도
ports.py                Protocol(WorkbenchRepository, ObjectStore, AgentClient, TokenVerifier) + 예외
repository.py           InMemoryWorkbenchRepository(RLock, org 스코프, idempotency)
functional_repository.py PostgresWorkbenchRepository: 전체 상태를 functional_uat.backend_state 1행 jsonb로 저장·복원
adapters.py             HttpAgentClient(유일한 Agent 전송), CallableAgentClient(테스트용), InMemory/MinioObjectStore
auth.py                 OidcJwtVerifier(JWKS, RS256/ES256), StaticTokenVerifier, require_roles
agent_contract.py       패키지된 Agent V3 schema 검증
guidance_registry.py    Agent 답변 인용이 등록된 공식 출처인지 검증
trade_features.py       HS 후보(run 사실 기준 build_hs_classification, 서류 텍스트 기준 build_case_hs_classification), HSK 검색, 원산지 검증(FTA hint), 관세 진단
duty_reference.py       SQLite 전체 관세율표, _FTA_AGREEMENT_PAGES(협정별 fta.go.kr URL)
law_reference.py        법령 검색/카탈로그(Agent 경유)
operations_metrics.py   운영 지표(review_load 포함)
admin_settings.py       approver 전용 LLM 설정 프록시 모델
knowledge_ingress.py / case_input_boundary.py  대화 입력 경계
local_profile.py        _local/profiles/<profile>/backend.env + Keycloak realm JSON 생성
scenario_transcript.py  _local/state/scenario-runs 기록기(redaction)
reference/              hsk_names_20260101.json, duty_rates_20260101.json
docs/openapi-v1.json    OpenAPI 스냅샷(생성물, 커밋 대상)
deploy/functional-uat/compose.yaml  Postgres, MinIO, Keycloak(+테마). Backend 자체 Dockerfile은 없음
tools/                  check_backend.py, export_product_openapi.py, verify_contract_snapshots.py, run_functional_uat_matrix.py,
                        run_functional_uat_browser_e2e.py, build_duty_reference.py, Import-LocalProfile.ps1, Copy-FunctionalUatPassword.ps1
tests/product/          약 20개 파일, 약 270개 테스트. 공용 fixture는 agent_v3_fixture.py
```

### 실행
```powershell
uv sync --locked --group dev --extra unit [--extra functional-uat] --python 3.11
uv run --no-sync customs-backend-profile-init --profile dev-fixture|functional-uat
. .\tools\Import-LocalProfile.ps1 -Profile <profile> -Scope backend     # 반드시 dot-source
docker compose --project-name customs-functional-uat --env-file ..\_local\profiles\functional-uat\backend.env -f deploy\functional-uat\compose.yaml up -d --wait   # UAT만
uv run --no-sync customs-backend-api
```
- UAT 계정은 `local-functional-reviewer`, `local-functional-approver`(org `local-functional`)이고, 비밀번호는
  `tools/Copy-FunctionalUatPassword.ps1`로 클립보드에 받는다.
- review_as_of를 생략하면 2026-08-05를 쓴다. functional-uat의 `FUNCTIONAL_UAT_AS_OF`는 2026-08-05 외의 값을 거부한다.
- `Import-LocalProfile.ps1`은 allowlist 밖의 키가 있으면 throw한다. 새 env를 추가할 때 allowlist도 같이 고친다.

### 도메인
- 역할은 `reviewer`(writers)와 `approver`다. 읽기는 둘 다 가능하다. 라우트 dependency와 `service._require_role`에서 **두 번** 검사하고,
  모든 조회는 `org_id`로 스코프한다.
- **CaseStatus는 `open` → `in_review` → `closed` 세 개뿐이다.**
  - `open → in_review`: `repository.create_review_job`에서 전환
  - `→ closed`: `POST /cases/{id}/closure`에서 전환. 조건은 disposition `cleared|returned`, reason 필수, 최신 Run이 있고
    모든 finding에 판단이 있을 것(없으면 409)
  - closed Case에서는 문서 추가, 업로드, 추출, 심사, revision이 모두 409다. 자동 종료는 없다.
- 심사 `POST /cases/{id}/reviews`는 202를 반환하지만 **요청 안에서 동기로** Agent를 호출한다. `_build_run`이 Agent 결과를
  다음 기준으로 재검증하고, 하나라도 어긋나면 409다: schema, `result_sha256` 재계산, case/run/as_of/manifest 바인딩,
  human_review 필수, ID 유일성, `temporal_context.law_as_of == review_as_of`. Run은 저장 후 불변이다(판단과 hs_selection은 옆에 붙는다).
- Finding 판단 `PUT /review-runs/{rid}/findings/{fid}/decision`
  - 상태: `confirmed|needs_information|resolved`
  - 사유 코드 `FindingDecisionReasonCode` 7종: `document_verified, notation_difference, supplement_requested,
    correction_proposed, requirement_satisfied, requirement_not_applicable, other`. 라벨은 `FINDING_DECISION_REASON_LABELS`.
  - `other`나 코드가 없으면 reason 필수. 프리셋이면 라벨을 reason으로 저장한다. 이력은 최대 100개.
- 보정 제안 → 재심사
  1. reviewer가 제안한다.
  2. approver가 승인한다. **작성자 본인은 승인할 수 없다.**
  3. approver가 `POST /review-runs/{rid}/revisions`로 child Run을 만든다. `human_corrections`가 Agent로 전달된다.
  4. child가 성공해야 제안이 `applied`가 된다.
- 원산지/관세(`trade_features.py`)는 저장된 Run에서 파생한다. HS 후보는 두 경로다: 심사 전에는 `cases/{id}/hs-classification`이 확정 서류 텍스트의
  세번(HS) 표기 줄에서 10자리 코드를(없으면 품명 매칭) 후보로 내고 확정을 `CaseRecord.hs_selection`에 기록한다(2026-09-15). 품명은 라벨 줄의 값과
  표 머리글("Description of Goods") 아래 줄들(수량·단위 줄에서 정지)에서 읽는다(`_description_values`, 텍스트 레이어가 표 셀을 한 줄씩 내놓기 때문). 심사 뒤에는 run 사실 기준
  `review-runs/{rid}/hs-classification`. 심사 시작 시 Case·run 확정 중 `selected_at`이 최신인 코드가 `reviewer_hs_selection`으로 Agent에 들어가므로
  첫 심사부터 법령 근거를 조회한다. Case 확정은 Case revision을 바꾸지 않는다(대기 중 심사의 멱등 식별 유지). 관세는 `CUSTOMS_BACKEND_DUTY_REFERENCE_DB`(SQLite)가 있으면 전체 관세율표를, 없으면
  JSON 스냅샷을 쓴다. FTA는 협정국 원산지, C/O 존재, C/O 원산지 일치를 모두 만족해야 적용한다.
- 영속: functional-uat는 전체 aggregate를 jsonb 1행에 저장하고, 마이그레이션 도구는 없다. **새 필드는 반드시 기본값을 가져야**
  과거 상태를 복원할 수 있다.

### API (`/api/v1`, R=reviewer A=approver)
| 그룹 | 엔드포인트 |
|---|---|
| auth | GET `auth/config`(UAT만), POST `dev/session`(local-dev+loopback만), GET `session` |
| cases | POST `cases`(R), GET `cases`, GET/DELETE `cases/{id}`(DELETE는 R), POST `cases/{id}/closure`(R), GET `cases/{id}/review-progress` |
| documents | POST(R)/GET `cases/{id}/documents`, GET `…/documents/{doc}/content` |
| assets | POST(R)/GET `cases/{id}/document-assets`; `…/{asset}/extractions`, `raster-extractions`, `raster-text-extractions`(POST는 R, GET `/{job}`); GET `…/raster-extractions/{id}/pages/{n}`, `…/source`; POST `…/confirmed-document`(R) |
| reviews | POST `cases/{id}/reviews`(R, Idempotency-Key), GET `jobs/{id}`, `cases/{id}/review-runs`, `review-runs/{rid}` |
| trade | GET/POST(R) `cases/{cid}/hs-classification`(심사 전 Case 단위), GET/POST(R) `review-runs/{rid}/hs-classification`, GET `…/origin-verification`, `…/duty-assessment`, GET `hsk-reference?q=`, `hsk-reference/{code}` |
| decisions | PUT `review-runs/{rid}/findings/{fid}/decision`(R) |
| assistant | POST/GET `review-runs/{rid}/assistant/conversations`, POST(Idempotency-Key)/GET `assistant/conversations/{cid}/messages` |
| knowledge | POST/GET `knowledge/conversations`, POST/GET `knowledge/conversations/{cid}/messages` |
| corrections | POST(R)/GET `review-runs/{rid}/correction-proposals`, PUT `correction-proposals/{id}/decision`(A), POST `review-runs/{rid}/revisions`(A, Idempotency-Key) |
| reference·metrics | GET `law-search`, `laws`, `operations-metrics` |
| admin | GET/PUT/DELETE `admin/llm-settings`, POST `admin/llm-settings/test`(모두 A) |
| health | `/health/live`, `/health/ready`, `/health/functional-uat-ready` |

오류는 `application/problem+json`으로 돌려준다.

| 예외 | HTTP | code |
|---|---|---|
| NotFound | 404 | `not_found` |
| Conflict | 409 | `stale_or_conflicting_state` |
| Authorization | 403 | `permission_denied` |
| ValueError | 422 | `invalid_operation` |
| 요청 검증 실패 | 422 | `request_validation_failed` |
| Agent LLM 설정 오류 | Agent가 준 값 | Agent가 준 값 |

본문 한도는 4MiB, asset 업로드는 16MiB다.

### 주요 환경변수 (`CUSTOMS_BACKEND_*`)
| 변수 | 설명 |
|---|---|
| `ENVIRONMENT` | `local-development`(기본) 또는 `functional-uat` |
| `AGENT_BASE_URL` | 기본 `:18090` |
| `AGENT_SERVICE_TOKEN` | 필수 |
| `AGENT_TIMEOUT_SECONDS` | 기본 180 |
| `DEV_REVIEWER_SESSION_TOKEN`, `DEV_APPROVER_SESSION_TOKEN` | local-dev 필수, 두 값이 서로 달라야 함 |
| `DATABASE_URL` | UAT 필수, `:15432` |
| `MINIO_*` | MinIO 접속 설정 |
| `OIDC_ISSUER`, `OIDC_AUDIENCE`, `OIDC_JWKS_URL`, `OIDC_CLIENT_ID`, `OIDC_AUTHORIZATION_ENDPOINT`, `OIDC_TOKEN_ENDPOINT` | realm `customs`, aud `customs-workbench-api`, client `customs-workbench-spa` |
| `CUSTOMS_BACKEND_DUTY_REFERENCE_DB` | Settings가 아니라 `os.environ`에서 직접 읽는다 |
| `PUBLIC_OIDC_ORIGIN` | 선택. 터널·리버스 프록시 뒤 UAT에서 issuer와 브라우저용 auth/token/end_session을 이 bare HTTPS origin 아래에 둘 수 있게 한다. JWKS는 loopback 유지. Frontend `CUSTOMS_PUBLIC_TUNNEL_HOST`와 짝 |

### 검증
- `python tools/check_backend.py quick` = ruff → mypy → pytest
- `python tools/check_backend.py contract` = `verify_contract_snapshots.py` + contract/agent-boundary/local-profile 테스트
- **contract gate는 스냅샷 경로가 dirty나 untracked이면 실패한다.** 즉 OpenAPI를 재생성한 뒤 커밋해야 통과한다.
  재생성 명령은 `uv run --no-sync python tools/export_product_openapi.py`다.
- **`tests/product/test_functional_uat_matrix_identity.py`는 matrix 도구의 검증 함수와 Agent 보고서 문구 형식에 의존한다.**
  예를 들어 claims는 한국어여야 하고 원본 파일명이 없어야 한다. Agent 보고서 텍스트를 바꾸면 이 테스트가 깨진다.
- CI(`unit.yml`)는 quick → contract → `git diff --check` → 작업트리 clean → gitleaks 순서다.

### 변경 색인
| 작업 | 파일 |
|---|---|
| 엔드포인트 추가 | `api.py`(라우트+view, writers/readers/approvers) → `service.py`(+`_require_role`) → `domain.py` → 필요시 `ports.py`/`repository.py`/`functional_repository.py` → `tests/product/test_workbench_api.py` → OpenAPI 재생성·커밋 → frontend `contract:generate` |
| 사유 코드 추가 | `domain.py` enum + LABELS → OpenAPI 재생성 → frontend `labels.ts`·`types.ts` |
| 상태·종료 규칙 | `service.py`(close_case, closed 가드), `repository.py`(create_review_job, close_case), `tests/product/test_case_closure.py` |
| Agent 계약 변경 수용 | `contracts/agent/v3/*.schema.json`(Agent와 같은 바이트), `agent_contract.py`, `verify_contract_snapshots.py` AGENT_SCHEMA_PATHS, `domain.py` Agent* 모델, `service._build_run`, `agent_v3_fixture.py` |
| 새 Agent 호출 | `ports.AgentClient`, `adapters.HttpAgentClient`와 `CallableAgentClient` **둘 다**, `tests/product/test_agent_http_boundary.py` |
| 저장 필드 추가 | `domain.py`(기본값 필수), `repository.py`, `functional_repository.py` restore/serialize |
| 환경변수 추가 | `settings.py`, `local_profile.py`, `tools/Import-LocalProfile.ps1` allowlist, `tests/product/test_local_profiles.py` |
| 관세·FTA | `duty_reference.py`, `trade_features.py`(build_duty_assessment, _fta_hints), `reference/`, `tools/build_duty_reference.py` |
| HS·원산지 | `trade_features.py`(build_case_hs_classification, build_hs_classification, build_origin_verification) |
| 지표 | `operations_metrics.py`, `tests/product/test_operations_metrics.py` |
| LLM 관리자 | `admin_settings.py`, `service.py`, `adapters.py` `_LLM_SETTINGS_PATH`, `tests/product/test_admin_llm_settings.py` |
| Keycloak realm·테마 | `local_profile.py`, `functional_uat_branding.py`, `deploy/functional-uat/keycloak-themes/` |

---

## 5. customs-ai-agent

### 구조 (`src/customs_ai_agent/`)
```
service/app.py            FastAPI create_app: 모든 라우트, bearer 인증, 모드 헤더 미들웨어, preview engine 주입
service/__main__.py       uvicorn 기동(_serve(mode))
service/settings.py       ServiceSettings.from_environment(): 모든 env 파싱·범위 검증
service/profiles.py       ProfileRuntime(심사·도우미·지식·law 프록시), ProfileEvidenceRetriever(Data 클라이언트), 결정론 분석기, _engine(),
                          _evidence_stage(그래프의 evidence 노드 본체: Data 1회 호출 또는 명시적 skip)
service/graph.py          LangGraph StateGraph: extraction_agent → cross_validation_agent →(감사 루프 1회)→ evidence_retrieval
                          → expert_review_agent → report_generation. ReviewGraphConfig/run_review_graph/derive_statuses
service/workflow.py       결정론 핵심(추출 렌더링·규칙 비교·finding·stage)과 run_bounded_workflow(그래프 래퍼, evidence 없이 종료)
service/confidence.py     결정론 신뢰도(추출·비교·근거 성분 0.4/0.4/0.2)와 finding별 신뢰도, review_track
service/expert_review.py  권고 적격 판정 규칙(rule_verdict)과 LLM 의견 병합(merge_llm_verdict, 더 엄격한 방향만)
service/controller.py     동시성 Semaphore, review_run_id 기반 idempotent replay
service/assistant.py      Case 도우미·통관 지식 답변, guidance V1/V2
service/guidance_registry.py  registry manifest fail-closed 소비
service/report.py         결정론 8구역 보고서
service/llm_settings.py   LLM_SETTINGS 전역 스토어(env 기준값 + override 파일 + 구독자)
service/metadata.py       /internal/agent/v2/metadata(engine, 한도, schema sha256)
service/progress.py       심사 진행 상황(메모리)
service/document_extraction.py / document_preview_runtime.py   v2 미리보기 엔진(기본 Disabled, 승인 pointer 방식)
service/raster_document_* / raster_text_* / ppstructurev3_sidecar.py   v3 raster OCR(해시 고정 sidecar 번들)
adapters/llm.py           ClaudeSettings(provider 중립), build_provider_client, OpenAIMessagesClient, ClaudeDocumentAnalyzer, ClaudeGroundedResponder
adapters/retrieval.py     Data URL loopback 검증
schemas/v3.py             심사 wire 모델(ReviewRunResultV3, schema 3.3, EngineV3+OrchestratorV3, GraphTraceV3, ConfidenceV3, EligibilityVerdictV3)
contracts/schemas/backend/v3/   Agent HTTP 계약 원본 12개
contracts/evidence/v3/    Data 계약 사본
contracts/registry.py     패키지된 schema 로더·sha256
prompts/*.txt             functional-uat 고정 prompt 7개(finding_classification_v1, expert_review_v1 포함)
scenario_transcript.py    _local/state/scenario-runs 기록(agent-data.jsonl, claude.jsonl)
evaluation/               OCR Gold·합성 평가(서비스 경로 아님)
tools/Import-AgentProfile.ps1   profile env를 현재 PS 세션에 주입(dot-source 필수)
deploy/ocr-oci/           raster OCR OCI 이미지(GHCR, digest 고정)
```

### 실행
```powershell
uv sync --locked --group dev [--extra functional-uat]
. .\tools\Import-AgentProfile.ps1 -Profile dev-fixture
uv run --locked --no-sync --group dev customs-ai-agent-dev-serve
. .\tools\Import-AgentProfile.ps1 -Profile functional-uat-law-history      # 또는 functional-uat-kr-trade-v2-law-history -EvidencePort 18083 -GuidancePort 18094
uv run --locked --no-sync --group dev --extra functional-uat customs-ai-agent-functional-uat-serve
```
- importer profile은 6종이다: dev-fixture, 3law, law-history, kr-trade, kr-trade-v2, kr-trade-v2-law-history.
  내부적으로 Backend의 `Import-LocalProfile.ps1 -Scope agent`를 호출하고 `CUSTOMS_AGENT_PORT=18090`으로 고정한다.
- functional-uat importer는 `../_local/secrets/.env`에서 **`ANTHROPIC_API_KEY`만** 읽는다. OpenAI로 로컬 기동하려면
  `OPENAI_API_KEY`와 `CUSTOMS_AGENT_LLM_PROVIDER=openai`를 세션에 직접 설정한다.
- 일반 PDF/JPG/PNG 추출을 쓰려면 `CUSTOMS_AGENT_DOCUMENT_PREVIEW_DEMO=claude-vlm`로 띄운다. 이 모드는 pypdf 텍스트 레이어를
  먼저 쓰고 실패하면 LLM VLM OCR로 넘어간다. 이 설정이 없으면 v2 미리보기는 `unavailable`이다.
- 이 PC의 시스템 `python`은 3.14이므로 항상 `uv run`이나 `.venv`를 쓴다.

### 도메인
- 심사 파이프라인 `POST /internal/agent/v2/reviews` → `ProfileRuntime.review_v3_json` → `graph.run_review_graph`
  (LangGraph StateGraph, checkpointer 없음, recursion_limit 16). 노드:
  1. `extraction_agent`: local-dev는 결정론 분석기, UAT는 LLM 분석기. 2회차는 감사 요청 필드만 `focus_fields`로 재판독(문서당 1회).
  2. `cross_validation_agent`: 렌더링·승인된 사람 수정·**코드 규칙 비교**(판정은 항상 코드)·finding. 1회차에 감사 판단:
     필수 필드 누락/인용 미확인(`_reextraction_targets`) + UAT에서만 mismatch 재판독·LLM 재판독 요청 → 있으면
     `extraction_agent`로 되돌아간다(`loop_reason`). LLM(purpose `finding_classification`)은 오류 유형 분류·설명만 덧붙인다(fail-open).
  3. `evidence_retrieval`: Data `/retrieve` 1회 일괄 호출(프로필이 주입한 `_evidence_stage`). law-history면 `law_as_of == review_as_of` 요구.
     조회 품목은 `workflow.retrieval_plan_from_analyses`가 문서 사실의 `hs_code`로 만든다. Data는 HSK10 정확 일치 또는 HS6 단일 leaf만 해소하므로
     문서의 6·8자리 코드는 `reviewer_hs_selection`이 접두어와 맞을 때 그 10자리로 완성한다(2026-09-15, agent `45d0c21`; 이전에는 문서에 코드가 하나라도 있으면
     담당자 확정을 버렸다). 서로 다른 코드는 덮어쓰지 않는다. 문서에 코드가 전혀 없으면 확정 코드가 유일한 조회 품목이 된다.
  4. `expert_review_agent`: 신뢰도·review_track(`CUSTOMS_AGENT_CONFIDENCE_THRESHOLD`, 기본 0.8), 권고 판정(mismatch high →
     ineligible, 요건·needs_review·partial·근거 없음·낮은 신뢰도 → indeterminate, 그 외 eligible). UAT면 LLM(purpose `expert_review`)이
     더 엄격한 쪽으로만 바꿀 수 있고 인용 ID가 결과에 없으면 무시된다. `requires_human_review`는 항상 true.
  5. `report_generation`: required_actions와 결정론 8구역 보고서. 기능 UAT 문장 합성(`_compose_functional_report`)은 검증 뒤 profiles에서.
- stage 8종: classify, extract, normalize, match, validate, reextract, retrieve_evidence, generate_result.
  planner는 최대 2 cycle, 6 action 그대로다(예비 비교는 planner에 기록하지 않고 `graph_trace`에만 남는다). `graph_trace`는
  iteration ≤ 2, node_runs ≤ 8.
- `ReviewRunResultV3` 주요 필드:
  - status: `needs_review|blocked`
  - execution_status: `succeeded|partial|failed`
  - assessment: `no_issue_detected|issues_detected|indeterminate`
  - comparison: `pass|skip|mismatch|needs_review`
  - finding kind: `validation_issue|regulatory_finding`, severity: low~critical
- LLM 계층(`adapters/llm.py`)
  - `CUSTOMS_AGENT_LLM_PROVIDER`로 `openai|anthropic`을 고른다. 설정이 없으면 OpenAI 키만 있을 때 openai, 그 외에는 anthropic이다.
  - `OpenAIMessagesClient`가 Anthropic `messages.create` 형태를 Chat Completions로 변환한다. 분석기, 응답기, VLM OCR이 모두 이 경로를 쓴다.
  - `gpt-5*`와 `o*` 계열은 temperature를 생략한다.
  - `*_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` 등 전송 경로를 바꾸는 env가 있으면 기동을 거부한다.
- LLM 설정 스토어(`service/llm_settings.py`): 유효 설정 = env 기준값 + override(provider/model/max_tokens/timeout).
  `CUSTOMS_AGENT_LLM_SETTINGS_PATH`가 있으면 JSON으로 저장한다. 비밀은 저장하지 않는다. 변경되면 구독자(ProfileRuntime)가
  분석기와 응답기를 교체하므로 재시작이 필요 없다. PUT/DELETE/test는 functional-uat에서만 된다(그 외 409).
- 지식 답변: V2는 `topic+scope_hint`(scope 최대 2개)와 `expected_snapshot_id`로 스냅샷을 고정한다. 커버리지가 부족하면
  `insufficient_evidence`로 끝나고 모델 지식으로 채우지 않는다.

### API (`/internal/agent/*`, Bearer 필수. 없으면 401, 틀리면 403. 모드 불일치는 403 `service_mode_mismatch`)
| 그룹 | 엔드포인트 |
|---|---|
| health | `/health/live`, `/health/ready`(Data readiness 포함) |
| 메타·설정 | GET `v2/metadata`, GET/PUT/DELETE `v3/llm-settings`, POST `v3/llm-settings/test` |
| 심사 | POST `v2/reviews`(`X-Idempotent-Replay`, 같은 run_id에 다른 hash면 409), GET `v3/reviews/{id}/progress` |
| 대화 | POST `v2/assistant/answers`, POST `v2/knowledge/answers` |
| 법령 | POST `v3/law-search`, POST `v3/law-catalog`(Data로 중계) |
| 미리보기 | POST `v2/document-extractions`, GET `v3/raster-document-preview-status`, POST `v3/raster-document-extractions`, POST `v3/raster-document-pages/{n}`, GET `v3/raster-text-preview-status`, POST `v3/raster-text-extractions` |

라우트 목록의 정본은 `tests/service/test_openapi.py`다. README의 "열두 개" 목록은 오래됐다.

### 주요 환경변수
| 변수 | 설명 |
|---|---|
| `CUSTOMS_AGENT_SERVICE_BEARER_TOKEN`, `EVIDENCE_SERVICE_BASE_URL`, `EVIDENCE_SERVICE_TOKEN` | 필수 |
| `CUSTOMS_AGENT_SERVICE_MODE`, `CUSTOMS_AGENT_EVIDENCE_PROFILE` | 모드와 Evidence 프로필 |
| `GUIDANCE_SERVICE_BASE_URL`, `GUIDANCE_SERVICE_TOKEN`, `GUIDANCE_SERVICE_REGISTRY_VERSION` | 세 값을 함께 설정 |
| `CUSTOMS_AGENT_LLM_PROVIDER` | `openai` 또는 `anthropic` |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | 제공자 키 |
| `CUSTOMS_AGENT_CLAUDE_MODEL`, `CUSTOMS_AGENT_OPENAI_MODEL` | 기본값 `claude-haiku-4-5-20251001` / `gpt-4.1-mini` |
| `CUSTOMS_AGENT_LLM_MAX_TOKENS`, `CUSTOMS_AGENT_LLM_TIMEOUT_SECONDS` | 기본 4096 / 90. 없으면 `*_CLAUDE_*` 값을 대신 읽음 |
| `CUSTOMS_AGENT_LLM_SETTINGS_PATH` | LLM 설정 override 저장 경로 |
| `CUSTOMS_AGENT_RUN_TIMEOUT_SECONDS` | 420 |
| `CUSTOMS_AGENT_CONFIDENCE_THRESHOLD` | 0.8. review_track 분기 기준(0~1) |
| `CUSTOMS_AGENT_MAX_CONCURRENCY` | 2 |
| `CUSTOMS_AGENT_EVIDENCE_RETRIEVE_TIMEOUT_SECONDS` | 90 |
| `CUSTOMS_AGENT_DOCUMENT_PREVIEW_DEMO` | 문서 미리보기 데모 모드 |
| `CUSTOMS_AGENT_*_PREVIEW_APPROVAL_PATH`, `*_SHA256`, `*_AUTO_ACTIVATION` | 미리보기 승인 pointer |

### 검증·함정
- quick: `ruff check src tests` → `ruff format --check` → `mypy src` → `pytest -q`. contract는
  `tests/test_evidence_v3_consumer.py`와 `tests/service/test_openapi.py`다.
- **이 PC에서는 pytest에 `--basetemp=C:/t/pt`가 필요하다.** 사용자 환경변수 `TMPDIR`이 ESTsoft 경로라 경로가 너무 길어지고,
  파일시스템 테스트 약 84건이 실패한다.
- `LLM_SETTINGS`는 import할 때 만들어지는 모듈 전역이다. env를 바꾸는 테스트끼리 상태를 공유하므로 주의한다.
- `contracts/schemas/backend/v3/*.json`의 바이트를 바꾸면 Backend 사본, harness cross 검사, `/metadata`의 sha가 함께 바뀐다.
- 보고서와 답변 문구를 바꾸면 Backend의 `test_functional_uat_matrix_identity.py`가 깨질 수 있다.

### 변경 색인
| 작업 | 파일 |
|---|---|
| LLM 모델 기본값·허용 패턴 | `adapters/llm.py`(DEFAULT_*_MODEL, _SAFE_*MODEL), `service/llm_settings.py` _DEFAULT_MODELS, `tests/test_llm_openai_provider.py` |
| 새 LLM provider | `adapters/llm.py`(LLMProvider, build_provider_client), `llm_settings.py` _PROVIDERS, pyproject extra + `uv lock` |
| prompt | `prompts/*.txt` |
| 비교 규칙·finding | `service/workflow.py`(_comparisons), 필요시 `schemas/v3.py` + schema, `tests/service/test_bounded_workflow.py` |
| 그래프 노드·감사 루프 | `service/graph.py`(_audit, _mismatch_reread_targets, _llm_classifications), `tests/service/test_review_graph.py` |
| 신뢰도 공식·임계값 | `service/confidence.py`, `settings.py`, `tests/service/test_confidence.py` |
| 권고 판정 규칙 | `service/expert_review.py`, `tests/service/test_expert_review.py` |
| 결정론 추출 | `service/profiles.py` _DeterministicDocumentAnalyzer |
| 보고서 | `service/report.py`, `schemas/v3.py` ReportSectionV3 |
| 새 엔드포인트 | `service/app.py`, `ProfileRuntime.*_json`, `tests/service/test_openapi.py`, 필요시 계약 schema + `contracts/registry.py` + Backend 사본 |
| Data 호출·타임아웃 | `service/profiles.py` ProfileEvidenceRetriever, `service/settings.py` |
| 새 env | `service/settings.py` |
| importer profile | `tools/Import-AgentProfile.ps1`, `settings._EVIDENCE_PROFILES` |

---

## 6. customs-data-platform

### 구조
```
contracts/evidence/v3/        계약 원본(schema 3개 + registry manifest 2개)
law_repository/docker-compose.yml   PG(pgvector)/Qdrant/Neo4j, 이미지 digest 고정, loopback 포트
law_repository/migrations/001..007_*.sql   PG DDL(적용된 파일은 수정 금지, 새 번호로 추가)
law_repository/scripts/migrate.py   advisory lock + schema_migrations 체크섬
law_repository/data/          HS부호 xlsx 2개(20260101), ccct_progress.jsonl(22,654줄, 고정 입력)
law_repository/src/
  config.py                   Settings(env_file=None)
  local_profiles.py           프로필 이름·포트·토큰·config.env, LAW_API_OC 범위 제한, CUDA opt-in
  profile_evidence.py         FastAPI 앱 팩토리(Evidence / guidance 전용 / dev-fixture), 인증·모드 헤더
  evidence.py / evidence_v3.py   V1 내부 모델·조합, V3 retrieve·law-search·law-catalog
  guidance_v3.py, guidance_registry(_v2).py(+json)   안내 질의와 registry
  conformance.py              dev-fixture 결정론 합성 백엔드
  repository.py               조회 계층(_temporal_predicate 단일 경로, 저장소별 검색, HSK 해소, 그래프 확장)
  hybrid.py                   (연구 프로토타입) RRF(k=60) + bge-reranker, 실패 시 RRF 순위로 fallback
  retrieval_five_stage.py     제품 5단 검색: 질의 재작성(사전·앵커, LLM 없음) → 가중 RRF(변형 0.6) → DELEGATES 이웃 확장(시드 10, ≤15,
                              (law_id, article_no)→기준일 조문 매핑) → CPU cross-encoder(풀 30) → z(rerank)+2.0·z(RRF), score는 min-max
  lawgo.py                    법제처 DRF 클라이언트(JO 6자리)
  ingest/{laws,hsk,ccct,graph}.py   조문·HSK·CCCT·조문 그래프 적재. `graph.build(as_of=)`는 기준일에 유효한 조문(valid_from ≤ as_of < valid_to)으로
                              :Article 노드를 만든다(law-history 원장은 `is_current=false` 고정이라 플래그로는 0건). `as_of` 없이는 3법 로더의 `is_current`
  embed/{dense,bm25,rerank,model_identity}.py   bge-m3 / Kiwi 형태소 BM25(`ko-kiwi-md5-bm25/v2`, avgdl 141, 결합형 방출) / reranker, revision 고정
  law_history.py(+_spec)      65법령 시점 이력 프로필(수집, 영수증, 락, 복구, 백엔드)
  functional_uat.py(+_spec)   3law 프로필
  functional_uat_kr_trade(_v2).py   guidance 스냅샷 빌드·검증·V2 변경 감지
  hsk_ccct_history.py         날짜별 HSK/CCCT archive import·activate
  store_consistency.py        3-store 전수 비교(유지보수용)
tools/                        프로필 CLI(run_dev_fixture_evidence, dev_store, functional_uat_law_history, functional_uat_3law,
                              functional_uat_kr_trade, hsk_ccct_archive), generate_evidence_v3_contracts, check_contract_drift, 벤치마크
law_repository/tests/         31개 파일(기본 실행은 pyproject testpaths 19개)
```
Dockerfile, 이미지 발행, 배포 워크플로는 없다.

### 실행
```powershell
uv sync --locked --group dev --python 3.11                     # 실제 저장소를 쓰려면 --extra store --extra rag --extra ingestion 추가
uv run --locked --no-sync --group dev python tools/run_dev_fixture_evidence.py --host 127.0.0.1 --port 18083
# law-history (--extra store --extra rag --extra ingestion)
python tools/functional_uat_law_history.py init            # 이후 _local/profiles/functional-uat-law-history/config.env 에 LAW_API_OC 기입
python tools/functional_uat_law_history.py compose up
python tools/functional_uat_law_history.py build --as-of 2026-08-05 [--allow-cuda]
python tools/functional_uat_law_history.py serve --host 127.0.0.1 --port 18083
python tools/functional_uat_law_history.py refresh --from D --to D | verify-store | reset --confirm-profile functional-uat-law-history
```
복구 명령은 `recover-refresh`, `recover-build`, `recover-baseline`, `rebind-frozen-inputs`, `recover-published`이고, 언제 쓰는지는
README에 있다. stale lock은 자동으로 지우지 않는다.

프로필별 store 포트(PG / Qdrant http / Neo4j bolt):

| 프로필 | PG | Qdrant http | Neo4j bolt |
|---|---|---|---|
| dev-store | 55432 | 6333 | 7687 |
| law-history | 55472 | 6363 | 7717 |
| 3law | 55452 | 6343 | 7697 |

### 도메인
- PG가 원장이고, Qdrant와 Neo4j는 다시 만들 수 있는 projection이다.
- `law_articles`는 유효구간이 반열림 `[valid_from, valid_to)`이고, gist EXCLUDE로 구간이 겹치지 않게 막는다.
  - law-history의 `version_key`는 `<MST>@<YYYYMMDD>` 형식이다(단계 시행 대응).
  - `article_no`는 조번호×100에 가지번호를 더한 값이다(제5조의2 → 502).
  - 조문 하나가 한 청크다. 6000자를 넘으면 항 단위로 쪼갠다.
- 시점 경계: 응답의 `temporal_context`는 `law_as_of`, `hsk_snapshot_as_of`, `requirements_snapshot_as_of`를 담는다.
  HSK와 CCCT는 2026-08-05에 고정되어 있으므로, 법령 기준일이 다르면 결과는 `partial`이 된다. 현행 조문으로 과거 조문을 대신하지 않는다.
- Evidence 필드 값:
  - `source_type`: `law_article|customs_requirement|hsk_entry|notice`
  - `eligibility`: `finding|supporting|blocked`
  - `hs_resolution`: `exact|unique_descendant|ambiguous|invalid|missing`
  - HSK10이 정확히 맞거나 HS6가 leaf 하나로 해소될 때만 finding 자격이 있다.
- Neo4j 그래프:
  - `(:HSNode|:HSK)-[:HAS_REQUIREMENT]->(:Requirement)-[:BASED_ON]->(:Law)`
  - `(:Requirement)-[:APPROVED_BY]->(:Agency)`, `(:Requirement)-[:REQUIRES_DOCUMENT]->(:Certificate)`
  - `(:Article)`의 `CITES`(같은 법령 안), `DELEGATES`(항상 상위→하위), `REFERS_TO`(다른 법령 계열)
- **관세율 데이터는 여기 없다.** 관세 진단은 Backend `duty_reference.py`가 담당한다.

### API (`/internal/rag/v3/*`)
| 엔드포인트 | 설명 |
|---|---|
| GET `health/live` | 인증 없음 |
| GET `health/ready` | readiness 확인 |
| POST `retrieve` | Evidence 조회 |
| POST `law-search` | query 200자 이하, top_k 20 이하, as_of 필요 |
| POST `law-catalog` | `list_laws`가 없는 백엔드는 503. dev-fixture는 빈 목록 |
| POST `guidance/query` | law-history 프로필은 의도적으로 실패 |
| guidance 전용 앱(18093/18094) | live, ready, `guidance/query`만 제공 |

오류 규칙: 토큰이 틀리면 401, 모드 헤더가 다르면 412, 헤더가 중복되면 400, 프로필 오류는 503과 고정 문구다.
OpenAPI 문서 경로는 꺼져 있다.

### 주요 환경변수
| 변수 | 설명 |
|---|---|
| `CUSTOMS_DATA_PROFILE` | 사용할 프로필 |
| `CUSTOMS_LOCAL_ROOT`, `CUSTOMS_ARTIFACT_ROOT` | 로컬 루트 재지정 |
| `POSTGRES_*`, `QDRANT_*`, `NEO4J_*` | store 접속 설정 |
| `LAW_API_OC` | 프로필 config.env에만 둔다. build·refresh 중에만 쓰고, serve에서는 환경에서 제거된다 |
| `HF_HOME`, `HF_HUB_OFFLINE` | 코드가 `_local/cache/huggingface`와 `1`로 설정한다 |

설정은 `_local/profiles/<p>/config.env`에서 전용 파서로 읽고, 저장소 `.env`는 절대 읽지 않는다.

### 검증·함정
- quick(harness 기준): `ruff format --check` → `ruff check` → `mypy` → `pytest -q`. contract: `tools/check_contract_drift.py`.
- 기본 `pytest -q`는 pyproject `testpaths` 목록만 실행한다. 새 테스트 파일은 목록에 추가한다. `live` 마커 테스트는 store가 떠 있어야 한다.
- mypy `files` 목록 밖에 있는 모듈이 있다(hybrid, repository, ingest, embed 등).
- xlsx와 jsonl의 바이트가 바뀌면 `law_history_spec.py`의 해시와 수치가 깨진다. `.gitattributes`는 py/json/jsonl/sql/md를 LF, ps1을 CRLF로 둔다.
- Windows에서 만든 영수증 경로에는 백슬래시가 들어간다. `law_history._profile_source_file`과
  `functional_uat_kr_trade(_v2)._safe_profile_file`은 이를 `/`로 정규화해 다른 OS에서도 검증되게 한다.
  `hsk_ccct_history._safe_asset_path`는 설계상 백슬래시를 거부한다(archive manifest는 POSIX 경로로 작성).
- CUDA는 CLI `--allow-cuda`로만 켤 수 있다. 파괴적 명령에는 `--confirm-profile <정확한 이름>`이 필요하다.
- registry를 바꾸면 `REGISTRY_SHA256`이 바뀐다. manifest를 다시 생성하고 Agent, Backend, Frontend 사본을 모두 갱신한다.

### 변경 색인
| 작업 | 파일 |
|---|---|
| retrieve 필드 추가 | `evidence.py`, `evidence_v3.py` → `tools/generate_evidence_v3_contracts.py` → schema → Agent 사본, `tests/test_evidence_v3_contract.py` |
| law-search/catalog | `evidence_v3.py`(search_laws_v3, law_catalog_v3), `evidence.py`, `law_history.py`, `tests/test_law_search_v3.py`(직접 실행) |
| 새 엔드포인트 | `profile_evidence.py`(인증·validator 패턴 유지) |
| guidance 토픽·scope | `guidance_registry_v1.json` / `_v2.json` + 대응 .py → manifest 재생성 |
| guidance 질의 | `guidance_v3.py`, `guidance_query_v3.schema.json` |
| 65법령 목록·기준일·고정 해시 | `law_history_spec.py` |
| 검색 순위 | `retrieval_five_stage.py`(제품), `hybrid.py`(프로토타입), `law_history_benchmark.py` |
| 검색 전략 바인딩 | manifest `retrieval_strategy ∈ {rrf_lexical, cross_encoder_rerank, five_stage_cpu}`. `build --retrieval-strategy five_stage_cpu --allow-unbenchmarked`(개발) 또는 벤치마크 `--apply-result`. five_stage_cpu는 refresh에서 유지(디지스트만 초기화). 모든 후보가 recall ≥ baseline **과** p95 ≤ 1.10×baseline을 함께 만족해야 선택된다(2026-09-15 면제 제거, `LATENCY_EXEMPT_CANDIDATES`는 빈 집합) |
| 임베딩·BM25 | `embed/model_identity.py`, `embed/bm25.py`(ENCODER_VERSION 변경 시 전체 재인덱싱; Kiwi 전환으로 v2, 기존 저장소는 `build` 재실행 필요) |
| PG 스키마 | 새 `migrations/008_*.sql` |
| 조문 그래프 규칙 | `ingest/graph.py`(extract_refs) |
| 프로필 포트·토큰 | `local_profiles.py` |

---

## 7. 이 저장소(harness)

- `harness/eng.py`(`eng.ps1` 래퍼): 명령은 `doctor`, `snapshot verify`, `setup [--repo]`, `check quick|contract --repo X [--cross]`다.
  - `setup`만 의존성을 설치하고 `.venv`/`node_modules`에 lock 지문 마커를 쓴다.
  - `check`는 마커가 현재 lock과 맞을 때만 저장소 소유 명령을 실행한다.
  - 저장소 선택자는 `frontend|backend|ai_agent|data_platform`이고, `customs-*` 이름도 받는다.
- `config/workspace.toml`: 도구 버전(Python 3.11, Git 2.40+, Node 22.12+, npm 10.9.8), 저장소별 setup/quick/contract 명령, cross_contract 쌍.
- `.nvmrc`(22.23.1): fnm/nvm 자동 전환용. 하네스는 이 저장소 폴더에서 실행되므로 frontend의 `.nvmrc`만으로는 Node 22가 잡히지 않아 `setup --repo frontend`가 EBADENGINE으로 실패했다(2026-09-15 추가).
- `config/project-snapshot.toml`: clean main일 때 `git ls-files -s -z`의 SHA-256을 기록한다(현재 r16, 2026-09-11).
  병합된 기준선이 바뀌면 maintainer가 갱신한다. 검증은 untracked 파일도 dirty로 보므로 작업 파일이 있으면 실패한다.
- `harness/validation_report.py`: 모든 저장소의 quick·contract 실행 결과, 계약 바이트, UAT·Gold 증거 인벤토리를
  `../_artifacts/validation/validation-report-<날짜>.md`와 `.json`으로 쓴다([설명](validation-report.ko.md)). 이 PC에서는 `$env:TMPDIR="C:/t"`로 실행한다.
- 테스트: `python -B -m unittest discover -s harness/tests -q`. CI는 이 테스트, `git diff --check`, gitleaks 순서다.

---

## 8. 여러 저장소를 건드리는 변경 레시피

| 변경 | 순서 |
|---|---|
| Browser API 필드·엔드포인트 | Backend 코드 → `export_product_openapi.py` → **커밋** → `check_backend.py contract` → Frontend `contract:generate` → types/decoders/client/store → `check contract --repo frontend --cross` |
| 판단 사유 코드 추가 | Backend `domain.py` enum+LABELS → OpenAPI 재생성·커밋 → Frontend `labels.ts` DECISION_REASON_OPTIONS + `types.ts` |
| Agent 심사 결과 필드 | Agent `schemas/v3.py` + `contracts/schemas/backend/v3/review-result.schema.json` → Backend 사본 복사 + `domain.py` + `_build_run` + fixture → 필요하면 OpenAPI → Frontend. `check contract --repo backend --cross` |
| `Engine.kind`·orchestrator (완료, 3.3) | Agent `_engine()`은 설치된 제공자를 보고한다. 값 추가 시 Agent schema(`review-result`, `assistant/knowledge-answer-response`) → Backend 사본 → Frontend `decodeEngine` |
| 그래프 노드·LLM purpose 추가 | Agent `service/graph.py` + `schemas/v3.py`(GraphNodeRunV3 Literal) + `adapters/llm.py` purpose + `prompts/` → schema 3곳 → Backend `run_functional_uat_matrix.py` graph_trace 상한 → Frontend `decodeGraphTrace`·`labels.graphNodeLabel` |
| Evidence 필드 | Data 모델 → 계약 생성 → Agent 사본·consumer → `check contract --repo ai_agent --cross` |
| guidance registry | Data json → manifest 재생성 → Agent·Backend·Frontend 사본 3곳 |
| LLM 설정 필드 | Agent `llm_settings.py` + 라우트 → Backend `admin_settings.py`·adapters·service → OpenAPI → Frontend `Admin.tsx` |
| Agent 보고서 문구 | Agent `report.py` → Backend `test_functional_uat_matrix_identity.py` 확인 |
| 계약 병합 뒤 | harness `project-snapshot.toml` 갱신 PR(각 저장소 PR 템플릿의 체크 항목) |

---

## 9. 공통 규약

- 커밋 메시지는 한국어 접두어를 쓴다: `추가:`, `수정:`, `개선:`, `정리:`, `병합:`, `문서:`, `보안:`. 한 줄을 "…한다"체로 쓰고, 본문은 bullet이다.
  옛 이력에는 `feat:`/`fix:`, `TASK-00xx` 형식이 섞여 있다.
- Python 저장소 공통: ruff line-length 100(규칙은 최소한만), mypy `check_untyped_defs`, `uv --locked`, LF, ps1만 CRLF.
- CODEOWNERS는 `@gdh0730`이다. 모든 저장소 CI가 uv 0.11.28, Node 22.23.1, gitleaks 8.30.1을 핀으로 고정한다.
- 서비스 settings는 `.env`를 자동으로 읽지 않는다. 값은 각 저장소의 PowerShell importer(dot-source)로 셸에 주입한다.
  생성된 토큰과 비밀번호는 `_local/profiles/<profile>/` 아래에 두고 PC마다 따로 만든다.
- transcript는 `_local/state/scenario-runs/<review-run-id>/`에 6개 파일로 남는다. Backend가 scenario, public-api,
  backend-agent를 쓰고, Agent가 agent-data, claude를 쓴다. 지식 대화는 `scenario-runs/knowledge/<conv>/turn-<id>/`에 남는다.

---

## 10. 남은 한계 (2026-09-14 기준)

- **Browser E2E 중단**: 3절 참조. 다시 도입하려면 의존성 정책(ADR)을 먼저 정하고 Frontend 스크립트와 Backend runner를 함께 복원해야 한다.
- **감사 루프의 mismatch 재판독은 UAT(LLM 분석기)에서만 돈다**: 결정론 분석기는 재판독해도 같은 값을 내므로 local-dev는 누락·미근거 필드만 재추출한다.
- **조문 그래프는 빌드 기준일 조문 기준**: `five_stage_cpu`의 DELEGATES 확장은 `(law_id, article_no)`로 기준일 조문에 되돌려 매핑하므로 개정으로 번호가 바뀐 조문은 이웃에서 빠진다(추가 전용이라 정확도는 떨어지지 않음).
  2026-09-15 수정 전에는 `graph.build()`가 `is_current`만 봐서 law-history 프로필의 그래프가 항상 0건이었다(DELEGATES 확장이 동작한 적 없음). refresh는 그래프를 다시 만들지 않으므로 개정 뒤 그래프는 마지막 build 시점 그대로다.
- **reranker는 프로필 전용 캐시에 있어야 한다**: `embed/rerank.py`는 `runtime_model_cache_directory()`(`_local/cache/huggingface`)에서 `local_files_only`로만 읽는다. 사용자 HF 캐시(`~/.cache/huggingface`)에만 있으면 `cross_encoder_rerank_cpu`·`five_stage_cpu`가 재정렬 없이 degrade되고 벤치마크는 두 후보를 measured 불가로 기록한다. 로컬은 2026-09-15에 `models--BAAI--bge-reranker-v2-m3`(revision 953dc6f6)를 복사했고, 서버는 확인이 필요하다.
- **five_stage_cpu 벤치마크 결과(2026-09-15, 로컬 8코어 CPU)**: 골드셋 100문항(`trade-doc-auditor/law_repository/data/eval/검색평가_100문항_20260810.xlsx` 골드셋 시트를
  `_local/data/law-history-heldout/goldset-100-20260810.suite.json` + 승인 영수증으로 변환, 법령 ID 단위 strict recall)로 측정했다.
  rrf_lexical r@5 0.77 / r@10 0.84 / p95 1.9s · cross_encoder_rerank_cpu 0.87 / 0.89 / p95 89.8s · five_stage_cpu 0.82 / 0.89 / p95 110s · kiwi_sparse 단독 0.63 / 0.70 / p95 53ms.
  당시 도구는 p95 면제 때문에 five_stage_cpu를 선택했지만(면제는 같은 날 제거, data `5e59e55`) CPU cross-encoder 때문에 서빙 지연이 Agent 근거 타임아웃(기본 90초, 최대 120초)을 넘는다
  (`law-search` 실측 74~118초). GPU 없는 서버는 `rrf_lexical`로 빌드하거나 reranker 풀(30)·max_length(1024)를 줄여 다시 재야 한다.
  세트는 개발 골드셋 그대로라 독립 held-out이 아니다. 결과 파일 `_local/state/law-history-benchmarks/20260915T004159Z-1bb0fc.json`.
  **서버 전략은 `rrf_lexical`로 확정(2026-09-15)**. 로컬 manifest는 재빌드 대신 `law_history.bind_law_history_retrieval_strategy(strategy="rrf_lexical", …)`를 직접 호출해
  벤치마크 digest와 함께 재바인딩했다(`--apply-result`는 도구가 고른 후보만 바인딩). rrf_lexical law-search 실측 2.2~2.5초. 서버는 `build --as-of 2026-08-05`(기본 rrf_lexical)로 빌드한다.
- **Agent 로컬 venv에 functional-uat extra가 없으면 런타임이 비어 뜬다**: `openai` 패키지가 없으면 `ProfileRuntime`이 `functional_uat_claude_configuration_invalid`로 실패하고
  서비스는 뜨되 `/health/ready`가 503(dependency_unready)이다. `uv sync --locked --group dev --extra functional-uat` 뒤 재기동한다(2026-09-15 로컬에서 겪음).
- **Data venv의 torch가 CPU 빌드로 바뀜**: `uv add kiwipiepy` 뒤 `uv sync --locked`가 `torch 2.13.0+cu126`을 락 파일의 CPU 빌드로 교체했다. CUDA는 어차피 `--allow-cuda` 뒤에 있다.
- **Agent importer는 `ANTHROPIC_API_KEY`만 읽는다**: 로컬에서 OpenAI로 띄우려면 키와 `CUSTOMS_AGENT_LLM_PROVIDER=openai`를 세션에 직접 넣는다.
- **Frontend는 `temporal_context`를 표시하지 않는다**: 기준 시점 3날짜 패널은 React 전환 뒤 없어졌다.
- **Data mypy 범위**: `pyproject`의 `files` 목록 밖 모듈(hybrid, repository, ingest, embed 등)은 타입 검사를 받지 않는다.
- **snapshot 갱신은 push와 짝**: `project-snapshot.toml`은 origin `main`의 병합 기준선을 뜻한다. 로컬 커밋을 push하기 전에는
  원격 clone에서 `snapshot verify`가 맞지 않는다.
- 배포(개인 Lightsail 번들)는 이 저장소의 범위가 아니다. 워크스페이스 밖 `_local/lightsail/README.md`를 본다.
