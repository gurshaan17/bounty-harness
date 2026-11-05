# Phase 1 architecture audit

Audit date: 2026-11-05. Baseline: `d594684bbbed52e2e0f894363b93ebbe7d9cba21`.
Working tree was clean. Configured origin is `gurshaan17/bounty-harness`, not the upstream URL in the brief. No target campaign or third-party security testing was performed. This audit changes documentation only.

## Assessment

Extend the existing harness. Its workflow registry, program-memory SQLite store, asset graph, planner and trace system are useful foundations. It is already callable by any shell-capable agent. However, its current controls do **not** meet the requested mandatory security model, and its impact verifier does not establish impact. Agent adapters alone cannot fix these problems.

## Architecture and inspection coverage

Read root README, AGENTS, CLAUDE, contributing/security documentation, docs/adapters.md, docs/threat-model.md, and the four docs/research digests. Research digests are historical design inputs, not independently verified external claims. Inspected all six bin files, skill schema and package structure, adapter implementation, and scope/campaign/memory/impact/safety workflows and relevant implementations. This is a focused architectural audit, not an exhaustive review of every vulnerability payload or installed external tool.

| Component | Actual responsibility and extension seam |
|---|---|
| `bin/bb-init` | Writes repo-global `.bb/context.env` and JSON; creates output/evidence directories. Shell values use `%q`; JSON uses unescaped heredoc interpolation. Infers some program/scope defaults from target strings. |
| `bin/bb-validate` | Sources context; requires fields and scope-file existence; parses registry/lock metadata. Does not validate target membership or authorization. |
| `bin/bb-run` | Discovers YAML skills, loads context, resolves workflow shell command, warns about tools/scope, substitutes variables, checks circuit breaker, evaluates command, records completion. This is the common dispatch seam. |
| `bin/bb-hunt` | Thin wrapper around campaign skill; preserves orchestration in the skill. |
| `bin/bb-tools` | Registry-based list/doctor/explain/install/update/lock/verify. Install/update execute registry-derived commands; Go installs use `@latest`. A lock records versions, not an immutable verified tool supply chain. |
| `bin/bb-tools-doctor.py` | Checks binary availability and version output; does not demonstrate actual scanner correctness. |
| `.claude/skills/*` | Fat packages: router, YAML commands, scripts, runbooks, payloads and standards mappings. Preserve canonical location. |
| `tools/schemas/skill.schema.json` | Metadata plus named workflows; command required, inputs/output paths, evidence directory, safety/auth/rate metadata and next recommendations. These declarations are not universally enforced at runtime. |
| Campaign skill | Recon/model/technique/planning backbone, ranked skill execution, results/status/log files and FSM; skips successful prior workflows on resume. |
| Program memory | SQLite WAL facts with UUIDs, timestamps, confidence, sensitivity, source reference, expiry and superseding corrections. Existing negative-result category `tested_clean`. |
| Asset graph / traffic corpus / coverage | Existing relationship store, normalized traffic and coverage artifacts should remain authoritative domain records; reference them from memory rather than duplicate them. |
| Planner | Scores technique matches using business impact, surface prevalence, severity, signal quality, coverage and tool availability. Not yet a durable application-question queue. |
| Reporting / impact verifier | Candidate collection, classification, report formatting, readiness scoring and quality checks. Formatting checks are not evidence of a vulnerability. |
| Trace / circuit breaker | Local execution records and failure cooldown. Not complete tamper-resistant audit or aggregate request enforcement. |

Runtime state is generally gitignored under `.bb/`, output, evidence and engagements. A single repo-global context and cookie path are shared across sessions. Campaign resume reads that context rather than restoring an immutable campaign-owned context. Switching programs or running concurrently can therefore mix execution context. Status writes are not atomic, and the campaign finally block labels terminal state `done` even after exceptions. Reporting ignores the campaign budget and starts batch generation before candidate collection/readiness.

## Existing agent integrations

`AGENTS.md` already holds shared instructions. `CLAUDE.md` imports it via `@AGENTS.md`. No Claude runtime is needed to execute the shell/Python harness.

`tools/adapters/manifest.yaml` offers Claude Code, OpenCode, Codex, Cursor and generic outputs. However, the renderer still parses headings from CLAUDE.md, whose content is now only an import. It does not resolve that import. OpenCode generation targets the canonical AGENTS.md itself; `_apply_target` backs up then overwrites files without enforcing the manual-edit protection described in docs/adapters.md. Codex output is `.codex/instructions.md`; its runtime discovery has not been demonstrated. Avoid applying these generators as-is.

Hardcoded `.claude/skills` paths appear throughout scripts and docs; that is a storage convention, not inherently provider dependence. MCP-related registry/workflows exist (including binary/Burp-related tooling), but no shared BountyHarness MCP dispatch contract was established by this audit. Native Codex/OpenCode/Claude session compatibility was not tested.

## Prioritized findings

Priority is remediation order for the requested environment, not a public vulnerability severity rating.

| ID | Priority | Evidence and consequence |
|---|---|---|
| A1 | P0 | `bin/bb-run` preflight prints missing-scope warnings and continues to `eval`; no shared human approval gate. Campaign `hunt()` treats any nonempty scope file as enough to unlock intrusive execution. Active-safe traffic is allowed without that file. |
| A2 | P0 | Scope parser skips every `#` line, including documented/generated `# Out of Scope` headings. Entries below them can become inclusions. Plain domains also implicitly allow subdomains; URL rules use prefix matching and manual URL parsing. Independent request/redirect/DNS enforcement is absent from the dispatch path. |
| A3 | P0 | `impact_analyzer.py:verify_impact` marks matching `impact_class` as `confirmed` without inspecting evidence. Locally reproduced with a synthetic account-takeover label and no artifacts. Verification workflows overwrite the same verified output across classes. |
| A4 | P0 | Model-accessible source, context, policy and shell execution share the operator's authority. `source context.env` executes code; YAML commands run through eval. Instructions/regex sanitizers cannot prevent a shell-capable agent changing or bypassing its own restrictions. An external isolation boundary is necessary. |
| A5 | P1 | `tools/safety_profiles.yaml` allows active-safe and intrusive without approval; no shared request counter/budget. bb-run trace tier derives from skill tool dependencies rather than the selected workflow's risk. A Python wrapper can obscure actual activity. |
| A6 | P1 | Campaign uses shared context on resume; no durable provider/session identity, task lease, context binding or idempotency contract. Recovery and concurrent program isolation need behavioral tests. |
| A7 | P1 | Memory corrections preserve old content but get/correct select by ID without program binding. `--reviewed`, `--cross-program`, `--allow-secrets` are caller-controlled. Provenance lacks agent/session/task IDs; competing corrections lack revision checks. |
| A8 | P1 | Memory summary only excludes high-sensitivity technology facts, not all categories. Safe export trusts sensitivity labels and does not re-scan content. Correction reason is concatenated after secret checking. Secret regexes and gitignore are useful but insufficient boundaries. |
| A9 | P1 | bb-run logs completion after eval, missing denied/preflight/interrupted lifecycle events; raw command stdout and campaign capture need independent redaction review. Trace records label redaction applied but do not sanitize arbitrary metadata. BatchedTraceWriter.flush has unreachable assignment to `d`, causing an unbound local when flushing a nonempty buffer (static finding). |
| A10 | P1 | Reporting quality gate treats missing request/response as warnings, and evidence checks are optional without finding-dir. It cannot establish authorization boundaries, independent review or submission approval. |
| A11 | P1 | Adapter canonical-source mismatch and overwrite behavior can remove shared instructions. No tested native capability negotiation or task/result schema. |
| A12 | P2 | Program-memory SKILL.md advertises workflows absent from current YAML, including import-run and export-planner-hints. README/AGENTS skill counts drift. bb-init JSON escaping, macOS-specific PATH assumptions and bb-tools embedded Python interpolation require hardening. |

Agent-safety offers optional pattern scanning/sanitization and tool-call assessment. It is not centrally invoked by bb-run, and stripping instruction-like text is not a reliable prompt-injection security boundary. Target content must remain explicitly untrusted through ingestion, memory, planning and review.

## Baseline and validation limits

Initial default `python3` lacked PyYAML: make test failed YAML parsing; validator raised ModuleNotFoundError. Installed PyYAML 6.0.3 into `/tmp/bh-audit-venv` (Python 3.13) only; repository dependencies/source were unchanged.

Reproduction commands:

```sh
python3 -m venv /tmp/bh-audit-venv
/tmp/bh-audit-venv/bin/pip install pyyaml==6.0.3
PATH=/tmp/bh-audit-venv/bin:$PATH make test
PATH=/tmp/bh-audit-venv/bin:$PATH python3 tools/validate_skills.py
PATH=/tmp/bh-audit-venv/bin:$PATH python3 tools/validate_skills.py audit-security
PATH=/tmp/bh-audit-venv/bin:$PATH python3 tools/validate_skills.py audit-workflows
```

`make test`: exit 0 with dependency present; YAML parse and Python compile only. General validator: exit 0 **but reports SOME FAIL**, average 82/100; binary-analysis, race-condition and sqli each 69/100 against threshold 70. Do not interpret its process status as all packages passing. Security audit: exit 2. Workflow audit: exit 2. A second local synthetic check reproduced A2: `# In Scope` / `example.test` / `# Out of Scope` / `private.test` was parsed with both hosts in_scope, and https://private.test was accepted. Local logs are `/tmp/bh-test-ready.log`, `/tmp/bh-validator-ready.log`, `/tmp/bh-security.log`, `/tmp/bh-workflows.log`; generated quality report remains ignored.

CI runs compile/parse, lint, quality/release audits, gitleaks and private-path checks. No general behavioral tests directory was found. Evaluation-harness has deliberately vulnerable positive/negative shell fixtures, but make test does not execute them. Those applications were not started in this audit. No end-to-end provider, network-isolation, request-budget or recovery guarantee is established by the baseline.

See AGENT_COMPATIBILITY_PLAN.md for the staged changes and acceptance gates. Implementation requires approval after this audit.
