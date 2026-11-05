# Phase 3: persistent research and campaign recovery

Phase 3 extends the existing SQLite facts store. Phase 4 adds migration 3 for plans and queue history; Phase 5 adds migration 4 for evidence/review; see [research planning](phase4-planning.md). Migration 1 adds research events and projections; migration 2 freezes migrated legacy history. Existing fact IDs, correction links and rows survive unchanged. Imports remain legacy-unreviewed regardless of old review flags. New events are append-only; projection updates and events commit in one transaction. A database newer than the supported schema is rejected. Older clients can read legacy tables but their writes fail explicitly after migration, preventing divergent histories.

## Private data and migration

Set `BB_DATA_ROOT` to an absolute directory outside the repository, or pass `bin/bb-agent --data-root /absolute/private/directory`. The interface stores its database and memory.sqlite there; campaigns use its campaigns subdirectory. Existing `.bb` remains the default compatibility location. No data moves automatically. Operator policy, credentials and authoritative audit storage must remain separately managed; this local implementation does not establish the external Phase 6 security boundary.

Stop writers before changing selected roots. Copy memory consistently with SQLite's backup API:

```sh
python3 .claude/skills/program-memory/scripts/research_admin.py migrate --source .bb/memory.sqlite --destination /absolute/private/directory/memory.sqlite
python3 .claude/skills/program-memory/scripts/research_admin.py backup --source .bb/agent-interface.sqlite3 --destination /absolute/private/directory/agent-interface.sqlite3
```

Destination files must not exist. Migrate preserves the source and adds the new schema to the copy. Existing databases also receive uniquely named `.pre-v4-*.sqlite` backups before migration. `backup` copies without migrating; `restore` consistently copies a backup to a fresh destination. To roll back, stop writers and select the preserved source or a restored previous-version copy. Restrictive directories/databases protect local state; encryption and backup retention are operator responsibilities.

## Research operations

Each JSON operation takes session_id. The service derives principal, program, campaign and provider/client provenance from the session. Provider labels confer no authority. Agent mutations remain unreviewed; scope notes cannot change scope policy.

- `research.append`: takes record, following tools/schemas/research-record.schema.json. Required kind and content; optional details, confidence, sensitivity, basis, expires_at, sources, artifacts and external_refs.
- `research.correct`: takes entity_id, expected_revision, content and reason. Stale revisions conflict; old event content remains readable. Sensitivity and source attribution survive corrections.
- `research.query`: optional query and kind. `research.history`: entity_id required.
- `research.context`: requires entity_id; optional max_records (1–25, default 12) and max_bytes (2048–65536, default 32768). Returns a deterministic task-specific context pack.
- `research.summary`, `research.contradictions`, `research.export`: bounded derived summaries, review candidates and report-safe exports respectively.

Kinds cover program, asset, component, technology, role, functionality, observation, hypothesis, investigation, negative_result, coverage, evidence, question, queue_task, decision and technique_candidate. `details` holds application-specific facets: responsibilities/trust boundaries for components; role/tenant permissions; workflow transitions/invariants; hypothesis mechanism/preconditions/predictions; investigation methods/results/limitations; negative-result surface/version/method/conditions/retest trigger; evidence capture metadata; question resolution criteria. Queue records are research proposals; scheduling/state transitions belong to Phase 4.

`basis` distinguishes observation and inference. Agent requests cannot declare human conclusions. `sources` reference existing same-program entities and pin source revisions. Derived records inherit the strongest source sensitivity. `artifacts` contain registered opaque IDs and SHA-256 hashes, checked against program ownership and stored bytes. `external_refs` reference asset-graph, traffic-corpus or coverage IDs without duplicating those stores; these references are provenance pointers, not independently verified evidence.

All string-bearing fields, including reasons and provenance, are scanned. Known legacy secret-bearing records are suppressed on retrieval. Exports recheck content instead of trusting a report-safe label. High-sensitivity research stays out of summaries/history/export. Expired facts remain historical data and appear in expired_review. Session handoffs include bounded research records, source IDs, negative results, decisions and questions alongside persisted tasks. Summaries are derived and do not grant authorization.

## Task-specific context and reusable lessons

`research.context` takes a focus entity rather than replaying session history. It reads one SQLite snapshot and selects the focus plus its transitive source/dependent neighborhood. Selection puts the focus first, then negative results, investigations, hypotheses, questions and remaining kinds, with stable ID ordering within each group. Link counterevidence, prerequisites and methodology through sources; unlinked material is not inferred to be relevant. The pack preserves full selected records, trust and sensitivity, pins their revisions, and reports stale source references, record/byte omissions, unrelated counts and restricted/unsafe counts without exposing excluded content or IDs. A focus exceeding the byte budget is rejected. Rebuilding against unchanged records gives the same pack; changed source revisions are visible. Context packs remain derived views and grant no authority.

Reusable lessons use `research.append` with kind `technique_candidate`. Require sources and exactly three details fields: nonempty applicability, a nonempty supporting_outcomes list of source IDs, and a counterexamples list (which may be empty). Outcome IDs must reference same-program investigation or negative_result records and also appear in sources, pinning their revisions. An empty counterexamples list records absence of supplied counterexamples, not proof that none exist. All claims remain agent-unreviewed, even when supporting records describe success. Secret scanning and sensitivity inheritance apply; candidates are excluded from report-safe export.

Example params for `research.append` (replace placeholders with an existing session and investigation ID):

```json
{
  "session_id": "SESSION_ID",
  "record": {
    "kind": "technique_candidate",
    "content": "Recheck ownership at each workflow transition",
    "sources": ["INVESTIGATION_ID"],
    "details": {
      "applicability": "Multi-step workflows with object ownership boundaries",
      "supporting_outcomes": ["INVESTIGATION_ID"],
      "counterexamples": []
    }
  }
}
```

There is no agent operation to approve candidates, share them across programs or write techniques/skills. Promotion requires an independently reviewed, sanitized package and `skill-scientist` evaluation; that promotion path is not implemented by this storage increment. Program-private evidence stays in its program. No schema migration is needed for the additive candidate kind or derived context view.

## Campaign continuity

Each new campaign has a unique directory and immutable context.snapshot.json binding target, program, scope digest and execution-policy parameters. Initialization uses a campaign-owned bootstrap directory; it does not overwrite global context. Workflows receive a generated campaign context through BB_CONTEXT_FILE. Outputs belong to that campaign and cookie jars/context env files belong to the current runner. `--no-init` explicitly snapshots the selected legacy context once; resume reads the saved context and rejects target/program/scope/policy changes. Legacy campaigns without snapshots require explicit reconciliation rather than silently adopting global context.

SQLite execution records persist task identity, workflow digest, owner, heartbeat, lease expiry and result. A campaign process lock prevents simultaneous runners. Workers heartbeat while subprocesses run. Timeout/interruption terminates the process group. Expired leases become interrupted; remote outcome is treated as unknown and never automatically retried. Completed workflows retain identity across restart, including with a partial results.jsonl tail. Failed workflows also require an operator decision. Status and FSM writes are atomic; exceptions/timeout/budget exhaustion do not become done.

After reviewing available artifacts and logs, an operator can record an outcome without rerunning it:

```sh
python3 .claude/skills/campaign/scripts/campaign_runner.py reconcile --campaign CAMPAIGN_ID --task SKILL/WORKFLOW --outcome succeeded
```

Use outcome failed when success cannot be established; this keeps automatic retries blocked. A fresh investigation is a separate task/campaign decision. Interface tasks also persist launch leases; expired offline workers become interrupted, and identical replay returns that record. Phase 6 approval/isolation controls remain prerequisites for production active research. These recovery changes grant no active testing authority.
