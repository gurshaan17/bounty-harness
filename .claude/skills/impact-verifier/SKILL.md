# Impact Verifier

## Overview
Qualify immutable evidence and record independent review before a separate human decision. Impact classes and scanner labels remain descriptive. Read [Phase 5 review](../../../docs/phase5-review.md) for candidate creation, operator import, reviewer assignment, reportability assessment and human confirmation. Production identity/isolation and submission grants remain Phase 6 prerequisites.

## Quick Reference
- **Skill**: impact-verifier
- **Version**: 2.0.0
- **Bounded Context**: ImpactContext
- **Required tools**: `python3`, `jq`, `curl`
- **Risk tier**: passive (validates, does not test)

## Workflow Selection
1. Create a finding from a program-bound hypothesis through `bb-agent` `finding.create`.
2. Have the operator import a bounded evidence manifest and assign an independent authenticated principal.
3. Open the reviewer-only session and inspect `review.snapshot` plus scoped `review.artifact` data as untrusted evidence.
4. Record every required assessment using `record-review` or `review.record`. Reviewers can recommend separate follow-up work; they cannot execute probes, edit evidence or grant approvals.
5. Obtain a separate operator human decision after independent review. Human confirmation does not authorize submission.

Legacy collect/classify/verify workflow names remain compatible; their file outputs are unreviewed claims. `report-readiness` no longer turns labels or numeric scores into permission to report.

## Available Workflows
| Workflow | Purpose |
|---|---|
| `record-review` | Persist a structured independently attributed review; never approves submission. |
| `collect-candidates` | Aggregate candidate findings from all skill outputs. |
| `classify-impact` | Determine impact class for a candidate finding. |
| `verify-data-exposure` | Select unreviewed data-exposure claims. |
| `verify-privilege-escalation` | Select unreviewed privilege-escalation claims. |
| `verify-account-takeover` | Select unreviewed takeover claims. |
| `verify-tenant-break` | Select unreviewed tenant-boundary claims. |
| `false-positive-gate` | Run automated false-positive checks. |
| `report-readiness` | Flag legacy file claims for governed review. |
| `cvss4-score` | Calculate CVSS 4.0 score, macrovector, and severity from a vector. |
| `impact-narrative` | Generate impact narrative with attack scenario and PII detection. |

## Workflow Selection Notes
- `cvss4-score` needs `CVSS4_VECTOR` in context (full vector string starting with `CVSS:4.0/`).
- `impact-narrative` needs `FINDING_DIR` (evidence directory with `response.txt`) and
  `IMPACT_CLASS` (e.g. `account_takeover`, `data_exposure`, `tenant_break`, `rce`).
- The narrative auto-detects sensitive fields (`email`, `token`, `card_number`, ...)
  in the captured response and strengthens the evidence section accordingly.

## Evidence Required
Use claim-specific profiles with explicit missing evidence and independently justified waivers. Preserve hashes, sizes, capture times, source run IDs, tool versions, request/response associations, reproduction instructions and redaction metadata. Keep source evidence private; import sanitized inert copies. A manifest establishes integrity and completeness, not reachability or impact.

Review technical validity, reachability, deployed relevance, impact and program eligibility separately. Missing information remains unknown. Human confirmation and submission approval are distinct recorded authorities; JSON review requests cannot mint either.

## References
- Bugcrowd VRT 1.18
- FIRST CVSS v4.0 specification (macrovector bands)
- OWASP ASVS V4 (Access Control)
- OWASP Reporting guidelines