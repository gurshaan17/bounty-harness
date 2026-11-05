# Phase 4: research planning, durable queue and completion

Research planning is an offline workflow over the Phase 3 program memory. The planner skill owns question generation, ranking and queue semantics; the campaign skill owns completion assessment. `bin/bb-agent` supplies the same session-bound operations to every provider. Neither planning nor queue readiness grants target access, human confirmation or submission permission. The legacy technique-ranking workflows remain available separately.

## Inputs and generated questions

Record application knowledge through `research.append`: components/trust boundaries, roles/permissions, functionality with `details.surface`, invariants/transitions, observations, hypotheses, investigations, negative results, evidence and coverage. Link related records through `sources`. Reference asset-graph, traffic-corpus and coverage IDs through `external_refs`; register the route/assessment knowledge needed for planning as research records. The planner consumes those pinned research representations and references, not arbitrary paths or unverified external database contents. It does not infer that a provenance pointer proves a route or finding.

`plan.generate` reads a consistent program snapshot, current queue and prior program queue. It pins the session context revision and scope/policy digests, visible research revisions, the full research revision digest and queue revisions. Missing component, role or functionality knowledge produces an architecture-understanding task before probe proposals. Otherwise it generates questions about object ownership and role/tenant isolation for each functionality; recorded transitions/invariants add a workflow question. Related hypotheses, investigations, evidence, counterevidence and coverage inform each proposal. At most 40 functionality records are used in default generation; explicit proposals can target additional surfaces. Default methods analyze existing local evidence only.

Example JSON request sent to `bin/bb-agent`:

```json
{
  "schema_version": "1.0",
  "operation": "plan.generate",
  "params": {"session_id": "SESSION_ID"}
}
```

All examples require an existing program/campaign registration and session, using the same `BB_DATA_ROOT` as Phase 3. Use `bin/bb-agent --data-root /absolute/private/root` for individual JSON operations, or export `BB_DATA_ROOT` for both the interface and skill workflow. No production testing authorization is established by registration.

To save both outputs, put the request in a local JSON file, initialize the normal run context, then run:

```sh
RESEARCH_REQUEST=/absolute/private/root/research-request.json bin/bb-run planner research-plan
```

The registered workflow writes `$OUTDIR/planner/research-plan.json` and `research-plan.md` with private file permissions. It invokes only `plan.generate`. The interface response also includes the readable `markdown` field. Requests and runtime plans remain outside Git.

## Proposals and scoring

Optional `proposals` replaces default questions once architecture is known. Each item follows `tools/schemas/research-opportunity.schema.json`: question, hypothesis, optional existing hypothesis ID, surface, boundary, roles, potential impact, supporting/contradicting source IDs, alternative explanations, prerequisites, method, expected artifacts, stop condition and existing task dependencies. Unknown fields, caller-supplied scores/states and foreign/inaccessible source references fail validation. References and strings pass the existing secret/sensitivity checks.

A method is either `{"kind":"offline_analysis"}` or `{"kind":"workflow","skill":"...","workflow":"..."}`. Workflow proposals require a registered workflow with complete security metadata, a permitted safety tier and no side effects beyond declared `network_read`. Unknown or unsafe methods are filtered before ranking regardless of impact. Only `offline_analysis` can acquire a queue lease; workflow proposals cannot launch tools through the queue. A method suggestion never establishes approval.

Normalized weights default to impact 30, evidence quality 25, research value 30 and effort 15; supply all four nonnegative finite numeric values through `weights` to change them. The planner computes components from boundary specificity, current evidence/investigation sources, current adequately-investigated coverage and prerequisite count. Attempted coverage does not close a research gap. The effort component rewards smaller investigations. Impact is a conservative boundary-relevance heuristic, not demonstrated severity. Scores include their components, weights and a readable rationale. Identical proposals, revisions and weights retain deterministic ordering while evidence validity is unchanged; expiry is an explicit time-dependent retest trigger.

Deduplication uses program, normalized surface, boundary and hypothesis. Existing work in another campaign suppresses duplicate dispatch. Repeated completed/disposed work stays suppressed unless application/source facts change, negative evidence expires or the registered method changes. Reopening records a reason and preserves events. A negative result identifies its investigation through `details.identity`, or the exact surface/boundary/hypothesis fields, with optional method and `expires_at`. Completion-generated negative results include those fields. Newly recorded negative/coverage/investigation outputs alone do not count as changed application facts.

`plan.get` takes `plan_id`, returns the immutable plan and marks a changed research snapshot stale. Planning after new evidence resets affected pending proposals for review and invalidates previous readiness; a running or unknown-outcome task is never silently relaunched. Any program research revision change prevents claiming or completing stale work. Refresh planning before continuing pending tasks; preserve dependencies and operator ordering/defer choices. No automatic retries occur after expired leases.

## Queue operations

Every JSON operation requires `session_id`. Program/campaign/principal come from the authenticated local transport/session. Mutations require `task_id` and `expected_revision`; stale writers receive `context_conflict`. The local OS user remains the principal, as in Phase 2; these controls are not a production isolation boundary.

| Operation | Additional fields | Behavior |
|---|---|---|
| `queue.list` | none | Return durable tasks in operator position/score order; recover expired leases as blocked unknown outcomes. |
| `queue.history` | task_id | Read immutable events, including dispositions and ordering changes. |
| `queue.transition` | task_id, expected_revision, state, reason | proposed → ready → awaiting_approval; proposed/ready/awaiting_approval can become blocked/closed/cancelled; blocked can return to proposed when outcome is known. |
| `queue.dependencies` | task_id, expected_revision, dependencies, reason | Set existing same-campaign dependencies before readiness; reject unknown references and cycles. |
| `queue.claim` | task_id, expected_revision, optional ttl | Atomically lease ready offline analysis after dependency, freshness and defer checks. |
| `queue.heartbeat` | task_id, expected_revision, lease_token, optional ttl | Extend the owning session's lease and return its new revision. |
| `queue.stop` | task_id, expected_revision, lease_token, state, reason | Owning session stops offline analysis as blocked/cancelled, without minting a result. |
| `queue.complete` | task_id, expected_revision, lease_token, result, verdict | Commit investigation, attempted coverage, optional negative result and queue completion in one SQLite transaction. |

Lease TTL defaults to 30 seconds and must be 1–300 seconds. Use the returned revision after every mutation/heartbeat. Leases bind actor, session and token; provider switching preserves task identity but cannot steal a live lease. Expiry becomes blocked with an unknown outcome and requires an operator disposition before replacement work. Dependencies must be investigated, closed or cancelled before readiness. A task's `investigated` execution state remains separate from `hypothesis_verdict`: supported, rejected, inconclusive or needs_review. Queue records also appear in provider-switch handoffs. All results remain agent-unreviewed; neither supported nor rejected means independently verified. Expected-artifact descriptions and stop conditions guide analysis; stored investigation/coverage records satisfy the Phase 4 result-record obligation. Class-specific artifact qualification and independent verification belong to Phase 5.

Operator controls are separate CLI commands, unavailable as JSON operations:

```sh
bin/bb-agent --data-root /absolute/private/root queue-adjust --session-id SESSION_ID --task-id TASK_ID --expected-revision 1 --position 0 --deferred --reason 'Wait for application documentation'
bin/bb-agent --data-root /absolute/private/root queue-adjust --session-id SESSION_ID --task-id TASK_ID --expected-revision 2 --position 0 --no-deferred --reason 'Documentation is available'
bin/bb-agent --data-root /absolute/private/root queue-dispose --session-id SESSION_ID --task-id TASK_ID --expected-revision 3 --reason 'Close unresolved work without a confirmed finding'
```

Disposition closes work without asserting a successful remote outcome, human review or reportability. It cannot dispose a live lease. Events preserve the previous task and rationale. CLI separation records operator intent in local mode; an unrestricted shell can still invoke the CLI. External separation remains a Phase 6 prerequisite.

## Campaign completion

`campaign.evaluate` accepts only `session_id`. It derives obligations from the campaign queue and research provenance: nonterminal tasks, inconclusive/needs-review verdicts, missing result records and unresolved hypotheses/questions block completion. An operator disposition closes an obligation without approving a finding. Required-review questions cannot be discharged by an agent closing a related task; they require an operator disposition or the later independent review integration. Restricted/unsafe hypothesis/question records block completion without exposing their contents. A model cannot supply a complete flag or a fabricated resolution status.

The legacy campaign runner also records required phases/workflows before execution and checks successful persisted runs plus nonempty declared output files. Missing or unresolved output descriptors block completion. `completion.json`, `status.json` and FSM history preserve the assessment and remaining obligations. Outcomes are `completed`, `blocked`, `budget_exhausted`, `operator_stopped` and `previewed` for dry runs; unexpected execution exceptions retain `failed`. These replace the previous successful `done` phase and budget-as-interrupted classification. Completion never authorizes continuation beyond a budget or operator stop.

The existing `bb-hunt` technique sweep remains a legacy execution path; Phase 4 does not make it a production security boundary or enable active research dispatch through `bb-agent`. Production egress/approval enforcement and independent review are still later prerequisites.

## Storage and acceptance

Memory migration 3 adds immutable plans, mutable queue projections and append-only queue events to the existing SQLite store. Consistent `.pre-v3-*.sqlite` backups precede upgrading older stores. Fact IDs, corrections and research events remain intact; backup/restore uses the existing operator tools. No target responses, credentials or runtime databases enter commits.

Local synthetic acceptance covers architecture-first questions, application-specific boundary questions, explainable deterministic ranking, negative-result suppression and justified reopening, cross-campaign deduplication, dependencies/cycles, competing claims, provider switching, lease expiry, stale assumptions, transactional result rollback, migration and private JSON/Markdown output. A pending required review defeats a completion claim; budgets and operator stops preserve outstanding obligations without further execution.
