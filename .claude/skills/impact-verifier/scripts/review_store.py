"""Evidence-qualified candidates and independently attributed offline review; no submission."""

import hashlib
import importlib.util
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

_spec = importlib.util.spec_from_file_location("bh_evidence", Path(__file__).with_name("evidence_bundle.py"))
evidence = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(evidence)
CHECKS = {"original_claim", "evidence_support", "alternative_explanations", "unsupported_impact", "reproducibility", "scope_policy", "duplicates", "missing_evidence"}
REPORTABILITY = {"technical_validity", "reachability", "deployment_relevance", "impact", "program_eligibility"}
VERDICTS = {"needs_evidence", "reject", "possible_duplicate", "recommend_human_review"}


class ReviewConflict(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 8192:
        raise ValueError("Nonempty bounded text required")
    return value.strip()


def registered_context(database, program, campaign, principal):
    """Read existing operator context without creating or migrating interface state."""
    import sqlite3
    from contextlib import closing

    path = Path(database)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT revision,scope,policy FROM contexts WHERE program=? AND campaign=? AND principal=?", (program, campaign, principal)).fetchone()
        return {"program_id": program, "campaign_id": campaign, "context_revision": row[0], "scope_digest": row[1], "policy_digest": row[2]} if row else None
    except sqlite3.Error:
        return None


class Reviews:
    def __init__(self, store, principal):
        self.store, self.db, self.principal = store, store.db, principal

    def finding(self, finding_id):
        row = self.db.execute("SELECT data FROM findings WHERE id=?", (finding_id,)).fetchone()
        if not row:
            raise ValueError("Unknown finding")
        value = json.loads(row[0])
        self.store.validate_payload(value)
        return value

    def owned(self, finding_id, context, session):
        value = self.finding(finding_id)
        if value["program_id"] != context["program_id"] or value["campaign_id"] != context["campaign_id"] or value["author_principal"] != self.principal:
            raise ValueError("Finding ownership mismatch")
        return value

    def checked(self, value, revision):
        if type(revision) is not int or revision != value["revision"]:
            raise ReviewConflict("Finding revision changed")

    def save(self, value, action, provenance):
        value = {**value, "revision": value.get("revision", 0) + 1, "submission_approved": False}
        self.store.validate_payload(value)
        self.store.validate_payload(provenance)
        event = {"finding": value, "action": action, "at": datetime.now(UTC).isoformat(), "provenance": provenance}
        self.db.execute("INSERT OR REPLACE INTO findings VALUES(?,?,?,?,?)", (value["program_id"], value["campaign_id"], value["id"], value["revision"], json.dumps(value)))
        self.db.execute("INSERT INTO finding_events VALUES(?,?,?,?)", (str(uuid.uuid4()), value["id"], value["revision"], json.dumps(event)))
        return value

    def create(self, context, session, hypothesis_id, claim, impact_class, profile, legacy=None):
        self.store.validate_payload({"claim": claim, "impact_class": impact_class, "legacy": legacy})
        if profile not in evidence.PROFILES:
            raise ValueError("Unknown evidence profile")
        claim, impact_class = text(claim), text(impact_class)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            hypothesis = self.store.get(context["program_id"], hypothesis_id)
            if not hypothesis or hypothesis["kind"] != "hypothesis" or hypothesis["sensitivity"] == "high-sensitivity":
                raise ValueError("Visible program hypothesis required")
            self.store.validate_payload(hypothesis)
            value = {
                "schema_version": "1.0",
                "id": str(uuid.uuid4()),
                "program_id": context["program_id"],
                "campaign_id": context["campaign_id"],
                "author_principal": self.principal,
                "author_session_id": session["session_id"],
                "revision": 0,
                "hypothesis_id": hypothesis_id,
                "hypothesis_revision": hypothesis["revision"],
                "claim": claim,
                "impact_class": impact_class,
                "profile": profile,
                "state": "candidate",
                "evidence_qualified": False,
                "bundle_id": None,
                "sensitivity": "program-private",
                "trust": "legacy-unreviewed" if legacy else "agent-unreviewed",
                "legacy_claim": legacy,
                "review_ids": [],
                "reportable": False,
                "human_confirmed": False,
            }
            value = self.save(value, "legacy_import" if legacy else "candidate_created", {"principal": self.principal, "session_id": session["session_id"]})
            self.db.commit()
            return value
        except BaseException:
            self.db.rollback()
            raise

    def research_snapshot(self, program, hypothesis):
        records, pending = {}, [hypothesis]
        while pending:
            record = pending.pop()
            if record["id"] in records:
                continue
            self.store.validate_payload(record)
            if record["sensitivity"] == "high-sensitivity" or len(records) >= 50:
                raise ValueError("Research snapshot inaccessible or too large")
            records[record["id"]] = record
            for source_id, revision in record.get("source_revisions", {}).items():
                source = self.store.get(program, source_id)
                if not source or source["revision"] != revision:
                    raise ReviewConflict("Hypothesis relies on stale source facts; reconcile the research claim first")
                pending.append(source)
        return [records[key] for key in sorted(records)]

    def import_bundle(self, context, session, finding_id, revision, manifest, root, verify_run):
        # Complete all reads and checks before writing any blob or finding update.
        candidate = self.owned(finding_id, context, session)
        prepared, blobs = evidence.import_files(root, manifest, candidate["profile"], self.store.validate_payload, verify_run)
        if any(prepared[key] != context[key] for key in ("scope_digest", "policy_digest")):
            raise ReviewConflict("Evidence scope/policy differs from current context")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            candidate = self.owned(finding_id, context, session)
            self.checked(candidate, revision)
            hypothesis = self.store.get(context["program_id"], candidate["hypothesis_id"])
            self.store.validate_payload(hypothesis)
            research_snapshot = self.research_snapshot(context["program_id"], hypothesis)
            # Content IDs are unique per import so unrelated bundles cannot select each other's blobs.
            ids = {name: str(uuid.uuid4()) for name in blobs}
            artifacts = [{**a, "id": ids[a["id"]], "local_label": a["id"]} for a in prepared["artifacts"]]
            associations = [{key: ids[value] for key, value in pair.items()} for pair in prepared["associations"]]
            bundle = {
                **prepared,
                "id": str(uuid.uuid4()),
                "finding_id": finding_id,
                "program_id": context["program_id"],
                "campaign_id": context["campaign_id"],
                "context_revision": context["context_revision"],
                "hypothesis": hypothesis,
                "research_snapshot": research_snapshot,
                "author_principal": candidate["author_principal"],
                "author_session_id": candidate["author_session_id"],
                "artifacts": artifacts,
                "associations": associations,
                "trust": "operator-imported-unreviewed",
            }
            bundle["sha256"] = digest(bundle)
            for label, data in blobs.items():
                self.db.execute("INSERT INTO evidence_blobs VALUES(?,?,?)", (ids[label], context["program_id"], data))
            self.db.execute("INSERT INTO evidence_bundles VALUES(?,?,?)", (bundle["id"], finding_id, json.dumps(bundle)))
            candidate.update(
                bundle_id=bundle["id"],
                hypothesis_revision=hypothesis["revision"],
                evidence_qualified=not bundle["missing"],
                state="candidate" if bundle["missing"] else "evidence_qualified",
                reportable=False,
                human_confirmed=False,
                review_ids=[],
                last_review_kind=None,
                last_verdict=None,
                human_decision=None,
                reportability={},
            )
            candidate = self.save(candidate, "bundle_attached", {"principal": self.principal, "session_id": session["session_id"], "bundle_hash": bundle["sha256"]})
            self.db.commit()
            return {"finding": candidate, "manifest": bundle}
        except BaseException:
            self.db.rollback()
            raise

    def bundle(self, candidate):
        row = self.db.execute("SELECT data FROM evidence_bundles WHERE id=? AND finding=?", (candidate["bundle_id"], candidate["id"])).fetchone()
        if not row:
            raise ValueError("Missing evidence bundle")
        bundle = json.loads(row[0])
        self.store.validate_payload(bundle)
        if digest({k: v for k, v in bundle.items() if k != "sha256"}) != bundle["sha256"]:
            raise ValueError("Bundle manifest integrity failure")
        for artifact in bundle["artifacts"]:
            self.artifact_bytes(bundle, artifact["id"])
        return bundle

    def artifact_bytes(self, bundle, artifact_id):
        artifact = next((a for a in bundle["artifacts"] if a["id"] == artifact_id), None)
        if not artifact:
            raise ValueError("Artifact is outside the review snapshot")
        row = self.db.execute("SELECT data FROM evidence_blobs WHERE id=? AND program=?", (artifact_id, bundle["program_id"])).fetchone()
        if not row or len(row[0]) != artifact["size"] or hashlib.sha256(row[0]).hexdigest() != artifact["sha256"]:
            raise ValueError("Missing, empty or tampered evidence")
        evidence.scan_bytes(row[0], self.store.validate_payload)
        return artifact, row[0]

    def fresh(self, candidate, bundle, context):
        hypothesis = self.store.get(candidate["program_id"], candidate["hypothesis_id"])
        if not hypothesis or hypothesis["revision"] != bundle["hypothesis"]["revision"] or candidate["bundle_id"] != bundle["id"]:
            raise ReviewConflict("Hypothesis or evidence changed; a new review is required")
        for record in bundle.get("research_snapshot", []):
            current = self.store.get(candidate["program_id"], record["id"])
            if not current or current["revision"] != record["revision"]:
                raise ReviewConflict("Research source facts changed; reconcile before review")
        if any(bundle[key] != context[key] for key in ("scope_digest", "policy_digest", "context_revision")):
            raise ReviewConflict("Scope/policy context changed; requalify evidence")

    def status(self, candidate, context):
        value = {**candidate, "stale": False, "submission_approved": False}
        if candidate["bundle_id"]:
            bundle = self.bundle(candidate)
            try:
                self.fresh(candidate, bundle, context)
            except ReviewConflict:
                value.update(stale=True, human_confirmed=False, reportable=False, state="candidate", historical_state=candidate["state"])
        return value

    def listing(self, context, offset=0):
        if type(offset) is not int or not 0 <= offset <= 100000:
            raise ValueError("Invalid finding offset")
        query = "FROM findings WHERE program=? AND campaign=? AND json_extract(data, '$.author_principal')=?"
        parameters = (context["program_id"], context["campaign_id"], self.principal)
        total = self.db.execute("SELECT count(*) " + query, parameters).fetchone()[0]
        rows = self.db.execute("SELECT data " + query + " ORDER BY id LIMIT 25 OFFSET ?", (*parameters, offset)).fetchall()
        summaries = []
        for row in rows:
            candidate = json.loads(row[0])
            self.store.validate_payload(candidate)
            try:
                state = self.status(candidate, context)
            except (ValueError, KeyError, TypeError):
                state = {**candidate, "state": "candidate", "stale": True, "human_confirmed": False, "reportable": False, "integrity_error": True}
            summaries.append({key: state.get(key) for key in ("id", "revision", "claim", "state", "bundle_id", "stale", "human_confirmed", "reportable", "integrity_error")})
        return {"findings": summaries, "total": total, "next_offset": offset + len(rows) if offset + len(rows) < total else None}

    def assign(self, candidate, context, reviewer_principal, ttl, reason):
        reviewer_principal, reason = text(reviewer_principal), text(reason)
        if reviewer_principal == candidate["author_principal"] or type(ttl) is not int or not 1 <= ttl <= 86400:
            raise ValueError("Independent reviewer principal and bounded grant lifetime required")
        bundle = self.bundle(candidate)
        self.fresh(candidate, bundle, context)
        support = self.support_snapshot(candidate)
        grant = {
            "support_snapshot": support,
            "support_hash": digest(support),
            "id": str(uuid.uuid4()),
            "finding_id": candidate["id"],
            "bundle_id": bundle["id"],
            "bundle_hash": bundle["sha256"],
            "reviewer_principal": reviewer_principal,
            "issued_by": self.principal,
            "reason": reason,
            "expires_at": time.time() + ttl,
            "revoked": False,
            "session_id": None,
            "context": context,
        }
        self.store.validate_payload(grant)
        with self.db:
            self.db.execute("INSERT INTO review_grants VALUES(?,?)", (grant["id"], json.dumps(grant)))
        return grant

    def grant(self, grant_id):
        row = self.db.execute("SELECT data FROM review_grants WHERE id=?", (grant_id,)).fetchone()
        if not row:
            raise ValueError("Unknown review grant")
        grant = json.loads(row[0])
        if grant["revoked"] or grant["expires_at"] <= time.time():
            raise ValueError("Review grant revoked or expired")
        return grant

    def open(self, grant_id, provider, client_version):
        text(provider)
        text(client_version)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            grant = self.grant(grant_id)
            if grant["reviewer_principal"] != self.principal:
                raise ValueError("Review principal mismatch")
            candidate = self.finding(grant["finding_id"])
            if self.principal == candidate["author_principal"] or candidate["bundle_id"] != grant["bundle_id"]:
                raise ValueError("Author self-review or stale grant")
            if grant["session_id"]:
                raise ValueError("Grant already consumed")
            session = {
                "id": str(uuid.uuid4()),
                "grant_id": grant_id,
                "principal": self.principal,
                "provider": provider,
                "client_version": client_version,
                "mode": "independent_agent",
                "read_only_evidence": True,
            }
            self.store.validate_payload(session)
            grant["session_id"] = session["id"]
            self.db.execute("INSERT INTO review_sessions VALUES(?,?)", (session["id"], json.dumps(session)))
            self.db.execute("UPDATE review_grants SET data=? WHERE id=?", (json.dumps(grant), grant_id))
            self.db.commit()
            return session
        except BaseException:
            self.db.rollback()
            raise

    def reviewer(self, session_id, current_context):
        row = self.db.execute("SELECT data FROM review_sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise ValueError("Unknown reviewer session")
        session = json.loads(row[0])
        grant = self.grant(session["grant_id"])
        if digest(grant["support_snapshot"]) != grant["support_hash"]:
            raise ValueError("Review context integrity failure")
        candidate = self.finding(grant["finding_id"])
        if (
            session["principal"] != self.principal
            or grant["reviewer_principal"] != self.principal
            or self.principal == candidate["author_principal"]
            or session["id"] == candidate["author_session_id"]
        ):
            raise ValueError("Independent reviewer identity required")
        bundle = self.bundle(candidate)
        self.fresh(candidate, bundle, current_context(candidate["program_id"], candidate["campaign_id"]))
        if bundle["id"] != grant["bundle_id"] or bundle["sha256"] != grant["bundle_hash"]:
            raise ReviewConflict("Review snapshot changed")
        return session, grant, candidate, bundle

    def support_snapshot(self, candidate):
        local = []
        for row in self.db.execute(
            "SELECT id,data FROM findings WHERE program=? AND id!=? AND json_extract(data, '$.author_principal')=? ORDER BY id LIMIT 25",
            (candidate["program_id"], candidate["id"], candidate["author_principal"]),
        ):
            prior = json.loads(row["data"])
            self.store.validate_payload(prior)
            if prior["impact_class"] == candidate["impact_class"] or prior["hypothesis_id"] == candidate["hypothesis_id"]:
                local.append({"id": prior["id"], "claim": prior["claim"], "state": prior["state"], "trust": prior["trust"]})
        records, _ = self.store.snapshot(candidate["program_id"])
        references = [r for r in records if r["kind"] in ("investigation", "negative_result") and candidate["hypothesis_id"] in r.get("sources", [])][:20]
        return {"local_duplicate_candidates": local, "prior_investigations": references, "duplicate_search_limit": 25, "investigation_limit": 20}

    def snapshot(self, session_id, current_context):
        session, grant, candidate, bundle = self.reviewer(session_id, current_context)
        if digest(grant["support_snapshot"]) != grant["support_hash"]:
            raise ValueError("Review context integrity failure")
        return {"session": session, "finding": candidate, "manifest": bundle, **grant["support_snapshot"], "trust": "untrusted_evidence", "grants_execution": False, "submission_approved": False}

    def assessment(self, value, bundle, allowed_duplicates=()):
        evidence.exact(value, ("verdict", "checks", "reportability", "waivers", "duplicate_ids", "follow_up"))
        self.store.validate_payload(value)
        if value["verdict"] not in VERDICTS:
            raise ValueError("Unknown review verdict")
        evidence.exact(value["checks"], CHECKS)
        evidence.exact(value["reportability"], REPORTABILITY)
        artifact_ids = {a["id"] for a in bundle["artifacts"]}

        def references(refs):
            if not isinstance(refs, list) or len(refs) > 32 or any(not isinstance(ref, str) or ref not in artifact_ids for ref in refs):
                raise ValueError("Review references must belong to the immutable evidence bundle")

        for check in value["checks"].values():
            evidence.exact(check, ("conclusion", "artifact_ids"))
            text(check["conclusion"])
            references(check["artifact_ids"])
        for dimension in value["reportability"].values():
            evidence.exact(dimension, ("status", "reason", "artifact_ids"))
            if dimension["status"] not in ("supported", "unsupported", "unknown"):
                raise ValueError("Invalid reportability status")
            text(dimension["reason"])
            references(dimension["artifact_ids"])
            if dimension["status"] == "supported" and not dimension["artifact_ids"]:
                raise ValueError("Supported reportability requires artifact references")
        if not isinstance(value["waivers"], dict) or value["waivers"].keys() - set(bundle["missing"]):
            raise ValueError("Only missing requirements can be waived")
        for waiver in value["waivers"].values():
            evidence.exact(waiver, ("reason", "artifact_ids"))
            text(waiver["reason"])
            references(waiver["artifact_ids"])
            if not waiver["artifact_ids"]:
                raise ValueError("Evidence waiver requires supporting artifacts")
        if not isinstance(value["duplicate_ids"], list) or len(value["duplicate_ids"]) > 25:
            raise ValueError("Invalid duplicate references")
        for candidate_id in value["duplicate_ids"]:
            if candidate_id not in allowed_duplicates:
                raise ValueError("Duplicate reference is outside the assigned snapshot")
            prior = self.finding(candidate_id)
            if prior["program_id"] != bundle["program_id"] or prior["id"] == bundle["finding_id"]:
                raise ValueError("Duplicate reference must be a different local program finding")
        text(value["follow_up"])
        qualified = not (set(bundle["missing"]) - value["waivers"].keys())
        reportable = qualified and all(d["status"] == "supported" for d in value["reportability"].values())
        return qualified, reportable

    def record(self, session_id, revision, assessment, current_context, human=False, decision=None, reason=None):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            session, grant, candidate, bundle = self.reviewer(session_id, current_context)
            self.checked(candidate, revision)
            qualified, reportable = self.assessment(assessment, bundle, {item["id"] for item in grant["support_snapshot"]["local_duplicate_candidates"]})
            if human:
                if decision not in ("confirm", "reject", "needs_evidence"):
                    raise ValueError("Unknown human decision")
                text(reason)
                latest = self.db.execute("SELECT data FROM review_records WHERE finding=? ORDER BY rowid DESC LIMIT 1", (candidate["id"],)).fetchone()
                prior = json.loads(latest[0]) if latest else None
                if decision == "confirm" and (
                    assessment["verdict"] != "recommend_human_review"
                    or not reportable
                    or not prior
                    or prior["kind"] != "independent_agent"
                    or prior["assessment"]["verdict"] != "recommend_human_review"
                    or prior["bundle_id"] != bundle["id"]
                ):
                    raise ValueError("Human confirmation requires qualified evidence and independent recommendation")
            review = {
                "id": str(uuid.uuid4()),
                "schema_version": "1.0",
                "finding_id": candidate["id"],
                "bundle_id": bundle["id"],
                "bundle_hash": bundle["sha256"],
                "principal": self.principal,
                "session_id": session_id,
                "kind": "human_review" if human else "independent_agent",
                "assessment": assessment,
                "decision": decision,
                "reason": reason,
                "at": datetime.now(UTC).isoformat(),
            }
            self.store.validate_payload(review)
            self.db.execute("INSERT INTO review_records VALUES(?,?,?)", (review["id"], candidate["id"], json.dumps(review)))
            candidate.update(
                state="human_confirmed" if human and decision == "confirm" else "independently_reviewed",
                evidence_qualified=qualified,
                human_confirmed=bool(human and decision == "confirm"),
                reportable=bool(human and decision == "confirm" and reportable),
                review_ids=candidate["review_ids"] + [review["id"]],
                last_review_kind=review["kind"],
                last_verdict=assessment["verdict"],
                human_decision=decision if human else None,
                reportability=assessment["reportability"],
            )
            candidate = self.save(candidate, "human_decision" if human else "independent_review", {"principal": self.principal, "session_id": session_id, "review_id": review["id"]})
            self.db.commit()
            return {"finding": candidate, "review": review, "submission_approved": False}
        except BaseException:
            self.db.rollback()
            raise

    def history(self, finding_id):
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM finding_events WHERE finding=? ORDER BY revision", (finding_id,))]

    def revoke(self, grant_id, context, reason):
        text(reason)
        grant = self.grant(grant_id)
        candidate = self.finding(grant["finding_id"])
        if candidate["author_principal"] != self.principal or any(candidate[key] != context[key] for key in ("program_id", "campaign_id")):
            raise ValueError("Grant ownership mismatch")
        grant.update(revoked=True, revocation_reason=reason)
        self.store.validate_payload(grant)
        with self.db:
            self.db.execute("UPDATE review_grants SET data=? WHERE id=?", (json.dumps(grant), grant_id))
        return {"revoked": True, "grant_id": grant_id}


if __name__ == "__main__":
    import argparse

    argparse.ArgumentParser(description=__doc__, epilog="Use bin/bb-agent finding/review operations; see docs/phase5-review.md.").parse_args()
