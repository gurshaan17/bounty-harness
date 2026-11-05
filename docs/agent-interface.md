# Offline agent interface (protocol 1.0)

`bin/bb-agent` accepts one JSON object on stdin and emits one JSON response. Python requires PyYAML, as does the existing harness. Discovery is read-only and needs no initialized context:

```sh
printf '%s\n' '{"schema_version":"1.0","operation":"capabilities","params":{}}' | bin/bb-agent
printf '%s\n' '{"schema_version":"1.0","operation":"discover","params":{}}' | bin/bb-agent
```

Operator registration snapshots a nonempty local scope file without interpreting it as active authorization:

```sh
bin/bb-agent register --program-id demo --campaign-id local --scope-file /path/to/scope.txt
```

The default database is `.bb/agent-interface.sqlite3`; operator transport option `--database PATH` selects another local database. The OS UID establishes the principal. This local transport does not isolate two clients running as the same OS user and cannot issue approvals.

Supported operations are reported by capabilities. `session.open` takes program_id, campaign_id, provider and client_version; optional predecessor_session_id must belong to that campaign. Providers: codex, opencode, claude-code, cursor, generic. The response supplies context revision and scope/policy digests. `session.resume` and `session.handoff` take session_id. A provider switch opens a new session with its predecessor ID, then reads its handoff. It never changes global context.env.

`task.submit` params follow `tools/schemas/agent-task.schema.json`. Copy program/campaign/session IDs and context digests from session.open; select exact skill/workflow and workflow_digest from discover. Supply a stable task_id and idempotency_key. Only standard-catalog/search advertises offline execution; its inputs are `{ "QUERY": "injection" }` (optional QUERY defaults to injection). No shell command, environment, output path, arbitrary import or credential can be supplied. All other workflows are denied even with approval_ref. Existing bb-run remains outside this security boundary and is unsuitable for production enforcement.

`task.status` and `task.cancel` take session_id and task_id. `artifact.read` additionally takes artifact_id and checks ownership and content hash. Artifacts are opaque database blobs, capped at 1 MiB. A handoff includes the latest 25 tasks, source IDs and explicit research-unavailable status. Research projections now accompany handoffs; see phase3-memory.md for research operations and migration. Empty summaries do not imply that an investigation found nothing.

Identical replay returns the persisted result, including across provider switches; changing the payload conflicts. Interrupted or running records never automatically rerun. After a process crash, an expired launch lease marks the task interrupted. Artifacts need reconciliation before any new investigation; identical replay never reruns it. Context changes increment revision and stale sessions/tasks fail. Provider labels record provenance, not authenticated authority.

Messages are capped at 64 KiB, nesting at 12, general strings at 8192 characters and QUERY at 256. Duplicate keys, unknown execution fields and incompatible versions fail. Exit codes: 0 success, 1 denied/failed/interrupted task, 2 contract/service error. Diagnostics exclude raw exception text. No stdout/stderr subprocess streams exist in this fixed in-process handler.

MCP, active execution, queue and review operations explicitly report unavailable. Research operations are documented in phase3-memory.md. Active workers require the separate Phase 6 isolation and approval prerequisite.
