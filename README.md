# Customs Engineering Harness

This is the small shared local coordinator for four independently released
Customs product repositories. It contains no product code, service runtime,
deployment definition, copied data, or secret.

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

`_local/` is optional, outside Git, and is the only location for a developer's
credentials, real data, and runtime state. `_artifacts/` is also outside Git.
Neither directory is read or created by the everyday harness commands.

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

Approval-only local runtime orchestration is intentionally outside this shared
repository. It is supplied to authorized operators separately, along with its
own secret and data inputs, and is not a harness command.

See [local setup](docs/local-setup.md) and [architecture](docs/architecture.md).
