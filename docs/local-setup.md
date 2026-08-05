# Local setup

Install Git 2.40 or newer, Python 3.11, Node.js `>=22.12 <23`, and `uv` once
on the workstation.  The Frontend pins **npm 10.9.8 exactly** through
Corepack; do not rely on the npm bundled with Node.  Run `corepack enable` once
if your Node installation has not enabled it.  The harness invokes
`corepack npm` itself, and the Frontend's `packageManager` field selects the
checked-in npm version. If Python 3.11 is not the `python` on `PATH`, set
`CUSTOMS_PYTHON` to that executable before running `eng.ps1`.

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
(Backend API), Backend (Agent review schemas), and Agent (Data Evidence
schemas):

```powershell
.\harness\eng.ps1 check contract --repo frontend --cross
```

The harness has no runtime command. Follow [runtime profiles](runtime-profiles.md)
and the product READMEs to start the services in the documented order. Default
development uses `dev-fixture` and needs no supplied secret. Actual functional
UAT is explicit and uses each operator's own Law API and Claude credentials;
those values and all generated local configuration remain outside Git.

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

## Run the actual three-law functional UAT

This is a deliberate, local functional check—not a release, deployment,
signature, quality certification, or approval.  It uses actual official-law
responses, local PostgreSQL/Qdrant/Neo4j stores, pinned local models, and the
operator's Claude account.  The only valid conclusion is that the one user
journey worked and still requires human review.

Before starting, the person running it needs two **personal, approved-channel**
credentials:

| Owner | Put only in | Never share or commit |
| --- | --- | --- |
| Data operator | `LAW_API_OC` in the ignored Data functional-UAT profile | key, raw official-law responses, model cache |
| Agent operator | current Agent terminal's `ANTHROPIC_API_KEY` | key, prompt/request/response logs |

No teammate should send `_local`, a `.env` archive, a database volume, a model
cache, or a Docker image to satisfy this step.  Backend/Data initialization
creates all remaining passwords and loopback service tokens locally.

Prepare Data first.  `init` creates the ignored profile; add only the personal
`LAW_API_OC` through the approved secret method before `build`.

```powershell
Set-Location ..\customs-data-platform
uv sync --locked --group dev --extra store --extra rag --extra ingestion --python 3.11
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_3law.py init
# Add LAW_API_OC only to the ignored _local/profiles/functional-uat-3law/config.env.
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_3law.py compose up
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_3law.py build --as-of 2026-08-05
```

`build` and the normal UAT server perform only bounded source/cache/count and
connectivity checks. A Data maintainer may explicitly run the costly complete
PostgreSQL/Qdrant/Neo4j ID, hash, graph, and index comparison after Data
ingestion/retrieval changes or an unexpected result; it is not required for
every functional UAT journey.

```powershell
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_3law.py verify-store
```

In a separate Data terminal, serve the validated build:

```powershell
Set-Location ..\customs-data-platform
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_3law.py serve `
  --host 127.0.0.1 --port 18083
```

Create the unrelated Backend local profile once, then start the remaining
three terminals in order:

```powershell
# One-time Backend functional-UAT profile creation
Set-Location ..\customs-backend
uv sync --locked --group dev --extra unit --extra functional-uat --python 3.11
uv run --no-sync customs-backend-profile-init --profile functional-uat
```

```powershell
# Agent terminal — supply personal ANTHROPIC_API_KEY only to this process by
# the approved local secret mechanism before running the server.
Set-Location ..\customs-ai-agent
uv sync --locked --group dev --extra functional-uat --python 3.11
. .\tools\Import-AgentProfile.ps1 -Profile functional-uat-3law
uv run --locked --no-sync --group dev --extra functional-uat customs-ai-agent-functional-uat-serve
```

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
human-review result).  The Data manifest records date, law IDs, source hashes,
model revisions, store counts, and build time only; it is diagnostic metadata,
not an approval record.  Results stay in ignored
`_artifacts/functional-uat/<run-id>/`.

Stop Frontend, Backend, Agent, and Data with `Ctrl+C`.  Then stop only the
functional Backend dependencies if they are no longer needed:

```powershell
Set-Location ..\customs-backend
$Profile = '..\_local\profiles\functional-uat\backend.env'
docker compose --project-name customs-functional-uat --env-file $Profile `
  -f deploy\functional-uat\compose.yaml down
```

For a fresh three-law build on the same machine, use Data's explicit scoped
reset.  It removes only that profile's store volumes and active manifest; it
preserves raw local source files and UAT artifacts.

```powershell
Set-Location ..\customs-data-platform
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_3law.py reset `
  --confirm-profile functional-uat-3law
```
