# Implementation ledger

## Phase 2 — offline implementation, 2026-11-05

Implemented canonical YAML discovery, digest binding, versioned strict JSON task/session/result contracts, bounded messages, offline dispatch through a fixed existing skill handler, persisted sessions/tasks, provider switching, idempotency, program/principal ownership, stale-context denial, cancellation records and hashed opaque artifacts. Repaired adapter source/ownership handling, atomic writes, backups, regeneration and uninstall. Existing CLI interfaces are preserved.

Validation: 22 focused tests passed; make test completed successfully with the existing /tmp/bh-audit-venv Python/PyYAML environment. General validator still reports SOME FAIL, average 82/100 (known baseline); security and workflow audits still exit 2. These existing audit failures were not weakened or counted as passes. No database migration was needed because the interface uses a separate local database. No target testing occurred.

Native checks: Codex 0.165.0 strict configuration accepted; OpenCode v2.2.0 discovered generated research/reviewer agents. Claude is unavailable. See adapters.md for sources and exact limits.

Remaining acceptance: full native invocation/permission/session checks, external scope/approval execution prerequisite, and production isolation are not established. Phase 2 is therefore delivered as an offline increment, not declared fully accepted. Optional MCP is unavailable. Research handoffs explicitly defer projections to Phase 3. Legacy bb-run does not inherit the new boundary. Crash recovery returns persisted states without automatic retries; no durable worker leases are claimed.

## Phase 3 — research storage increment

Added additive legacy-fact migration, research event/projection storage, program-bound query/history/correction, revision conflicts, recursive secret checks and private data-root transport selection. See phase3-memory.md. Phase 3 remains incomplete: campaign snapshot/recovery work and full acceptance verification are outstanding. No completion claim or production activation is made.

## Phase 3 completion — 2026-11-05

Completed local Phase 3 persistence and recovery acceptance. Changes: numbered transactional migrations over existing facts; consistent pre-migration backup and operator copy/restore; preserved IDs/correction links; immutable research events and projections; recursive secret scanning and export rescan; sensitivity inheritance and source revision pinning; schema-validated research records; program-bound queries/history/corrections; hypothesis/negative-result/decision summaries; expired-fact and contradiction review views; provider-switch handoffs; configurable private data root; campaign-owned bootstrap/context snapshots; per-run cookies; transactional task identity/leases/heartbeats; process-group cancellation; unknown-outcome reconciliation; atomic status and accurate terminal states. Legacy insecure review/secret/cross-program overrides now fail explicitly.

Validation: `PATH=/tmp/bh-audit-venv/bin:$PATH make test` passed, including 34 behavioral tests plus existing YAML/compile checks. Tests cover original-row and correction-link preservation, rollback on migration failure, backup/restore history equality, competing stale corrections, sensitivity/secret metadata, cross-program source/artifact denial, tampered artifacts, provider switch, expiry, snapshot identity changes, concurrent runner exclusion, interrupted lease reconciliation, completed-task recovery with partial JSONL, and failure status preservation. `git diff --check` passed.

General validator still reports the same three baseline failures (binary-analysis, race-condition, sqli: 69/100), average 82/100. Changed packages pass: program-memory 89/100, campaign 82/100. Security/workflow audits retain their pre-existing exit-2 failures; thresholds and checks were not weakened. Gitleaks was unavailable locally and Docker daemon was stopped; no generated evidence, credentials or runtime databases are included in commits.

Limits belong to later phases: external execution isolation/approval enforcement, durable research planning queue, independent human review and report submission. External domain-store IDs are provenance references; evidence references require registered artifact ownership/hash agreement. Existing programs are not actively tested by this work. Native client limitations from Phase 2 remain recorded above.

Commits: `5b2f14b` — offline agent contracts and governed research persistence; following commit `feat: make campaign context and recovery durable` — campaign snapshots, execution recovery and Phase 3 acceptance/ledger. Final lint: `ruff check tools/ bin/bb-tools-doctor.py` passed with ruff 0.17.3 installed only in the temporary validation environment. Final make test passed after formatting changes.
