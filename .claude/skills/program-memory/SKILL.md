# Program Memory

## Overview
Persist application understanding, observations, hypotheses, investigations, negative results and decisions across provider sessions. Research logic lives in scripts/research_store.py; provider transports establish principal and session binding.

## Workflow selection
Use `bin/bb-agent` research operations described in docs/phase3-memory.md for migrated stores. `research.append` records unreviewed knowledge; `research.correct` requires an expected revision and reason. `research.query`, `research.history`, `research.summary`, `research.contradictions` and `research.export` retrieve bounded program-specific views. Read session.handoff when switching agents.

Existing skill.yaml workflows remain available for legacy stores. Once migrated, legacy fact writes fail explicitly; use the research API. Never set reviewed, allow-secrets or cross-program overrides. Agent scope notes do not change authoritative scope. A negative result records what was tested, conditions, evidence and an expiry/retest trigger; it does not prove a surface permanently safe.

## Storage and migration
Phase 5 stores governed findings, immutable evidence and independent review separately from ordinary research assertions; use [the review operations](../../../docs/phase5-review.md) for those records.
The research API extends the existing facts SQLite database with numbered transactional migrations, immutable events and query projections. Original fact IDs and correction links remain intact. It creates a consistent pre-migration backup before changing existing databases. Migrate/import, backup and restore commands are in scripts/research_admin.py and docs/phase3-memory.md. Originals are never moved or deleted automatically.

Use an operator-selected private data root. Runtime databases and backups stay out of Git. All source metadata and correction reasons are checked for secrets; exports recheck records. Sensitivity propagates through source references. Human review is a separate authority, unavailable through agent flags.

## Evidence and provenance
Link existing asset-graph, traffic-corpus and coverage IDs rather than copying their stores. Evidence references use registered opaque artifact IDs and hashes. Preserve source IDs, revisions, confidence and observation/inference labels. Contradiction detection only proposes review and never merges claims.

## Quick Reference
- Skill: `program-memory`, version 3.1.0; passive local storage.
- Canonical research interface: `bin/bb-agent`; SQLite store is local-only, never committed, protected by .gitignore.
- Redaction policy: reject detected secrets before persistence and recheck retrieval/export; omit high-sensitivity records from derived views.

## Workflow Selection
Use research operations for migrated databases. For unmigrated legacy databases, choose an existing workflow below. Consult technique-kb for testing methods; memory records do not authorize them.

## Available Workflows
| Workflow | Purpose |
|---|---|
| init-memory | Initialize legacy facts storage |
| record-fact | Add a legacy unreviewed fact |
| search-facts | Program-bound legacy search |
| record-correction | Program-bound correction with revision guard |
| decay-facts | Mark expired legacy facts stale |
| summarize | Sanitized legacy summary |
| check-contradictions | Propose legacy contradiction review |
| export-safe | Recheck report-safe legacy export |

## Evidence Required
Preserve source IDs, artifact hashes, capture context and tool provenance. Store observed behavior separately from inference. Negative investigations need conditions, limitations and retest criteria.

## References
OWASP WSTG-INFO-01; ASVS V1; docs/phase3-memory.md; technique-kb.
