# Customs Engineering Harness

`customs-engineering-harness` coordinates local checks and bounded UAT for the
four independent Customs product repositories. It owns no product code and does
not use Git submodules.

## Workspace layout

Clone the five repositories as siblings under one directory:

```text
project/
  customs-engineering-harness/
  customs-frontend/
  customs-backend/
  customs-ai-agent/
  customs-data-platform/
```

The optional `_local/` and `_artifacts/` siblings stay outside Git. `_local/`
holds data, credentials, and runtime state; `_artifacts/` holds local evidence.
Neither is a publication source.

## Commands

```powershell
.\harness\eng.ps1 doctor
.\harness\eng.ps1 check quick --repo ai_agent
.\harness\eng.ps1 check contract --repo backend --task TASK-0033
.\harness\eng.ps1 uat smoke
```

`doctor` validates the expected organization remotes and sibling layout.
Product repositories own their code, API contracts, and CI; this harness only
coordinates their existing commands. See [local setup](docs/local-setup.md) and
[architecture](docs/architecture.md).
