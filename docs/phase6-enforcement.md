# Phase 6: scope enforcement, approvals, budgets, audit and submission gate

Phase 6 adds the execution security contract's enforcement plane to the existing provider-neutral interface. It is still an offline increment: `dispatch.authorize` is a per-request decision boundary that performs no network I/O, and no constrained worker exists. What changed is that every future active path now has a single, tested gate — and submission can no longer happen without a separate human grant bound to the exact report bytes.

## Scope registration and rules

`bin/bb-agent register` now parses the scope snapshot with `tools/execution_control/scope.py` and stores the canonical rules beside the context. Unparseable or empty scopes fail registration closed. Rules are keyed to `(program, campaign, principal, scope_digest)`; a context without stored rules denies all dispatch.

Supported syntax (see `scope.py` docstring):

- `# In Scope` / `# Out of Scope` comment headings and plain `out of scope` / `excluded` marker lines switch sections. Exclusions always win over inclusions.
- `example.com` matches exactly that host — subdomains are never inferred.
- `*.example.com` is an explicit glob matching proper subdomains only (not the apex).
- `https://app.example.com:8443/api` pins scheme, host, port and path prefix. Path prefixes are segment-aware (`/api` does not match `/apis`).
- IP and CIDR entries match resolved addresses.
- Only `http`/`https` URLs are eligible; userinfo, fragments, non-HTTP schemes and malformed URLs are denied.
- DNS resolution runs at check time; unresolved hosts and any non-public resolved address are denied unless an explicit IP rule covers them.

## Approval grants (agent requests, operator mints)

1. Agent sends `approval.request` with `session_id`, `skill`, `workflow`, `inputs`, `max_requests`, `ttl_seconds`. The service validates the workflow against the catalog and input schema, stores a `requested` record binding program, campaign, context revision, scope/policy/workflow digests and the canonical inputs digest. Inputs containing secret-like material are rejected; stored inputs are redacted.
2. Operator runs `bin/bb-agent approval-grant --request-id ID [--ttl N] [--max-requests N]`. The grant inherits every binding from the request; the agent cannot mint, extend or edit it.
3. `approval-revoke --grant-id ID --reason` withdraws a grant. Expired, revoked, consumed-budget or stale-context grants deny.

Grants allow only `GET` and `HEAD` — destructive methods and data-modifying operations are denied regardless of approval.

## dispatch.authorize — the per-request boundary

`dispatch.authorize` takes `session_id`, `grant_id`, `url`, `method` and returns `{allowed, reason, addresses}`. It enforces, in order: grant existence/liveness/binding, aggregate budget configuration, grant and campaign request budgets, method allowlist, rate interval, registered scope rules, URL/scheme/port/path checks, exclusion precedence and DNS/IP checks. Every call writes `proposed`-equivalent `approved`/`denied` audit events atomically with any budget consumption inside one `BEGIN IMMEDIATE` transaction, so concurrent authorizations cannot overspend.

A worker that does not call this boundary for every request — including every redirect — is outside the contract. Redirect targets must be re-authorized as new requests.

## Aggregate budgets

`budget-set --program-id P --campaign-id C --max-requests N [--max-concurrent N] [--min-interval-ms M]` configures per-campaign budgets in SQLite. Consumption is transactional and survives restarts and concurrent service instances (verified by threaded tests). Re-setting limits preserves consumed counters. `max_concurrent` caps simultaneously `running` interface tasks at `task.submit` time. `budget.status` lets agents read remaining budget. No budget row means dispatch is denied — fail closed.

## Audit chain and recovery

`audit_events` is an append-only, hash-chained log (`prev_hash`/`hash` per event) protected by UPDATE/DELETE triggers. Details pass through secret redaction before persistence; material that still matches secret patterns is stored as a digest only. Task lifecycle now emits `proposed`, `started`, `completed`/`failed`/`interrupted`/`cancelled`, `denied`, `approved`, `revoked`, `configured` and `submitted` events; lease-expiry recovery writes `interrupted` events for the owning principal.

Operator commands: `audit-list [--program-id P] [--limit N]` and `audit-verify` (recomputes the chain, reports `first_break_seq`). The chain detects tampering but is local SQLite — production deployment still requires audit storage outside agent write authority.

## Submission gate

`report.submit` (agent op) takes `session_id`, `finding_id` and a `report` object. It requires a `submission_grant` — issued only by the operator CLI — that binds the finding, its current revision, the submitter principal, program/campaign and the SHA-256 of the canonical report JSON. Grants are single-use, expire, and can be revoked (`submission-revoke`). A successful submission records an immutable `submissions` row and a `submitted` audit event; `delivered` is always false — no external submission transport exists.

```sh
bin/bb-agent --data-root /abs/root submission-grant --finding-id ID --report-file /abs/root/report.json --ttl 3600
```

The report file must be a JSON object; secret-scanning applies to both grant issuance and submission. Any finding revision change invalidates outstanding grants.

## What remains outside this phase

- No constrained worker executes workflows; `execution_modes` is still `["offline"]` only for `standard-catalog/search`. `active_dispatch` remains unavailable.
- Egress isolation, OS-level credential separation and policy storage outside agent write authority are deployment requirements, not provided by local SQLite.
- Process-group kill on cancellation applies to future workers; the offline handler runs in-process.
- `bb-run`/`bb-hunt` legacy paths are unchanged and remain outside this boundary.

## Validation

`tests/test_phase6.py` (19 tests) plus the updated Phase 5 assertion: documented scope headings, exact vs wildcard hosts, scheme/port/path pinning, exclusion precedence, userinfo/fragment/non-HTTP denial, DNS failure and private-address denial with explicit IP scope, approval request→grant→revoke lifecycle, missing grant/budget/scope denial, budget exhaustion surviving restart, 8-thread concurrent budget consumption capped at the configured maximum, rate limiting, audit chain verification and trigger-protected tamper resistance, redaction of secret-like material, task lifecycle audit events, exact-hash single-use submission grants, finding-revision invalidation, and CLI operator commands.
