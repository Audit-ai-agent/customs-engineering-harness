# Architecture

The project consists of four independently released repositories:

| Repository | Responsibility |
| --- | --- |
| `customs-frontend` | Backend-API-only user workbench |
| `customs-backend` | Auth, cases, uploads, jobs, HITL, and operational data |
| `customs-ai-agent` | Document-review workflow and model adapters |
| `customs-data-platform` | Evidence API, knowledge ingestion, and retrieval stores |

This repository is a shared local coordinator. It reads the sibling repositories
through their published interfaces and configured paths; it neither imports their
source nor owns their release lifecycle. `config/project-snapshot.toml` records
the component revisions coordinated by this harness release.
