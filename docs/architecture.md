# Architecture

| Repository | Responsibility |
| --- | --- |
| `customs-frontend` | User workbench that consumes the Backend API only |
| `customs-backend` | Authentication, cases, uploads, jobs, HITL, and operational data |
| `customs-ai-agent` | Document-review workflow, policy, and model adapters |
| `customs-data-platform` | Knowledge ingestion, Evidence API, retrieval stores, and RAG evaluation |

The harness has one responsibility: validate the sibling workspace and invoke
repository-owned setup and checks. It does not import product modules, define
cross-repository runtime wiring, or prescribe a deployment path.

An explicit `check contract --cross` compares configured provider/consumer
schema bytes only. It does not compare commits, create an approval record, or
serve as a release gate.

`config/project-snapshot.toml` records a known `main` composition. Its stable
gate is the SHA-256 of `git ls-files -s -z`; the recorded commit is a reference
only. A maintainer refreshes both values after a deliberately merged component
baseline changes.
