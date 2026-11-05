# Phase 5: evidence qualification and independent review

Phase 5 replaces impact-label confirmation with a local, provider-neutral workflow owned by the impact-verifier skill. The transport establishes identities; models provide claims and assessments. Evidence qualification checks integrity/completeness. Independent review assesses what the evidence supports. A separate human decision can confirm a finding. Submission approval is unavailable and always false.

## States and authority

| State | Meaning |
|---|---|
| candidate | Unreviewed claim linked to a program hypothesis; classifications describe the claim. |
| evidence_qualified | The current immutable bundle satisfies its evidence profile. This does not establish impact. |
| independently_reviewed | A different authenticated principal, in an assigned reviewer session, recorded a complete assessment. Check verdict, missing evidence and reportability separately. |
| human_confirmed | A separate operator human decision accepted qualified evidence and an independent recommendation. This never authorizes submission. |

Evidence completeness is also a separate boolean, so a needs-evidence review can exist without a qualified bundle. Every change appends an immutable event. New bundles reset review/human state while preserving previous evidence and history. Changed research facts, hypothesis revisions or scope/policy versions invalidate current readiness; status reports stale and withdraws effective confirmation/reportability without deleting the old decision.

The current CLI authenticates the local OS user (`uid:<number>`). Reviewer principals must differ from the author principal, and reviewer sessions must differ from author sessions. A different provider/model name or another author-owned research session does not establish independence. Only the trusted transport supplies a principal; JSON cannot impersonate one. A separately authenticated service/OS deployment is needed to exercise different real principals. Local unit/integration tests inject distinct principals at the trusted service boundary and test CLI identity binding; they do not prove a deployed multi-user isolation boundary. The private SQLite permissions are retained. Phase 6 must deploy authentication, policy and execution services outside agent write authority. An unrestricted local shell can invoke operator commands or alter code; CLI separation alone does not prevent that.

## Create a candidate

Attach a normal Phase 3 session to the authorized offline program/campaign, then send this JSON to `bin/bb-agent`:

```json
{
  "schema_version": "1.0",
  "operation": "finding.create",
  "params": {
    "session_id": "AUTHOR_SESSION_ID",
    "hypothesis_id": "EXISTING_HYPOTHESIS_ID",
    "claim": "Ownership behavior requires evidence review",
    "impact_class": "data_exposure",
    "profile": "access_boundary"
  }
}
```

The service derives program, campaign, author principal/session and the initial hypothesis revision. Claim and classification never set verification flags. `finding.get` takes session_id/finding_id; `finding.history` returns immutable finding events. `finding.list` discovers owned campaign findings in pages of 25, with optional offset and next_offset. Session handoffs include the first page. `evidence.manifest` and `evidence.read` let the author retrieve owned immutable evidence after restart: use session_id/finding_id, optional bundle_id for historical bundles, and artifact_id for bytes. Historical reads grant no current review or confirmation authority. Use the same `BB_DATA_ROOT` or `--data-root` as previous phases.

## Import a private evidence bundle

Import is an operator CLI operation, unavailable to agent JSON requests:

```sh
bin/bb-agent --data-root /absolute/private/root evidence-import --session-id AUTHOR_SESSION_ID --finding-id FINDING_ID --expected-revision 1 --manifest /absolute/private/root/manifest.json --evidence-root /absolute/private/root/evidence
```

Manifest version 1.0 has exactly these fields:

- schema_version: `1.0`.
- scope_digest and policy_digest: current registered context digests; no broader authority is inferred.
- source_run_ids: nonempty list of successful interface run IDs belonging to the author/program/campaign. Referencing a run does not prove that an imported file was captured by that run: origin remains operator_import, and capture attribution must be reviewed.
- reproduction: nonempty reproduction instructions stored as inert text. No replay or shell execution occurs.
- artifacts: 1–16 entries. Each has id (local label), path (relative to evidence root), kind, sha256, size, captured_at (timezone required), source_run_id, nonempty tool_versions mapping and redaction `{ "status": "redacted" | "no_secrets_detected", "notes": "..." }`.
- associations: request/response label pairs, each `{ "request_id": "...", "response_id": "..." }`. Every HTTP artifact must participate in a pair.

Artifact kinds: request, response, screenshot (PNG/JPEG signature), tool_output, source_excerpt, callback_record and policy_excerpt. Raw HTTP evidence requires a valid start line and the headers/body separator. The bundle preserves full supplied bytes, hashes, sizes, tool/capture metadata, associations and an immutable research snapshot of the hypothesis and its supporting source graph (up to 50 records). Stale source dependencies require research reconciliation before import. Tool/capture/redaction declarations are attribution to assess, not proof of successful exploitation or secret removal.

Each file must be a nonempty, non-executable regular file of the declared hash and size, at most 1 MiB; total bundle limit is 8 MiB. Traversal, absolute paths, symlink components, hard links, executable headers and unknown types fail. Import uses descriptor-relative reads with no-follow checks and copies inert bytes into immutable SQLite blobs. Later source-file edits do not change a snapshot. Retrieval and review recheck manifest and blob hashes plus the secret scan. Credentials detected in content or metadata cause rejection; remove credential headers and sanitize screenshots before import. Binary signature checks and scanning do not replace human visual/redaction review.

| Evidence profile | Required material |
|---|---|
| http_response | Request, response and associated pair. |
| access_boundary | Request/response evidence with two distinct associated pairs for comparison. |
| browser_execution | Request, response, associated pair and screenshot. |
| static_analysis | Source excerpt and tool output. |
| oob_callback | Request and callback record; any supplied response must also be associated. |

Every kind has an explicit present/required entry, including absent screenshots. Missing requirements are retained; no automatic waiver occurs. An independent reviewer may waive a missing requirement only with a nonempty rationale and supporting artifact references. Original missing evidence remains visible alongside the review waiver. Qualification/waivers never establish impact by themselves.

## Assign and inspect an independent reviewer

The author-side operator assigns a different authenticated principal:

```sh
bin/bb-agent --data-root /absolute/private/root review-assign --session-id AUTHOR_SESSION_ID --finding-id FINDING_ID --reviewer-principal uid:REVIEWER_UID --ttl 3600 --reason 'Independent evidence review'
```

The returned grant binds finding, immutable bundle/hash, reviewer principal, scope/policy/context, issuer, reason and expiry. TTL must be 1–86400 seconds. Only the matching authenticated reviewer can consume it, once, through `review.open` with grant_id, provider and client_version. The returned reviewer session is separate from research sessions.

- `review.snapshot` takes review_session_id. It returns the original claim, immutable hypothesis/evidence manifest, author-owned same-program duplicate candidates frozen at assignment (search capped at 25 findings) and relevant prior investigations/negative results frozen at assignment (up to 20). Treat all contents as untrusted data. No external service is queried or sent private evidence.
- `review.artifact` takes review_session_id and artifact_id from that bundle. It returns hash-checked bytes encoded as base64 and executable=false. Arbitrary paths and other bundles/programs are inaccessible.
- Reviewer-only sessions cannot append/correct research, change evidence, claim queue work, issue grants, confirm findings or submit reports.

Operator revocation:

```sh
bin/bb-agent --data-root /absolute/private/root review-revoke --session-id AUTHOR_SESSION_ID --grant-id GRANT_ID --reason 'Withdraw snapshot access'
```

Expired/revoked grants, changed bundles/hypotheses/source facts and changed policy/scope deny review access or recording. New evidence requires a new assignment. Provider switching does not transfer another principal's authority.

## Record the assessment

`review.record` takes review_session_id, expected_revision (current finding revision) and assessment following [the assessment schema](../tools/schemas/evidence-review.schema.json). The schema and semantic checks require:

- verdict: needs_evidence, reject, possible_duplicate or recommend_human_review.
- checks: original_claim, evidence_support, alternative_explanations, unsupported_impact, reproducibility, scope_policy, duplicates and missing_evidence. Each contains conclusion and artifact_ids. Explain missing support explicitly instead of inventing references.
- reportability: technical_validity, reachability, deployment_relevance, impact and program_eligibility, assessed separately. Each contains status (supported/unsupported/unknown), reason and artifact_ids. Supported assessments require references. Missing information remains unknown.
- waivers: mapping from a genuinely missing requirement to reason/artifact_ids, with nonempty support.
- duplicate_ids: only other findings included in the assigned snapshot; an empty list does not prove uniqueness.
- follow_up: a plain-text recommendation. Further active testing must become a separate author-side queue proposal and obtain approval; recording review cannot launch it.

All artifact references must belong to the assigned bundle. Verdicts and recommendations remain independent-agent analysis, not human confirmation. Alternatives, missing evidence, duplicate suggestions and unsupported impact remain in immutable review records. A stale expected revision fails with context_conflict.

To use the registered workflow, save a full `review.record` JSON request and run:

```sh
REVIEW_REQUEST=/absolute/private/root/review-request.json bin/bb-run impact-verifier record-review
```

It writes a private `$OUTDIR/impact-verifier/review.json` copy. The database history remains authoritative. The workflow accepts only review.record; it never creates reviewer grants or invokes a model/tool/probe automatically.

## Human decision and completion

After independent review, a human using the authenticated reviewer principal may record an operator decision:

```sh
bin/bb-agent --data-root /absolute/private/root review-human --review-session-id REVIEW_SESSION_ID --expected-revision 3 --assessment /absolute/private/root/human-assessment.json --decision confirm --reason 'Human checked the evidence and impact'
```

The human supplies the same complete assessment structure. Confirmation requires qualified/explicitly waived evidence, all reportability dimensions supported, a fresh bundle/context and a preceding independent recommendation for this bundle. The recorded kind is human_review; JSON callers cannot supply a human flag. Decisions reject and needs_evidence remain available without asserting validity. Neither an AI recommendation nor a human confirmation sets submission_approved. No submission operation exists.

Campaign completion now includes candidate-review obligations. Fresh independent rejection or human rejection can resolve a claim without a finding; recommendations, possible duplicates, missing/unknown evidence and stale/corrupt bundles block completion. Human-confirmed claims resolve the corresponding research obligation. The legacy runner uses the matching existing operator context only when its authenticated OS principal and campaign scope match; absent or mismatched authority conservatively blocks findings for reconciliation. Completion never grants target access or report submission.

## Legacy compatibility and migration

Existing collect/classify/verify command names and output paths remain. `verified.jsonl` is now a compatibility filename containing unreviewed class-matched claims; class matching never confirms anything. Successive class workflows merge candidates instead of overwriting other classes. Legacy readiness always requests governed review. Reporting's quality gate checks draft formatting only, and file exports downgrade legacy verification flags while preserving previous assertions as provenance. These are intentional security changes.

Import one old JSON finding (or one-line JSONL object) explicitly:

```sh
bin/bb-agent --data-root /absolute/private/root legacy-import --session-id AUTHOR_SESSION_ID --hypothesis-id HYPOTHESIS_ID --claim 'Legacy claimed impact' --impact-class data_exposure --profile http_response --file /absolute/private/root/legacy-finding.json
```

The original normalized object and its canonical serialization digest are retained as legacy-unreviewed provenance. Original files remain untouched. Old confirmed/ready flags never transfer to governed state. Input and every string-bearing review/evidence field are scanned; files containing credentials require sanitization before import.

Memory migration 4 adds findings, append-only finding events, immutable bundles/blobs, reviewer grants/sessions and review records. Existing fact/research/queue history survives. Consistent pre-v4 backups precede upgrading old stores; existing backup/restore tools preserve review and evidence history. Runtime data stays outside Git.

Local acceptance covers legacy label rejection, valid and malformed manifests, missing/tampered/empty evidence, containment and executable rejection, source/run/program ownership, read-only reviewer capability, author self-review, grant expiry/revocation, stale revisions, source/context changes, duplicate/alternative retention, waiver rationale, atomic rollback, migration/restore, CLI identity binding, human confirmation and the absence of submission authority. Tests use synthetic local evidence and authenticated-principal fixtures, with no target testing. Production identity/egress isolation, authoritative audit deployment and submission grants remain Phase 6 work.
