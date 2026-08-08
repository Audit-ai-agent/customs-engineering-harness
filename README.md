# Customs Engineering Harness

This is the small shared local coordinator for four independent
Customs product repositories. It is the team's only workspace validation
path. It contains no product code, service runtime, deployment definition,
copied data, or secret.

## Workspace

Clone all five repositories as siblings. HTTPS and SSH GitHub clone URLs are
both supported; each `origin` must identify the configured organization repo.

```text
project/
  customs-engineering-harness/
  customs-frontend/
  customs-backend/
  customs-ai-agent/
  customs-data-platform/
```

`_local/` is optional, outside Git, and holds generated local service state,
Law-API input, and real local data. `_artifacts/` is also outside Git. A
personal Claude key is never stored or read by the harness: the UAT operator
supplies it only to the current Agent process through the approved local secret
mechanism. Neither directory is read or created by the everyday harness
commands.

## Everyday commands

```powershell
.\harness\eng.ps1 doctor
.\harness\eng.ps1 snapshot verify
.\harness\eng.ps1 setup --repo backend
.\harness\eng.ps1 check quick --repo backend
.\harness\eng.ps1 check contract --repo backend
```

`setup` is the only command that installs or synchronizes dependencies. It
keeps a local lock marker in the repository's ignored dependency directory.
`check` only verifies that marker and runs the owning repository's existing
command; it never installs or removes dependencies.

The harness deliberately has no runtime command.  Each product repository
owns its own local start command; [local setup](docs/local-setup.md) gives the
small, documented order for the ordinary fixture flow and the explicit
functional-UAT flow.  No separate runtime bundle is required.

The case AI assistant and the separate customs knowledge assistant are both
product features, not Harness commands. In `dev-fixture`, their official-
guidance boundary is exercised with deterministic fixture data and no external
credential. Data now also owns an explicit, hash-verified
`functional-uat-kr-trade` **guidance-only** snapshot build. It complements the
existing three-law Evidence UAT; it neither renames nor replaces that profile.
The Harness does not collect sources, build, serve, migrate, or delete either
local profile.

`_engineering/` is a personal record/archive directory, not a sixth repository
to clone or a team execution harness.  Use this shared harness for workspace
checks, then use the owning product README for service, `dev-fixture`, and
functional-UAT commands.

Functional-UAT keeps its redacted detailed transcript only in ignored
`../_local/state/scenario-runs/<review-run-id>/`. It is never committed, added
to a PR, pasted into chat, or put in a shared ZIP.

See the [Korean teammate onboarding guide](docs/team-onboarding.ko.md),
[local setup](docs/local-setup.md), [runtime profiles](docs/runtime-profiles.md),
and [architecture](docs/architecture.md).
