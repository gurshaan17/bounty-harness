# Independent evidence review

For candidate verification, follow [Phase 5 review](../../../../docs/phase5-review.md): create a hypothesis-bound candidate, have the operator import a private manifest and assign a different authenticated principal, then inspect the read-only snapshot and record all review checks.

The existing verify-data-exposure, verify-privilege-escalation, verify-account-takeover and verify-tenant-break workflows select unreviewed claims only. Their verified.jsonl filename is retained for compatibility. Classification cannot establish impact, human confirmation or submission permission.

Completion: a structured review records evidence support, alternatives, reproducibility, policy, duplicates, missing evidence and reportability separately, with reasons and artifact references. Human confirmation remains a separate operator decision.
