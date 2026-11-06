# Agent compatibility implementation plan

Status: Phases 1-6 implemented as local/offline increments. Phase 6 adds the scope parser, operator approval grants, aggregate budgets, hash-chained audit and the exact-report-hash submission gate; `dispatch.authorize` is a tested per-request decision boundary but no constrained worker executes yet, so production active dispatch still requires deployed isolation. Phase 7 (offline matrix, benchmarks, CI) and Phase 8 (docs) remain planned. See [the phase ledger](docs/AGENT_AGNOSTIC_PHASE_LEDGER.md), [Phase 6 enforcement](docs/phase6-enforcement.md) and read ARCHITECTURE_AUDIT.md first.

## Design decision

Keep the CLI and `.claude/skills` packages. Extend existing adapters, memory, planner, campaign and verifier modules. Use Python standard library and SQLite where practical; no new agent framework or mandatory model provider. Agents propose tasks and interpret evidence; a common harness service validates requests and owns execution policy.

Separate three authorities: operator-owned policy and approval service, constrained execution worker, and agent research client. Repository instructions assist the client but cannot enforce security against that client's unrestricted shell. Production research must run with policy/code mounted read-only, restricted credentials, private writable data storage and network egress controlled outside the agent. Coding mode can edit source, but must not have production research credentials/network authority. If isolation is unavailable, allow offline analysis only.

Preserve command names and documented arguments. Explicitly document the intentional security behavior change: previously accepted unsafe executions will fail closed. Backward compatibility must not preserve bypasses.

## Shared contract (proposed, not available commands)

Expose provider-neutral operations for capabilities, skill/workflow discovery, session open/resume, task dispatch/status/cancel, memory query/append/correct, queue listing and review recording. CLI JSON is the first transport; optional project-local stdio MCP wraps the exact same operations. Never expose arbitrary shell, environment injection, policy editing or human approval issuance through MCP.

Task request fields: schema_version, task_id, idempotency_key, program_id, campaign_id, session_id, agent/provider identity, skill, workflow, typed inputs, expected context revision, policy/scope digest and approval reference. The service resolves identity and validates authority; client labels are not authentication.

Results: schema_version, task_id/run_id, status, exit_code, typed error, artifact IDs/hashes, redacted summary, start/end timestamps, request-budget usage and provenance. Statuses distinguish queued, awaiting_approval, running, succeeded, failed, denied, interrupted and cancelled. Errors distinguish invalid input, scope denial, approval missing/expired, budget exhausted, missing tool, conflict, timeout and internal failure. Completion of a task never implies vulnerability confirmation.

Discovery returns workflow version/hash, input schema, side effects, effective safety tier, prerequisites and available tools. Unknown workflows, fields or capabilities fail closed. Keep vulnerability algorithms inside skills; share generic enforcement once below every adapter.

## Ordered delivery and acceptance gates

Each phase gets focused commits and tests, then a GitHub push to the configured fork. No private state or evidence enters commits. Phase 1 stops for approval. Later phases should remain independently reviewable; tests and documentation accompany each change rather than waiting until the end.

| Order | Phase / scope | Acceptance gate |
|---|---|---|
| 1 | Phase 2a: shared discovery/task schemas; repair adapter source/ownership handling; offline-only dispatch initially | Identical canonical workflow IDs across three adapters; malformed input rejected; hand edits preserved; existing CLI discovery still works. |
| 2 | Phase 6 prerequisite: scope parser and external approval/execution boundary before enabling active dispatch | Documented exclusions denied; no target traffic without explicit scope and operator approval; raw worker egress blocked; model cannot change restrictions. |
| 3 | Phase 2b: Codex/OpenCode/Claude session adapters; optional MCP | Same task/result contract; provider switch resumes same program/campaign; permission defaults deny unrestricted execution. Native clients tested at recorded versions. |
| 4 | Phase 3: extend persistent memory and campaign binding; task-specific context packs and reusable-lesson provenance | Existing database migrates without loss; corrections retain history; concurrent stale writes rejected; restart and cross-program isolation tested; bounded context retains counterevidence and source revisions; unreviewed lessons cannot become shared techniques or skills. |
| 5 | Phase 4: research planning, durable queue and campaign completion gate | Application-specific questions with evidence and rationale; tested-clean history suppresses repetition; missing understanding produces research tasks, not automatic scans; pending obligations prevent completion and survive stopped runs. |
| 6 | Phase 5: independent review, evidence gate and explicit reportability assessment | Unsupported synthetic claims never confirmed; reviewer differs from author; evidence tampering/missing artifacts blocked; validity, reachability, deployment relevance, impact and program eligibility assessed separately. |
| 7 | Phase 6 completion: aggregate budgets, secret boundaries, audit recovery, submission gate | Budgets survive restarts/concurrency; no destructive or extraction paths; report submission requires separate human grant bound to exact report hash. |
| 8 | Phase 7: full offline/local-fixture matrix, research-quality benchmarks and CI | All acceptance cases below pass; validator failures yield nonzero status; existing checks retained; repeated research evaluations compare quality and resource use against a pinned baseline. |
| 9 | Phase 8: setup/architecture/workflow/security/contribution docs | Exact commands tested in clean environments; limitations and platform/version matrix explicit. |

This ordering pulls minimum Phase 6 controls forward because exposing the existing unrestricted runner through new adapters first would violate the requested security model. Phase 2 can ship discovery and offline analysis before active execution is enabled.

## Provider adapters

Codex: keep root AGENTS.md canonical; generate minimal skill entry points linking to shared packages, plus explicit permitted-tool configuration. Do not assume the legacy `.codex/instructions.md` is automatically loaded.

OpenCode: reuse shared instructions; provide project agent definitions for research and independent review with explicit permissions. Never overwrite root AGENTS.md to generate provider output.

Claude Code: preserve CLAUDE.md import and `.claude/skills`; apply the same restricted dispatch contract. Existing security workflows remain discoverable, but unsupported unsafe operations are denied.

Future providers implement only transport, instruction discovery and capability negotiation. Before implementation, verify current native configuration formats against official provider documentation and installed clients. This audit does not claim native setup compatibility or introduce untested configuration syntax.

## Persistent research model

Extend the existing SQLite store with versioned migrations and append-only research events; keep source artifact links to asset graph, traffic corpus and coverage records. Store program policies/scope versions, assets, architecture/components, technologies/integrations, roles/permissions, functionality, observations, hypotheses, investigations, negative outcomes, coverage, evidence references, questions and queue tasks.

Every event records immutable ID, program/campaign/session/task, actor/provider, timestamp, source artifact hash, trust classification, sensitivity and supersedes/revision relationship. Corrections append history and compare expected revision transactionally; no silent replacement. Agent assertions remain unreviewed. Human review is a separately authenticated event, not a caller-controlled boolean.

Use program-bound queries for get/correct as well as search. Re-scan permitted export fields and retain sensitivity through corrections. Do not store credentials as research facts; store opaque credential handles. Separate operator-authored scope authority from target-derived scope notes.

Move toward a configurable private data root outside source, preserving legacy `.bb` through explicit migration and compatibility reads. Back up SQLite consistently, record schema version, test rollback/restore. Campaigns own immutable context snapshots, scope hashes and output roots. Sessions attach to campaigns; task leases/idempotency prevent duplicates. Interrupted operations require reconciliation before retry, especially when remote effects are uncertain.

Phase 3 adds task-specific context packs alongside session handoffs. Assemble bounded relevant facts, counterevidence, negative results, prerequisites and methodology for each investigation. Record source IDs/revisions, selection rules and omitted material so context can be rebuilt and stale assumptions identified. Packs are derived views; durable records remain authoritative, with trust and sensitivity preserved.

Separate program observations from reusable technique candidates. Store sanitized provenance, applicability conditions, supporting outcomes and counterexamples without copying private program evidence into shared knowledge. Reuse `skill-scientist` for evaluation and require recorded review before promotion into `technique-kb` or a skill. Recording a success never automatically changes methodology or executable workflows.

## Planning workflow

Extend planner skill with a memory-first research workflow. Read application map, role boundaries, APIs, invariants, coverage, previous hypotheses, evidence and negative results. Emit question, application-specific hypothesis, supporting and contradicting facts, alternative explanations, smallest permitted next step, expected evidence, stop condition and dependencies.

Score potential impact, evidence confidence, information gained and cost explicitly; store score components and explanations. Prefer authorization boundaries, business logic and API behavior. Deduplicate by program/surface/hypothesis; reopen negative investigations only for changed evidence or expiry with a recorded reason. Queue transitions are proposed → ready → awaiting approval → running → investigated/blocked/closed. A plan never authorizes execution.

Phase 4 adds a deterministic campaign completion gate inside the campaign skill. Check required tasks, unresolved hypotheses, pending reviews and expected artifacts before accepting completion; the model may propose completion but cannot establish it. Distinguish completed, budget-exhausted, blocked and operator-stopped campaign outcomes from individual task statuses. Retain unfinished obligations with reasons and resume prerequisites. Completion checks must honor budgets and stops; they do not grant permission to continue execution.

## Independent verification

Add a verifier review workflow over an immutable evidence bundle. Separate author/reviewer sessions, read-only evidence permissions, recorded identity and conflict checks. A second agent reviews original hypothesis, provenance, alternative explanations, actual impact, duplicate candidates, scope and program policy; records accept-for-human-review, needs-evidence, reject or possible-duplicate with reasons.

Require artifact existence, nonempty content, checksums, request/response association, timestamps, tool versions and reproduction instructions. Preserve raw evidence privately; provide redacted review views. Evidence completeness is necessary but not proof of impact. Remove label-only confirmation and do not allow AI verdicts or scanner flags alone to set confirmed status. Human review and submission authorization remain distinct.

Phase 5 records reportability separately from technical validity: realistic reachability, relevance to the deployed application, demonstrated impact and program eligibility each need evidence and a rationale. Missing information remains unknown rather than becoming a positive assessment. A reproducible technical result alone does not establish report readiness, and reportability never supplies submission approval.

## Execution security contract

Default offline/read-only research. Explicit scope with exact hosts, schemes, ports, path rules and exclusion precedence; no inferred wildcards or target-name authorization. Check every request and redirect, DNS/IP resolution and alternate protocols at an external boundary. Unsupported tools fail closed instead of bypassing the boundary.

Active approval binds program, scope/policy/workflow hashes, exact task/input digest, time expiry and request budget. Operator service issues/revokes grants; agents can request but cannot mint them. Deny destructive testing, data modification and extraction regardless of approval. GET alone does not establish safety: allow only reviewed operations with constrained response size and fixture/test-account data where appropriate.

Shared rate/concurrency/request budgets cover tools, retries and redirects; persist consumption transactionally. Kill process groups on cancellation/time limits. Log proposed, denied, approved, started, completed and interrupted events with redaction before persistence; no raw secrets in argv/logs. Policy and audit storage must be outside agent write authority. Treat all target text and imported memory as untrusted data, never executable commands or authority changes. Regex scanning is supplementary only.

## Test matrix

Use temporary data roots, mocks and local deliberately vulnerable fixtures; deny public network in CI tests.

- Codex/OpenCode/Claude: native instruction discovery, permission behavior, stable catalog, transport parity; record versions and skip reasons honestly when clients unavailable.
- Memory: migration, provenance, program isolation, corrections, conflicting writers, sensitivity and synthetic secret rejection/export; bounded context selection preserves counterevidence, records omissions and detects stale source revisions; unreviewed or unsanitized lessons cannot enter shared techniques/skills.
- Campaigns: agent switch, context binding, crash between start/result, partial JSONL recovery, leases and idempotency; a model declares completion while a required review is pending, the campaign rejects completion and preserves the obligation; budget-exhausted, blocked and operator-stopped runs retain unfinished work without bypassing execution limits.
- Scope/approval: documented comment headings, exclusions, exact/wildcard domains, URL prefix attacks, userinfo, IPv6, CIDR, redirect/DNS changes, revoked/expired/replayed grants and modified task hashes.
- Budgets: concurrent requests, retries, restart accounting and cancellation descendants.
- Evidence/review: fabricated labels, missing/empty/tampered artifacts, wrong program, self-review, alternate explanations, duplicates and report-hash approval invalidation; reproducible findings with unknown reachability, deployment relevance, impact or eligibility do not become report-ready automatically.
- Secrets/prompt injection: synthetic tokens in content/metadata/correction reason/logs; malicious target instructions cannot alter policy or authorize tools.
- Regression: existing CLI help/discovery, make test, validator, security/workflow/release audits and secret scanner. Convert known failures into explicit tracked regressions rather than silently weakening gates.

Phase 7 reuses `evaluation-harness` for repeated research-quality benchmarks on pinned local fixtures, including known vulnerabilities and negative controls. Measure supported findings, false positives, repeated work, request counts, model usage/cost where available and elapsed time. Record fixture, workflow, prompt, model/client and context-selection versions; compare planner, prompt and context changes against a pinned baseline under equivalent budgets. Report variation across runs and unavailable measurements explicitly. Set acceptance thresholds before evaluating a candidate change; convincing prose or a single successful run is not improvement evidence.

## Documentation deliverables

CODEX_SETUP.md, OPENCODE_SETUP.md and CLAUDE_SETUP.md: exact tested setup commands, supported versions, permissions, optional MCP, offline smoke test and session resume.
AGENT_ARCHITECTURE.md: contract, authority boundaries, ownership and migrations.
RESEARCH_WORKFLOW.md: program onboarding, memory, planning, approvals, independent review and report preparation.
SECURITY_MODEL.md: enforcement, deployment isolation, secret handling, denied operations and residual risks.
CONTRIBUTING.md: local-only tests, schema migration policy, adapter contract tests and evidence prohibition.

## Phase 1 exit

Audit and plan are reviewable documentation only. Approval is required before implementing Phase 2, as explicitly requested in the project brief.
