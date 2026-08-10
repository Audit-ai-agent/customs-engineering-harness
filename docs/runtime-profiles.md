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

## Actual functional check: `functional-uat-law-history` (default)

This is an explicit local check of one real user journey. It is not a release,
deployment, signature, quality certification, or approval step. The result is
always `human_review_required`.

The only externally issued inputs are individual, approved-channel values:

| Owner | Local input | Purpose |
| --- | --- | --- |
| Data operator | `LAW_API_OC` | Retrieve the fixed 65-law official registry for the `2026-08-05` baseline; later changes require an explicit refresh |
| Agent operator | one `ANTHROPIC_API_KEY` in ignored `_local/secrets/.env` | Run the real Claude review adapter |

The Data profile writes raw official responses below
`_local/profiles/functional-uat-law-history/data/`, downloads pinned models only to
`_local/cache/huggingface/`, and writes its diagnostic manifest locally.
Each review also writes its redacted detailed transcript only below
`_local/state/scenario-runs/<review-run-id>/`. Backend profile initialization
creates the local database, object-store, OIDC, and service-token settings
below `_local/profiles/functional-uat/`. These paths are all Git-ignored.

The profile keeps each official law version's local receipt, official URI,
hash, collection time, effective date, and article count. The legal source is
searched at the requested review date before ranking; it is never replaced
after retrieval with current text. Its Evidence response carries
`temporal_context`: `law_as_of` for the law result and the fixed `2026-08-05`
dates for HSK and requirements. A later law date therefore remains explicitly
`partial` for HSK/requirements rather than being presented as one
same-date-complete result. Dates before the baseline or outside an explicitly
refreshed history remain a bounded coverage failure, not a model-generated
substitute. Refresh is an operator command only; there is no scheduler or
automatic polling.

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

The Agent importer reads `_local/secrets/.env` only for its explicitly selected
Functional-UAT profiles, including `functional-uat-law-history` and the
composed V2 law-history profile. The file accepts one unquoted
`ANTHROPIC_API_KEY=value` entry plus blank/comment lines; it is never read for
`dev-fixture` and never reads the legacy `_local/secrets/uat/agent.env` file.
When the canonical file exists it takes precedence over an inherited terminal
value. If it is absent, a valid process-only `ANTHROPIC_API_KEY` remains a
compatibility fallback. In either case, the key is not printed or persisted by
the importer.

The normal UAT service checks the local manifest, source hashes, model cache,
minimum store counts, connectivity, mode, and token quickly. The exhaustive
PostgreSQL/Qdrant/Neo4j identifier, hash, graph, and index comparison is the
Data-owned optional `functional_uat_law_history.py verify-store` command. It
is for Data maintenance after store/retrieval changes or an unexpected result,
not a per-journey UAT or release gate.

## Legacy compatibility check: `functional-uat-3law`

`functional-uat-3law` retains the three official customs-law sources and its
existing local state as a review-only compatibility path. It is not renamed,
migrated, or used to fill a missing law-history result. The default
`127.0.0.1:18083` Evidence address belongs to `functional-uat-law-history`.
When an operator intentionally runs both profiles, the legacy server must use
an explicitly selected, different loopback port; no service is allowed to
silently take over the default history port.

## Broad official-knowledge Functional-UAT composition

`functional-uat-law-history` is the Data-owned default document-review
Evidence profile. Its law receipts, temporal store, manifest, and review
transcripts remain separate from all guidance snapshots. The retained
`functional-uat-3law` material is preserved as-is; it is neither renamed nor
reused as a broad knowledge snapshot.

`functional-uat-kr-trade` is the preserved V1/r3, Data-owned official-knowledge
**guidance-only** snapshot. Before it may serve, the Data operator must run
its explicit `init` → registry-approved local source `build` → `verify`
sequence. `build` and `verify` validate the staged source hashes and the local
projection; they do not make the Harness download a source or create an input
manifest. Follow the Data README for the approved input format and exact
commands.

The Data-owned `functional-uat-kr-trade` local profile keeps a **separate V2
active pointer** alongside its preserved V1/r3 pointer. That V2 pointer must
pass `build-v2` → `verify-v2` before serving and never changes or deletes
V1/r3. The default composed **Agent runtime** is
`functional-uat-kr-trade-v2-law-history`; it selects the law-history Evidence
peer plus the V2 endpoint and registry version. The existing
`functional-uat-kr-trade-v2` selection continues to mean V2 + legacy
three-law Evidence. The public registry manifest contains only the approved
page identity, authority, exact HTTPS URL, topic/scope/direction binding, and
required-source bundle; raw source bytes, local paths, and source hashes stay
below ignored `_local`.

When both document review and page-level V2 general guidance are being
checked, one Agent uses the composed
`functional-uat-kr-trade-v2-law-history` profile with two separate loopback
Data peers:

| Agent operation | Data profile and default address | Permitted API |
| --- | --- | --- |
| document-review readiness and Evidence retrieval | `functional-uat-law-history` at `127.0.0.1:18083` | `/health/ready`, `/retrieve` |
| preserved V1/r3 general guidance | `functional-uat-kr-trade` at `127.0.0.1:18093` | `/guidance/query` only |
| page-level V2 general guidance | `functional-uat-kr-trade-v2` at `127.0.0.1:18094` | `/guidance/query` only |

The Agent importer reads a distinct generated local token for each peer, keeps
both tokens in the current Agent process only, and requires both peers to
return `X-Customs-Service-Mode: functional-uat`. The two ports must be
different. A missing source build, token, loopback peer, or mode match stops
the corresponding request without exposing a source body or credential.

Neither guidance snapshot exposes `/retrieve` or replaces the law-history
Evidence service. Conversely, the legacy three-law profile can continue to run
its bounded review journey, but broad topics outside its coverage must remain a
grounded `no_coverage` response. V2 may be promoted only when every declared
`(topic, scope, Korean applicable direction)` bundle is `covered`; until then
the service remains unavailable or returns its explicit partial/no-coverage
boundary. The Harness does not download sources, create an input manifest,
start either Data process, or migrate local data.

## Mode agreement

Only two inter-service mode values exist:

- `local-development` for the fixture flow;
- `functional-uat` for the real law-history flow and its explicit legacy
  compatibility path.

Each loopback service requires and returns the same
`X-Customs-Service-Mode` value. A mismatch, missing token, stale model/source
state, or unavailable local dependency stops the request without printing a
credential or raw source value.
