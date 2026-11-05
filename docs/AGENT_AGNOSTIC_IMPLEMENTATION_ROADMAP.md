# Agent-agnostic research environment: implementation roadmap

Status: Phase 1 documentation and Phase 3 local persistence/recovery implementation are complete. Phase 2 offline contracts/adapters are implemented, with full native-client acceptance still limited as recorded in the [phase ledger](AGENT_AGNOSTIC_PHASE_LEDGER.md). Phase 4 onward remains planned; active production dispatch still requires the Phase 6 external enforcement prerequisite. The user authorized Phase 2 and Phase 3 implementation in this session. This roadmap does not itself authorize target testing. It expands [the compatibility plan](../AGENT_COMPATIBILITY_PLAN.md) and addresses findings A1–A12 in [the architecture audit](../ARCHITECTURE_AUDIT.md).

## 1. Intended outcome and constraints

One authorized program has one durable research history, usable across Codex, OpenCode, Claude Code and future clients. A new session can recover application understanding, evidence, previous investigations, negative results, open questions and the next useful task without relying on chat history.

Preserve existing command names, arguments and `.claude/skills` packages. Add research logic inside skills; keep CLI wrappers thin. Share execution controls across all entry points. Prefer standard-library Python, SQLite and existing modules over frameworks. Do not introduce model-provider dependencies into the core.

Backward compatibility covers interfaces and stored data, not permission bypasses: unsafe commands previously accepted must become explicit denials. Production research must prohibit destructive testing, data modification and data extraction. Active security testing and report submission require separate human approvals.

### Implementation sequence

1. Phase 2a: discovery, contracts, safe adapter generation and offline dispatch.
2. Phase 6 prerequisite: scope and approval enforcement outside model control, before enabling any active dispatch.
3. Phase 2b: native client integrations and session switching.
4. Phase 3: persistent research model and reliable campaign recovery.
5. Phase 4: research planner and durable queue.
6. Phase 5: independent review and evidence validation.
7. Phase 6 completion: budgets, audit durability, credential isolation and submission gates.
8. Phase 7: integrated acceptance suite and CI enforcement.
9. Phase 8: tested setup and operational documentation.

Tests and documentation accompany every increment. Each phase receives focused commits and a GitHub push to the configured fork. Do not combine the work into one large rewrite.

## 2. Component ownership and proposed file layout

Paths below are design targets, not existing interfaces. Confirm names against repository conventions before implementation.

| Existing location | Planned responsibility |
|---|---|
| `bin/bb-init`, `bin/bb-validate` | Select and validate program/campaign context through common helpers; retain old CLI syntax. |
| `bin/bb-run` | Dispatch selected registered workflow through common validation and execution controls. |
| `bin/bb-hunt` | Continue delegating orchestration to campaign skill. |
| `bin/bb-tools` | Keep registry-based lifecycle; never give research clients installation privileges. |
| `tools/adapters/` | Render provider entry points from shared instructions and workflow catalog. |
| `tools/schemas/` | Versioned discovery, task, result, session, policy, evidence and review schemas. |
| Proposed `tools/agent_interface/` | Catalog, task validation, session operations, result serialization and optional MCP transport. |
| Proposed `tools/execution_control/` | Shared generic authorization, request budgets, execution lifecycle and audit integration. |
| `.claude/skills/program-memory/` | Research events, projections, migrations, queries, corrections and summaries. |
| `.claude/skills/campaign/` | Campaign context binding, task recovery and orchestration. |
| `.claude/skills/planner/` | Application questions, research opportunities, prioritization and queue transitions. |
| `.claude/skills/scope-manager/` | Canonical scope parsing and semantic membership decisions, reused by enforcement. |
| `.claude/skills/impact-verifier/` | Evidence qualification and independent review records. |
| `.claude/skills/reporting/` | Draft generation, redacted exports and report readiness. |
| Proposed `tests/` | Unit, contract, integration, recovery and local-fixture tests. |

The execution service may import scope semantics but must run from operator-controlled code. Sharing the implementation does not mean sharing write permissions. No provider adapter implements its own scope rules or verification logic.

## 3. Phase 2 — provider-neutral compatibility

### 3.1 Canonical discovery

Build one catalog reader over `.claude/skills/*/skill.yaml`. Return stable skill/workflow IDs, package version, command-content digest, description, typed inputs, output descriptors, effective safety tier, authentication requirements, side effects, tool prerequisites and supported execution modes.

Normalize legacy input lists and string output paths without changing the YAML files en masse. Add optional structured input metadata incrementally. Mark unknown side effects or incomplete security metadata as unavailable for active dispatch. A catalog entry is not permission to run it.

Capability discovery reports protocol versions, available operations, supported providers/transports, isolation availability, local tool status and limitations. Report unavailable capabilities explicitly. Cache only against package/registry digests; invalidate after changes.

### 3.2 Task and result contract

Use versioned JSON schemas. Reject unknown fields where they could influence execution. Limit message sizes and nesting. Resolve program/session ownership server-side.

| Task field | Meaning and validation |
|---|---|
| `schema_version` | Supported protocol version; incompatible versions rejected. |
| `task_id`, `idempotency_key` | Stable identity; replay with changed payload is a conflict. |
| `program_id`, `campaign_id`, `session_id` | Existing records with consistent ownership and program binding. |
| `skill`, `workflow` | Exact catalog identifiers; never filesystem paths or shell snippets. |
| `inputs` | Schema-validated values, bounded strings and approved artifact references. |
| `expected_context_revision` | Optimistic concurrency guard against stale campaign context. |
| `scope_digest`, `policy_digest`, `workflow_digest` | Bind execution to reviewed versions. |
| `approval_ref` | Opaque operator-issued grant reference; absent for offline tasks. |

Provider/model labels are provenance, not authentication. Tool handlers receive a service-established principal and allowed capabilities separately from request JSON.

Return task/run IDs, lifecycle status, exit code, typed error, safe diagnostic, timestamps, consumed budgets, provenance and artifact references with hashes. Keep stdout/stderr in bounded redacted artifacts, not an unbounded JSON response.

Lifecycle: `queued`, `awaiting_approval`, `running`, `succeeded`, `failed`, `denied`, `interrupted`, `cancelled`. Errors: invalid input, unsupported version/capability, missing tool, scope denial, missing/expired/revoked approval, budget exhaustion, context conflict, timeout and internal failure. Successful execution never sets vulnerability status.

### 3.3 Dispatch and transport

Provide a thin JSON CLI entry point first; choose its exact name during implementation. It delegates to existing skill workflows through the common execution service. Add optional project-local stdio MCP only when the CLI contract is stable.

Expose narrow operations: discover capabilities/skills, open/resume session, submit/status/cancel task, query/append/correct research, list queue and record review. Do not expose arbitrary shell, arbitrary environment variables, unrestricted file reads, policy edits or grant issuance. Resolve artifact IDs inside the authorized program root; reject traversal and symlink escapes.

For legacy shell workflows, pass typed values as a controlled environment or argument vector without textual command substitution. Treat registered shell code as trusted executable code, freeze its digest for an approval, and execute it inside the constrained worker. Migrate workflows to structured argv only where needed; do not rewrite every skill.

### 3.4 Adapter generation

Fix `render_adapter.py` to use root AGENTS.md as canonical shared instructions and YAML as the executable catalog. Preserve CLAUDE.md's import. Never generate OpenCode content over canonical AGENTS.md.

Render only provider-specific entry points. Preview diffs; track ownership and hashes; refuse to overwrite manually edited generated blocks unless the operator explicitly resolves the conflict. Preserve unrelated text. Use atomic writes and unique backups; make repeated generation idempotent. Test install, regenerate, conflict and uninstall behavior.

Codex: root instructions, minimal discoverable skill entry points and explicitly permitted tools. OpenCode: shared instructions, research/reviewer agent definitions and permissions. Claude Code: preserve `.claude/skills` and CLAUDE.md, route execution through the same controls. Future providers register a manifest and pass the contract tests.

Verify current configuration syntax against official documentation and actual installed client versions during implementation. Do not publish guessed setup files or claim native compatibility from template generation alone.

### 3.5 Sessions and handoff

Opening a session attaches a new session ID to an existing program/campaign; it does not overwrite the active program globally. Store provider/client version, principal, creation/last-seen time, context revision and predecessor session ID.

Generate a bounded handoff containing scope/policy references, application summary, relevant facts, recent decisions, open hypotheses, negative results, pending tasks, active leases and evidence links. Summaries retain source IDs and sensitivity; they are derived views, not the authoritative record. A provider switch must retain task identity and approval restrictions.

Phase 2 acceptance: identical catalog and results through all adapters, stale context rejected, no instruction overwrite, no arbitrary command escape, offline smoke tests pass, native integration limitations recorded.

## 4. Phase 3 — persistent program knowledge

### 4.1 Storage and migration

Extend the existing SQLite memory store rather than replacing it. Add numbered transactional migrations and a schema-version table. Back up with SQLite's consistent backup API; test restoring the previous version. Preserve original fact IDs and correction links.

Introduce a configurable private data root outside the repository. Keep legacy `.bb` readable through an explicit migration path; do not silently move or delete it. Separate agent-writable research data from operator-owned policy, credentials and authoritative audit storage.

Suggested private layout: program database and artifact directory per program, campaign context/status under that program, and session handoffs referencing shared records. Use restrictive permissions and operator-managed backup/encryption. Paths are implementation choices; all commands must use the resolved data-root abstraction.

### 4.2 Data entities

| Entity | Required information |
|---|---|
| Program | Stable ID, display name, policy references, restrictions, scope versions and review dates. |
| Asset | Canonical identity and asset-graph reference, ownership evidence, scope status and last seen. |
| Component | Application/service, responsibilities, trust boundaries, data flows and linked assets. |
| Technology/integration | Product/version confidence, source, external dependency and affected components. |
| Role/permission | Persona reference, permitted actions, ownership/tenant boundaries and observed exceptions. |
| Functionality | User journey, state transitions, APIs, expected invariants and unknown behavior. |
| Observation/note | Exact claim, context, source, confidence, trust and sensitivity. |
| Hypothesis | Research question, mechanism, preconditions, predicted result, supporting/contradicting facts. |
| Investigation | Hypothesis, tasks, methods, persona handles, results, limitations and conclusion. |
| Negative result | Tested surface/version, method, conditions, evidence, confidence and expiry/retest trigger. |
| Coverage | Surface × role × action × boundary and method; distinguish unknown, attempted and adequately investigated. |
| Evidence | Immutable ID, hash, type, size, capture metadata, tool version and redacted derivative links. |
| Open question | Missing knowledge, dependencies, information value and resolution criteria. |
| Queue task | Linked question/hypothesis, rationale, score, prerequisites, state and lease. |

Reference existing asset graph, traffic corpus and coverage records rather than duplicating their entire contents. Program scope notes from an agent cannot change authoritative scope.

### 4.3 Events, corrections and provenance

Use append-only events plus transactionally updated query projections. Events include ID, program/campaign/session/task/run IDs, actor, provider/client version, timestamp, entity/revision, event type, source artifact IDs/hashes, confidence, sensitivity and trust classification.

Corrections require the expected revision and reason; append supersession links and retain old content. Concurrent stale corrections return conflicts. Invalid correction targets and cross-program references fail. Human review is an authenticated operator event, never `--reviewed` supplied by an agent.

Validate and secret-scan every string-bearing field, including correction reason and source metadata. Remove unrestricted secret/isolation overrides from the agent-facing API. References to credentials use opaque handles only. Export rechecks content and permissions instead of trusting a report-safe label.

### 4.4 Queries and restart continuity

Provide program-bound retrieval, filtered search, history, contradiction candidates, expired-fact review and compact handoffs. Include source IDs in summaries. Distinguish observed facts, model inference and human conclusions. Contradiction detection proposes review; it does not automatically merge facts.

Persist campaign-owned context snapshots, not just a pointer to global context.env. Bind resume to target, program, policy and scope versions; require reconciliation on changes. Use atomic state writes, task leases, heartbeats and idempotency keys. Preserve interrupted/failed states instead of labeling every finally block done.

A crashed task whose remote outcome is unknown must not auto-retry. Reconcile available artifacts and audit events, then request a new decision if needed. Concurrent sessions must not share a mutable cookie jar or overwrite output directories.

Phase 3 acceptance: migration preserves data, restart/switch restores research, stale writers conflict, program boundaries hold, secrets remain absent, and backup/restore reproduces the same history.

## 5. Phase 4 — research planning workflow

### 5.1 Inputs and stages

Add a workflow to the planner skill that reads current knowledge, application/role maps, traffic routes, evidence, coverage, prior investigations and existing queue. Pin input revisions in each generated plan.

Stages: identify missing application understanding; find unexplained boundary behavior; review hypotheses and counterevidence; generate focused questions; propose the smallest informative investigation; rank and deduplicate; persist queue proposals.

Prefer questions about object ownership, tenant/role isolation, business invariants, state transitions and application-specific API behavior. When architecture is unknown, propose understanding tasks before probes. Existing technique-kb matching can suggest methods but must not turn every planning cycle into another generic scan.

### 5.2 Opportunity record and ranking

Each opportunity stores question, hypothesis, affected surface/roles, potential impact, evidence quality, supporting and contradicting references, alternative explanations, expected information gain, cost, prerequisites, allowed method, expected artifacts and a stop condition.

Use configurable normalized weights for impact, evidence quality, research value and effort. Store each component and a plain-language rationale. Safety eligibility is a hard filter, not a score that high impact can outweigh. Low-confidence high-impact ideas may be worth understanding, but remain hypotheses.

Deduplicate on program, surface, boundary and hypothesis identity. Compare against negative results and prior task inputs. Reopen only for changed application facts, expired evidence or a substantially different method; record why. Do not let model text set final scores or queue state without validation.

### 5.3 Queue behavior

Transitions: proposed → ready → awaiting approval → running → investigated; also blocked, closed and cancelled with reasons. Keep task execution status separate from hypothesis verdict. Dependencies must be acyclic; leases prevent two agents taking the same task. Completion appends investigation results and coverage updates transactionally.

Planner outputs both JSON and a readable review document. Operator can reorder/defer work without rewriting history. New evidence invalidates stale plan assumptions. Planning never grants active-test permission.

Phase 4 acceptance: a local synthetic application produces explainable boundary-specific questions, missing information creates understanding tasks, negative results prevent repetition, and identical input revisions produce stable deterministic ranking given the same proposed questions.

## 6. Phase 5 — independent evidence review

### 6.1 Fix current verification semantics

Replace impact-class matching as confirmation. Classification remains a descriptive label. Existing verified files must be imported as legacy unreviewed claims unless independently qualified; never silently trust old statuses. Preserve them for provenance.

Separate candidate, evidence-qualified, independently reviewed, human-confirmed and submission-approved states. No scanner output or AI verdict can independently reach human-confirmed.

### 6.2 Evidence bundle

Create a manifest with program/campaign/hypothesis IDs, source run IDs, scope/policy versions, capture times, tool versions, artifact hashes/sizes/types, request/response associations, reproduction instructions and redaction metadata. Verify containment, existence, nonempty contents and hash agreement. Reject unexpected executable artifacts from automatic execution.

Require evidence appropriate to the claim; mark missing screenshot or other artifacts explicitly instead of pretending every class needs identical proof. Any waived evidence requirement needs an explicit review rationale. Formatting completeness cannot establish impact.

### 6.3 Reviewer workflow

A separate reviewer session receives a read-only snapshot of the hypothesis and relevant evidence. Bind reviewer principal/session to the review record and reject author self-review. A different model name alone does not establish independence. Record whether review is independent agent analysis or human review.

Require findings on original claim, evidence support, alternative explanations, unsupported impact, reproducibility, scope/policy compliance, likely duplicates and missing evidence. Duplicate suggestions use local prior investigations and disclosed material already permitted for use; do not send private evidence to external services by default.

Verdicts: needs evidence, reject, possible duplicate, or recommend human review. Attach reasons and artifact references to each disputed claim. Reviewers cannot edit original evidence or mint approvals. Additional active testing becomes a separate queued task requiring approval.

Phase 5 acceptance: fabricated labels fail, missing/tampered evidence fails, self-review fails, alternatives/duplicates are retained, and review cannot trigger submission.

## 7. Phase 6 — execution and security controls

### 7.1 Authority separation

Run research clients without direct target network access or write access to policy/executor code. Operator-controlled service owns scope, grants and credential handles. Workers receive only task-scoped access. Use OS/container isolation appropriate to the supported platform; specify and test the chosen deployment before claiming enforcement.

Read-only/offline analysis is the fallback when isolation is unavailable. Do not assume a repository hook, MCP permission prompt or instructions protect against an unrestricted shell. Coding workspaces may edit source but must not also hold production execution authority.

### 7.2 Scope enforcement

Fix documented comment-heading parsing first. Adopt a versioned explicit scope format supporting exact host, explicit wildcard, scheme, port, path boundary, IP/CIDR and exclusions. Exclusions win; no inferred subdomains. Validate structured URLs with standard parsers, reject ambiguity and normalize IDNA/trailing-dot cases deliberately.

Enforce at dispatch and every outbound request. Validate redirects and resolved destination addresses, deny unauthorized private/loopback/link-local addresses except explicit local fixtures, and address DNS rebinding at connection time. Do not broaden scope based on discovered links or target-provided instructions.

Workers must use the enforced egress path. Tools that cannot be constrained are unavailable, not silently routed directly. Scope edits revoke affected grants and force task revalidation.

### 7.3 Human approval and prohibited operations

Approval requests present exact workflow, scope, inputs, anticipated interactions, budget, expiry and evidence handling. Grants bind those digests plus operator identity and allowed task uses. Store grants outside agent write authority. Reject revoked, expired, replayed or mismatched grants.

Human approval is required for active security testing, including active-safe probes. Destructive testing, data extraction and modification remain prohibited even with a grant. A read HTTP method is not proof of harmlessness; allow only reviewed operations and stop on unexpected sensitive data. Collect minimal redacted evidence, never bulk private content.

### 7.4 Rate limits and budgets

Persist program/host/task request counters, concurrency leases and deadlines. Reserve budget atomically before requests; include retries, redirects and child tools. Record attempted sends conservatively across crashes. Honor rate-limit responses and shared backoff. Existing circuit breaker remains a complementary signal.

Support request ceiling, requests per second, concurrency, response byte limit, total evidence byte limit and wall-clock deadline. Cancel process groups and release leases on termination. Unknown unmetered traffic is denied.

### 7.5 Credentials, content and audit

Keep API keys/cookies outside memory and argv. Resolve opaque handles only in authorized workers; restrict host and persona use. Redact headers, cookies, URL tokens, output metadata and exceptions before logging. Inspect exports with synthetic-secret tests and a secret scanner. No raw provider keys in project configs.

Treat all target/imported content as untrusted, including material later retrieved from memory. Preserve trust labels; never interpret data as approval, policy or executable instructions. Use bounded inputs and schema validation. Prompt-injection pattern detection may flag review but cannot replace isolation.

Audit proposal, authorization, denial, start, completion, cancellation, interruption, export and submission. Include actor, policy/workflow/scope hashes, task IDs, artifact references and budget consumption. Persist start before execution; reconstruct unfinished runs after restart. Fail closed when required audit storage is unavailable. Protect authoritative logs from agent mutation; hash chaining alone does not prevent deletion by a writer.

### 7.6 Report submission

Draft generation and local export are separate from submission. Require human-confirmed evidence, review completion and a separate operator grant bound to final report/package hash and destination. Editing the report invalidates approval. Agents cannot submit via alternative browser/network paths. If no submission integration is implemented, provide a reviewed local artifact and label submission as manual.

Phase 6 acceptance: denied operations generate no target traffic, workers cannot bypass egress or mutate restrictions, budgets survive concurrency/restarts, secrets stay out of logs, and submission cannot occur with a testing approval.

## 8. Phase 7 — tests and CI

Use only local fixtures, temporary roots and deliberately vulnerable training applications. Block external egress for integration tests. Do not test against real programs.

| Suite | Required cases |
|---|---|
| Catalog/contracts | Legacy YAML normalization, missing metadata, malformed inputs, version negotiation and transport parity. |
| Provider compatibility | Native instruction discovery, permissions, skill routing, session attach and handoff for Codex/OpenCode/Claude. |
| Adapter generation | Preview stability, idempotency, manual-edit conflict, ownership-safe uninstall and canonical file preservation. |
| Memory | Migration, rollback/restore, append/correct history, concurrent conflicts, sensitivity, provenance and cross-program denial. |
| Campaign recovery | Crash before/after launch, partial result persistence, lease expiry, stale context and provider switch. |
| Scope | Comment headings, exact/wildcard distinction, exclusions, URL prefix confusion, userinfo, ports, IPv6/CIDR, redirects and DNS changes. |
| Approvals | Missing/expired/revoked grant, replay, task digest change, scope change, self-issued approval and prohibited operation. |
| Budgets | Parallel reservations, retries, redirects, restart accounting, child processes and cancellation. |
| Planning | Application-specific questions, score explanations, unknown architecture, negative-result suppression and reopened-task rationale. |
| Evidence/review | Empty/missing/tampered evidence, unsupported impact, legacy confirmation, author self-review, duplicate and alternative explanation. |
| Secrets/injection | Synthetic tokens in every field/log/export; malicious target text cannot change grants, scope or command selection. |
| Submission | Wrong destination/hash, changed report, reused approval and separate testing/submission authorities. |

Retain existing make test and validators, but make semantic failures produce failing exit codes. Track audit baseline failures explicitly and repair them without lowering quality thresholds. Add behavioral suites to CI and distinguish required tests from unavailable native-client checks. Record client/runtime versions; a skipped native test is not compatibility proof. Run relevant tests after each significant change, then the complete gate before each phase is declared complete.

## 9. Phase 8 — documentation and setup

Create CODEX_SETUP.md, OPENCODE_SETUP.md and CLAUDE_SETUP.md with exact commands tested against recorded versions, project configuration, permissions, optional MCP, offline smoke tests, session switching and troubleshooting. Never present proposed command syntax as executable instructions.

AGENT_ARCHITECTURE.md documents contracts, component ownership, trust boundaries, data paths, schema versioning and failure handling. RESEARCH_WORKFLOW.md walks through authorized onboarding, application modeling, notes, hypotheses, planning, approval, investigation, independent review and report preparation. SECURITY_MODEL.md documents isolation deployment, scope semantics, budgets, credentials, prompt injection, audit and residual limitations. Update CONTRIBUTING.md with local-only fixtures, migration policy, adapter ownership rules and required gates.

Document every new option in one configuration reference. Proposed options include private data root, protocol version, default offline mode, policy path, worker isolation backend, approval expiry, rate/concurrency/request/byte/time budgets, audit location, credential backend and provider transport. Decide exact names and safe defaults during implementation; reject unsafe/malformed configuration. Include precedence and whether an option is operator-only or agent-readable.

## 10. Completion checklist and handoff to future agents

Before beginning a phase, read this roadmap, audit findings, current source and applicable repository instructions. Verify which items have already shipped; do not infer implementation from this design document. Keep a phase ledger listing commits, tests, migrations, known limitations and remaining items.

For each phase:

- Implement only its agreed scope plus necessary security prerequisites.
- Preserve working interfaces and explain intentional security behavior changes.
- Add meaningful local tests and run existing applicable gates.
- Record tested behavior and failures accurately; never promote AI claims to confirmed vulnerabilities.
- Keep runtime data, evidence and credentials out of Git.
- Commit a reviewable increment and push to the configured fork when authorized.
- Update documentation and the ledger; stop at any requested approval boundary.

Overall completion requires a demonstrated local-only scenario: create a program and scope, build application knowledge, record a hypothesis and a negative investigation, switch agents, recover after interruption, propose a justified next task, enforce approval/budgets, reject an unsupported finding, independently review evidence, and produce a human-reviewed report artifact without leaking secrets or accessing unapproved destinations.
