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

The current cross-repository mappings are deliberately small and directional:

| Consumer check | Provider-owned bytes compared |
| --- | --- |
| Frontend | Backend OpenAPI v1 |
| Backend | Agent review, case-assistant, and knowledge-assistant request/response schemas |
| Agent | Data retrieval, readiness, general-guidance-query schema, and guidance source-registry manifest |

The Harness never copies these files or interprets their legal content. A
provider change is valid only when the consumer's tracked copy has the exact
same bytes.

For general guidance, this byte check is a build-time boundary, not a runtime
legal conclusion. Data returns `registry_sha256` for the registry used by the
response and a stable `registry_source_id` for every result. Agent must reject
a response unless its copied registry has the same hash and that source ID
resolves to a source with the matching kind, topic, direction, and official
URI. It validates canonical law/article details for law results and registered
title/URI details for other official results. `source_id` remains the
identifier of a concrete result record; it is not the source-family provenance
key. The Harness only ensures both contract files are exact consumer copies
and does not perform this runtime validation itself.

`config/project-snapshot.toml` records a known `main` composition. Its stable
gate is the SHA-256 of `git ls-files -s -z`; the recorded commit is a reference
only. A maintainer refreshes both values after a deliberately merged component
baseline changes.
