# Local setup

Install Git 2.40 or newer, Python 3.11, Node.js 22.22 or newer (before 23), npm, and `uv` once on the
workstation. `doctor` reports a precise missing or incompatible prerequisite.
If Python 3.11 is not the `python` on `PATH`, set `CUSTOMS_PYTHON` to its
executable before running `eng.ps1`.

Clone the harness and four product repositories as direct siblings. Do not add
the historical Data Platform `upstream`; it is personal provenance, not a team
workspace requirement.

Run this sequence after a fresh clone or a lock-file change:

```powershell
.\harness\eng.ps1 doctor
.\harness\eng.ps1 snapshot verify
.\harness\eng.ps1 setup
```

Use `setup --repo <frontend|backend|ai_agent|data_platform>` when only one
repository changed. Then run only the affected repository's `quick` or
`contract` check. Setup synchronizes dependencies in place and checks never
install, delete, or clean caches. The harness does not create a virtual
environment outside a product repository, start containers, or handle
credentials.

Keep credentials, real datasets, runtime state, and non-reproducible evidence
under the sibling `_local/` or `_artifacts/` directories. Never copy them into
any of the five Git repositories.

Authorized runtime exercises use a separately delivered operator package. It
does not belong in this repository and must not be added as a Git remote,
submodule, or source copy.
