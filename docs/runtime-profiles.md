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
| Agent operator | `ANTHROPIC_API_KEY` | Run the real Claude review adapter |

The Data profile writes raw official responses below
`_local/profiles/functional-uat-3law/data/`, downloads pinned models only to
`_local/cache/huggingface/`, and writes the redacted run manifest/result under
`_artifacts/functional-uat/<run-id>/`. Backend profile initialization creates
the local database, object-store, OIDC, and service-token settings below
`_local/profiles/functional-uat/`. These paths are all Git-ignored.

No one should place the Law API key, Claude key, raw law text, model files,
database volume, generated token, or run artifact in a repository, Docker
image, chat message, or shared ZIP. The Data and Agent repository READMEs give
the exact local commands; the Backend README gives the profile initializer and
local dependency compose command.

The normal UAT service checks the local manifest, source hashes, model cache,
minimum store counts, connectivity, mode, and token quickly. The exhaustive
PostgreSQL/Qdrant/Neo4j identifier, hash, graph, and index comparison is the
Data-owned optional `functional_uat_3law.py verify-store` command. It is for
Data maintenance after store/retrieval changes or an unexpected result, not a
per-journey UAT or release gate.

## Mode agreement

Only two inter-service mode values exist:

- `local-development` for the fixture flow;
- `functional-uat` for the real three-law flow.

Each loopback service requires and returns the same
`X-Customs-Service-Mode` value. A mismatch, missing token, stale model/source
state, or unavailable local dependency stops the request without printing a
credential or raw source value.
