# Assess reportability

Use the governed finding state and [Phase 5 review](../../../../docs/phase5-review.md), not a legacy readiness score. Evaluate technical validity, realistic reachability, deployment relevance, demonstrated impact and program eligibility separately. Unknown information stays unknown.

`bin/bb-run impact-verifier report-readiness` flags legacy file claims for independent review; it never grants report readiness. A separate human decision requires qualified evidence and an independent recommendation. Submission approval remains unavailable until the separate Phase 6 gate is implemented.

Completion: preserve every missing requirement, waiver rationale, unsupported impact and duplicate concern. Human-confirmed state still has submission_approved=false.
