# Runtime profiles and local inputs

The shared harness validates a workspace; it never starts a service or reads a
secret. Start commands belong to the four product repositories and always run
in this order:

```text
Data → Agent → Backend → Frontend
```

## Default team development: `dev-fixture`

This is the normal fresh-clone path. It needs no shared credential, Law API
key, Claude key, model cache, Docker container, database dump, or copied data.

- Data exposes deterministic synthetic Evidence on loopback only.
- Agent calls that Evidence HTTP endpoint with a generated local token and
  returns deterministic, human-review-only results.
- Backend keeps state in memory and issues a short-lived loopback development
  session to the Browser.
- Frontend calls Backend API v1 only.

The case AI assistant and the separate customs knowledge assistant use the
same local-development boundary. The latter receives only deterministic
official-guidance fixture data through Data; it does not receive a Case,
document text, local secret, or live web-search capability.

Profile initialization creates the loopback tokens and local service settings
under the ignored workspace `_local/profiles/dev-fixture/` directory. They are
per-machine runtime state, not team secrets. Do not commit, archive, or send
those files to another developer; each machine generates its own copy.

## Actual functional check: `functional-uat-3law`

This is an explicit local check of one real user journey. It is not a release,
deployment, signature, quality certification, or approval step. The result is
always `human_review_required`.

The only externally issued inputs are individual, approved-channel values:

| Owner | Local input | Purpose |
| --- | --- | --- |
| Data operator | `LAW_API_OC` | Retrieve exactly the three official customs laws for the fixed date `2026-08-05` |
| Agent operator | one `ANTHROPIC_API_KEY` in ignored `_local/secrets/.env` | Run the real Claude review adapter |

The Data profile writes raw official responses below
`_local/profiles/functional-uat-3law/data/`, downloads pinned models only to
`_local/cache/huggingface/`, and writes its diagnostic manifest locally.
Each review also writes its redacted detailed transcript only below
`_local/state/scenario-runs/<review-run-id>/`. Backend profile initialization
creates the local database, object-store, OIDC, and service-token settings
below `_local/profiles/functional-uat/`. These paths are all Git-ignored.

The transcript has exactly `scenario.json`, `public-api.jsonl`,
`backend-agent.jsonl`, `agent-data.jsonl`, `claude.jsonl`, and `summary.json`.
Backend owns the first two stream files; Agent owns the latter two. It stays on
the operator's machine and is never a Git, PR, chat, or shared-ZIP input.

An independent **통관 지식 도우미** turn has no Case or Review Run to attach
to that six-file record. During Functional-UAT, the Agent records its two
Agent-owned streams below the reserved
`_local/state/scenario-runs/knowledge/<conversation-id>/turn-<id>/` namespace
instead. The namespace rejects Case, Review Run, document, finding, and
correction payloads and has an Agent-owned local verifier. It is still below
the same ignored `scenario-runs` boundary; earlier explicit-development
`_local/state/knowledge-runs/` records remain preserved history, not a current
Functional-UAT destination.

No one should place the Law API key, Claude key, raw law text, model files,
database volume, generated token, or run artifact in a repository, Docker
image, chat message, or shared ZIP. The Data and Agent repository READMEs give
the exact local commands; the Backend README gives the profile initializer and
local dependency compose command.

The Agent importer reads `_local/secrets/.env` only for
`functional-uat-3law`. The file accepts one unquoted
`ANTHROPIC_API_KEY=value` entry plus blank/comment lines; it is never read for
`dev-fixture` and never reads the legacy `_local/secrets/uat/agent.env` file.
When the canonical file exists it takes precedence over an inherited terminal
value. If it is absent, a valid process-only `ANTHROPIC_API_KEY` remains a
compatibility fallback. In either case, the key is not printed or persisted by
the importer.

The normal UAT service checks the local manifest, source hashes, model cache,
minimum store counts, connectivity, mode, and token quickly. The exhaustive
PostgreSQL/Qdrant/Neo4j identifier, hash, graph, and index comparison is the
Data-owned optional `functional_uat_3law.py verify-store` command. It is for
Data maintenance after store/retrieval changes or an unexpected result, not a
per-journey UAT or release gate.

## Broad official-knowledge Functional-UAT composition

`functional-uat-3law` remains the Data-owned document-review Evidence profile.
Its local source material, three-store state, manifest, and review transcripts
are preserved as-is; they are neither renamed nor reused as a broad knowledge
snapshot.

`functional-uat-kr-trade` is the preserved V1/r3, Data-owned official-knowledge
**guidance-only** snapshot. Before it may serve, the Data operator must run
its explicit `init` → registry-approved local source `build` → `verify`
sequence. `build` and `verify` validate the staged source hashes and the local
projection; they do not make the Harness download a source or create an input
manifest. Follow the Data README for the approved input format and exact
commands.

The Data-owned `functional-uat-kr-trade` local profile keeps a **separate V2
active pointer** alongside its preserved V1/r3 pointer.  That V2 pointer must
pass `build-v2` → `verify-v2` before serving and never changes or deletes
V1/r3.  The composed **Agent runtime** is named
`functional-uat-kr-trade-v2`; it deliberately reads the same Data profile's
generated service token while selecting the V2 endpoint and registry version.
Its public registry manifest contains only the approved page identity,
authority, exact HTTPS URL, topic/scope/direction binding, and required-source
bundle; raw source bytes, local paths, and source hashes stay below ignored
`_local`.

When both document review and page-level V2 general guidance are being
checked, one Agent uses the composed `functional-uat-kr-trade-v2` profile
with two separate loopback Data peers:

| Agent operation | Data profile and default address | Permitted API |
| --- | --- | --- |
| document-review readiness and Evidence retrieval | `functional-uat-3law` at `127.0.0.1:18083` | `/health/ready`, `/retrieve` |
| preserved V1/r3 general guidance | `functional-uat-kr-trade` at `127.0.0.1:18093` | `/guidance/query` only |
| page-level V2 general guidance | `functional-uat-kr-trade-v2` at `127.0.0.1:18094` | `/guidance/query` only |

The Agent importer reads a distinct generated local token for each peer, keeps
both tokens in the current Agent process only, and requires both peers to
return `X-Customs-Service-Mode: functional-uat`. The two ports must be
different. A missing source build, token, loopback peer, or mode match stops
the corresponding request without exposing a source body or credential.

Neither guidance snapshot exposes `/retrieve` or replaces the 3-law Evidence
service. Conversely, the 3-law profile alone can continue to run the existing
review journey, but broad topics outside its coverage must remain a grounded
`no_coverage` response. V2 may be promoted only when every declared
`(topic, scope, Korean applicable direction)` bundle is `covered`; until then
the service remains unavailable or returns its explicit partial/no-coverage
boundary. The Harness does not download sources, create an input manifest,
start either Data process, or migrate local data.

## Mode agreement

Only two inter-service mode values exist:

- `local-development` for the fixture flow;
- `functional-uat` for the real three-law flow.

Each loopback service requires and returns the same
`X-Customs-Service-Mode` value. A mismatch, missing token, stale model/source
state, or unavailable local dependency stops the request without printing a
credential or raw source value.
