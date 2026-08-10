# Local setup

Install Git 2.40 or newer, Python 3.11, Node.js `>=22.12 <23`, and `uv` once
on the workstation.  The Frontend pins **npm 10.9.8 exactly** through
Corepack; do not rely on the npm bundled with Node.  Run `corepack enable` once
if your Node installation has not enabled it.  The harness invokes
`corepack npm` itself, and the Frontend's `packageManager` field selects the
checked-in npm version. If Python 3.11 is not the `python` on `PATH`, set
`CUSTOMS_PYTHON` to that executable before running `eng.ps1`.

For the Korean guide to teammate access, inputs that must remain local, and
the boundary between ordinary development and functional UAT, see
[team onboarding](team-onboarding.ko.md).

Clone the harness and four product repositories as direct siblings. HTTPS and
SSH clone URLs both work, but each `origin` must identify the configured
`Audit-ai-agent` repository.

```text
project/
  customs-engineering-harness/
  customs-frontend/
  customs-backend/
  customs-ai-agent/
  customs-data-platform/
  _local/                 # ignored personal runtime state and inputs
  _artifacts/             # ignored local run results
```

Do not add the historical Data Platform `upstream`; it is not a team workspace
requirement.

## Fresh clone

From `customs-engineering-harness`:

```powershell
.\harness\eng.ps1 doctor
.\harness\eng.ps1 snapshot verify
.\harness\eng.ps1 setup
```

`snapshot verify` is only a clean-`main` baseline check. Run it before making a
branch or local change. `setup` is the only harness command that installs or
synchronizes dependencies. It reuses a healthy local dependency directory;
`check` never installs, deletes, or cleans a dependency/cache.

For a focused change, use the affected product only:

```powershell
.\harness\eng.ps1 setup --repo <frontend|backend|ai_agent|data_platform>
.\harness\eng.ps1 check quick --repo <repo>
.\harness\eng.ps1 check contract --repo <repo>
```

When a public provider schema changes, add `--cross` to compare the actual
sibling provider bytes with its consumer copy. It is available for Frontend
(Backend API), Backend (Agent review, case-assistant, and knowledge-assistant
schemas), and Agent (Data Evidence, general-guidance-query schema, and
guidance source-registry manifest):

```powershell
.\harness\eng.ps1 check contract --repo frontend --cross
.\harness\eng.ps1 check contract --repo backend --cross
.\harness\eng.ps1 check contract --repo ai_agent --cross
```

The harness has no runtime command. Follow [runtime profiles](runtime-profiles.md)
and the product READMEs to start the services in the documented order. Default
development uses `dev-fixture` and needs no supplied secret. Actual functional
UAT is explicit and uses each operator's own Law API and Claude credentials;
those values and all generated local configuration remain outside Git.

The standalone customs knowledge assistant is a product screen/API, not a
separate Harness runtime. In `dev-fixture` it uses the deterministic Data
guidance fixture. Data also owns the separate
`functional-uat-kr-trade` guidance-only profile and its explicit source
`build`/`verify` commands. Use the Data README for that local, hash-verified
source build; it does not rename, reset, or replace the current
`functional-uat-law-history` three-store Evidence UAT. The retained
`functional-uat-3law` profile remains a separate compatibility path.

## Start the complete fixture stack

Run this after the fresh-clone commands above.  It is the normal team workflow:
it exercises the real Frontend → Backend → Agent → Data HTTP path, but uses
deterministic synthetic Evidence and a deterministic Agent.  It needs no
Docker, Law API key, Claude key, model download, database dump, or copied
`_local` directory.

First create the one local Backend profile.  This generates per-machine
loopback values; it is not a file to hand to another teammate.

```powershell
Set-Location ..\customs-backend
uv run --no-sync customs-backend-profile-init --profile dev-fixture
```

Then keep these four terminals open, in this order:

```powershell
# Terminal 1 — Data
Set-Location ..\customs-data-platform
uv run --locked --no-sync --group dev python tools/run_dev_fixture_evidence.py `
  --host 127.0.0.1 --port 18083
```

```powershell
# Terminal 2 — Agent (after Data is ready)
Set-Location ..\customs-ai-agent
. .\tools\Import-AgentProfile.ps1 -Profile dev-fixture
uv run --locked --no-sync --group dev customs-ai-agent-dev-serve
```

```powershell
# Terminal 3 — Backend (after Agent is ready)
Set-Location ..\customs-backend
. .\tools\Import-LocalProfile.ps1 -Profile dev-fixture -Scope backend
uv run --no-sync customs-backend-api
```

```powershell
# Terminal 4 — Frontend (after Backend is ready)
Set-Location ..\customs-frontend
corepack npm run dev:local
```

Open the loopback URL printed by Vite (normally `http://127.0.0.1:5173`) and
run the small representative path once: create a Case, add a `.txt` document,
start a review, and inspect the human-review result.  The Browser obtains its
development session from the loopback Backend; there is no password to copy or
team secret to distribute.  Stop each process with `Ctrl+C` when finished.

## Run the actual Functional-UAT document review: 65-law Evidence history

This is a deliberate, local functional check—not a release, deployment,
signature, quality certification, or approval.  It uses actual official-law
responses, local PostgreSQL/Qdrant/Neo4j stores, pinned local models, and the
operator's Claude account.  The only valid conclusion is that the one user
journey worked and still requires human review.

The default Evidence profile is `functional-uat-law-history`. It collects the
fixed 65-law registry as the explicit `2026-08-05` baseline and accumulates
later official law versions only through an operator-requested refresh. It
keeps law provenance and legal dates separate from the fixed `2026-08-05` HSK
and requirements snapshots. The retained `functional-uat-3law` profile is a
compatibility path, not an input to or fallback for law history. A separately
built `functional-uat-kr-trade` profile serves only general-guidance queries;
it is not a substitute for Evidence retrieval.

Before starting, the person running it needs two **personal, approved-channel**
credentials:

| Owner | Put only in | Never share or commit |
| --- | --- | --- |
| Data operator | `LAW_API_OC` in the ignored Data functional-UAT profile | key, raw official-law responses, model cache |
| Agent operator | one `ANTHROPIC_API_KEY` in ignored `../_local/secrets/.env` | key, prompt/request/response logs |

No teammate should send `_local`, a `.env` archive, a database volume, a model
cache, or a Docker image to satisfy this step.  Backend/Data initialization
creates all remaining passwords and loopback service tokens locally.

Prepare Data first.  `init` creates the ignored profile; add only the personal
`LAW_API_OC` through the approved secret method before `build`.

```powershell
Set-Location ..\customs-data-platform
uv sync --locked --group dev --extra store --extra rag --extra ingestion --python 3.11
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_law_history.py init
# Add LAW_API_OC only to the ignored _local/profiles/functional-uat-law-history/config.env.
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_law_history.py compose up
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_law_history.py build --as-of 2026-08-05
```

`build` and the normal UAT server perform only bounded source/cache/count and
connectivity checks. A Data maintainer may explicitly run the costly complete
PostgreSQL/Qdrant/Neo4j ID, hash, graph, and index comparison after Data
ingestion/retrieval changes or an unexpected result; it is not required for
every functional UAT journey.

```powershell
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_law_history.py verify-store
```

In a separate Data terminal, serve the validated build:

```powershell
Set-Location ..\customs-data-platform
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_law_history.py serve `
  --host 127.0.0.1 --port 18083
```

Every history Evidence response exposes the law date, HSK snapshot date, and
requirements snapshot date separately. For a later legal review date, the
latter two remain `2026-08-05` and the result is explicitly `partial`; do not
present it as a same-date-complete answer. To extend legal coverage, run the
Data-owned explicit command `refresh --from YYYY-MM-DD --to YYYY-MM-DD` after
the baseline succeeds. It is not a background update or a call the Harness
makes.

### Add broad official guidance to the same Functional-UAT

Use this additional Data peer when the Functional-UAT must exercise the
standalone **통관 지식 도우미** or the general-guidance section of a mixed
case question. It complements the already-running law-history Evidence
service; it does not replace it.

The Data operator must first initialize the separate ignored profile, stage
only registry-approved official source inputs as described in the Data README,
then complete `build` and `verify`. The `--source-manifest` value is a relative
path below that profile's ignored `data/guidance-sources/` directory. Do not
put raw source material or a source manifest in Git.

```powershell
Set-Location ..\customs-data-platform
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py init
# Stage the approved local input below _local/profiles/functional-uat-kr-trade/data/guidance-sources/.
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py build `
  --source-manifest data/guidance-sources/<approved-source-build.json>
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py verify
```

Keep the law-history server at `127.0.0.1:18083`. In another Data terminal, start
the verified guidance snapshot at a different loopback port:

```powershell
Set-Location ..\customs-data-platform
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py serve-guidance `
  --host 127.0.0.1 --port 18093
```

`serve-guidance` exposes only `/internal/rag/v3/guidance/query`. It cannot
answer document-review `/retrieve` calls, and it stops if the local source
build is missing or no longer verifies. The history server remains the only
Data peer for review readiness and Evidence retrieval.

For TASK-0046 page-level V2 guidance, preserve the V1/r3 pointer above and
build the separate V2 pointer explicitly.  The V2 build accepts only the
registered official page IDs, exact HTTPS registry URLs, hash-verified local
raw bytes, date/effective-date evidence where required, and excerpts that are
traceable to those bytes.  Do not place any source input or raw page in Git.

```powershell
Set-Location ..\customs-data-platform
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py build-v2 `
  --source-manifest data/guidance-sources/<approved-v2-source-build.json>
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py verify-v2
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py serve-guidance-v2 `
  --host 127.0.0.1 --port 18094
```

V2 exposes the same guidance query path and a readiness endpoint only.  It is
not a second Evidence service.  The V2 pointer, registry version/hash, source
receipt, and local source hash are rechecked before use; any mismatch returns
the explicit unavailable boundary.

Create the unrelated Backend local profile once, then start the remaining
three terminals in order:

```powershell
# One-time Backend functional-UAT profile creation
Set-Location ..\customs-backend
uv sync --locked --group dev --extra unit --extra functional-uat --python 3.11
uv run --no-sync customs-backend-profile-init --profile functional-uat
```

```powershell
# Create ../_local/secrets/.env once on this PC with exactly:
# ANTHROPIC_API_KEY=personal-key
# The functional Agent importer reads that one value into this terminal only.
Set-Location ..\customs-ai-agent
uv sync --locked --group dev --extra functional-uat --python 3.11
# Review-only legacy path: . .\tools\Import-AgentProfile.ps1 -Profile functional-uat-3law
# Preserved V1/r3 knowledge path: . .\tools\Import-AgentProfile.ps1 -Profile functional-uat-kr-trade -EvidencePort 18083 -GuidancePort 18093
# Default V2 composition: law-history Evidence on 18083 + verified V2 guidance on 18094.
. .\tools\Import-AgentProfile.ps1 -Profile functional-uat-kr-trade-v2-law-history `
  -EvidencePort 18083 -GuidancePort 18094
uv run --locked --no-sync --group dev --extra functional-uat customs-ai-agent-functional-uat-serve
```

The composed Agent profile reads the two generated Data service tokens only
into this Agent process. It fails before startup if either token is missing,
the ports match, or the selected guidance snapshot is not available. If V2
has not been built and verified, do not fall back silently to V1/r3: use the
law-history-only `functional-uat-law-history` importer for document review or
keep the requested general question at the explicit unavailable boundary.

```powershell
# Backend terminal
Set-Location ..\customs-backend
. .\tools\Import-LocalProfile.ps1 -Profile functional-uat -Scope backend
$Profile = '..\_local\profiles\functional-uat\backend.env'
docker compose --project-name customs-functional-uat --env-file $Profile `
  -f deploy\functional-uat\compose.yaml up -d --wait
uv run --no-sync customs-backend-api
```

```powershell
# Frontend terminal
Set-Location ..\customs-frontend
corepack npm run dev:functional-uat
```

Sign in through the local Keycloak page as `local-functional-reviewer`.  To
avoid displaying the password, copy it only to the current user's clipboard,
paste it into that page, and clear the clipboard immediately:

```powershell
Set-Location ..\customs-backend
.\tools\Copy-FunctionalUatPassword.ps1
# Paste into the local Keycloak page, then:
Set-Clipboard -Value ''
```

Run exactly one representative journey (Case → `.txt` document → review →
human-review result). The Data manifest records date, law IDs, source hashes,
model revisions, store counts, and build time only; it is diagnostic metadata,
not an approval record.

Every functional-UAT review automatically writes a redacted detailed local
transcript under `../_local/state/scenario-runs/<review-run-id>/`:

```text
scenario.json          public-api.jsonl       backend-agent.jsonl
agent-data.jsonl       claude.jsonl           summary.json
```

Backend owns the public/API-to-Agent files and Agent owns the Data/Claude
files. The recorder excludes credentials, session/cookie/auth data, provider
error bodies, and literal local secret values. Keep this directory local: do
not commit it, attach it to a PR, paste it into chat, or include it in a shared
ZIP.

If this Functional-UAT also exercises the standalone **통관 지식 도우미**, its
Agent-owned turn record is kept separately below
`../_local/state/scenario-runs/knowledge/<conversation-id>/turn-<id>/`. It
contains no Case, Review Run, document, finding, or correction payload and is
validated with the Agent repository's local knowledge-transcript verifier. It
remains part of the same ignored `scenario-runs` boundary; do not move it into
`_artifacts`, Git, or a shared report.

Stop Frontend, Backend, Agent, and Data with `Ctrl+C`.  Then stop only the
functional Backend dependencies if they are no longer needed:

```powershell
Set-Location ..\customs-backend
$Profile = '..\_local\profiles\functional-uat\backend.env'
docker compose --project-name customs-functional-uat --env-file $Profile `
  -f deploy\functional-uat\compose.yaml down
```

For a fresh law-history build on the same machine, use Data's explicit scoped
reset. It removes only that profile's store volumes and active manifest; it
preserves raw local source files and UAT artifacts.

```powershell
Set-Location ..\customs-data-platform
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_law_history.py reset `
  --confirm-profile functional-uat-law-history
```

### Run the retained three-law compatibility profile deliberately

Use `functional-uat-3law` only when a bounded compatibility check is the
intent. Its raw local source, stores, and manifest stay independent of
law-history. When it runs at the same time as the default profile, choose an
explicit different loopback port for the legacy server and pass that port to
the Agent importer; `18083` is reserved for law-history in the default
topology. The Data README owns the exact legacy build, serve, and reset
commands. It must never replace a missing law-history result.
