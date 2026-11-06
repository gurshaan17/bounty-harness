"""Phase 6 enforcement plane: operator approvals, aggregate budgets, audit chain, submission grants.

Runs inside the agent-interface service database, which is operator-side
state. Agents can request approvals and authorize dispatches but can never
mint grants, change budgets, or edit audit history. All decisions and
denials are appended to a hash-chained, trigger-protected audit log with
secret redaction applied before persistence.
"""

import hashlib
import importlib.util
import json
import re
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from tools.agent_interface.contracts import InterfaceError, digest

from . import scope

_spec = importlib.util.spec_from_file_location(
    "bh_memory_legacy", Path(__file__).resolve().parents[2] / ".claude/skills/program-memory/scripts/memory_store.py"
)
_legacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_legacy)

REDACT_PAIR = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|id[_-]?token|token|secret|password|passwd|private[_-]?key)\b\s*[:=]\s*\"?[^\s,;\"'}]+"
)
ALLOWED_METHODS = ("GET", "HEAD")
GENESIS = "0" * 64
MAX_REPORT_BYTES = 49152


def now():
    return datetime.now(UTC).isoformat()


def report_digest(report):
    """Canonical digest shared by report.submit and the operator grant CLI."""
    return hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()


def _redact(value):
    """Redact secret-looking material; fall back to a digest if anything remains."""
    text = json.dumps(value, sort_keys=True, default=str)
    for pattern, _ in _legacy.SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    text = REDACT_PAIR.sub(lambda m: m.group(1) + "=[REDACTED]", text)
    if _legacy._check_secrets(text):
        return {"redacted": True, "sha256": hashlib.sha256(text.encode()).hexdigest()}
    return json.loads(text)


class Control:
    """Enforcement operations bound to one authenticated principal."""

    def __init__(self, db, principal):
        self.db, self.principal = db, principal

    # ------------------------------------------------------------------ audit

    def audit(self, kind, program=None, campaign=None, detail=None, task_id=None, principal=None):
        """Append one hash-chained audit event. Caller owns the transaction."""
        last = self.db.execute("SELECT data FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
        previous = json.loads(last["data"] if isinstance(last, sqlite3.Row) else last[0])["hash"] if last else GENESIS
        body = {
            "schema_version": "1.0",
            "type": kind,
            "at": now(),
            "principal": principal or self.principal,
            "program_id": program,
            "campaign_id": campaign,
            "task_id": task_id,
            "detail": _redact(detail or {}),
        }
        body["prev_hash"] = previous
        body["hash"] = digest(body)
        event_id = str(uuid.uuid4())
        self.db.execute("INSERT INTO audit_events(id,data) VALUES(?,?)", (event_id, json.dumps(body, sort_keys=True)))
        return event_id

    def audit_list(self, program=None, limit=50):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise InterfaceError("invalid_input", "Invalid audit limit")
        if program is not None and not isinstance(program, str):
            raise InterfaceError("invalid_input", "Invalid program filter")
        query = "SELECT data FROM audit_events"
        params = ()
        if program is not None:
            query += " WHERE json_extract(data,'$.program_id')=?"
            params = (program,)
        rows = self.db.execute(query + " ORDER BY seq DESC LIMIT ?", (*params, limit)).fetchall()
        events = [json.loads(row["data"] if isinstance(row, sqlite3.Row) else row[0]) for row in rows]
        return {"events": events, "total_returned": len(events)}

    def audit_verify(self):
        previous = GENESIS
        count = 0
        first_break = None
        for row in self.db.execute("SELECT seq,data FROM audit_events ORDER BY seq"):
            event = json.loads(row["data"] if isinstance(row, sqlite3.Row) else row[1])
            expected = digest({k: v for k, v in event.items() if k != "hash"})
            if event.get("prev_hash") != previous or event.get("hash") != expected:
                first_break = row["seq"] if isinstance(row, sqlite3.Row) else row[0]
                break
            previous = event["hash"]
            count += 1
        return {"ok": first_break is None, "verified_events": count, "first_break_seq": first_break}

    # -------------------------------------------------------------- approvals

    def _approval_row(self, approval_id):
        row = self.db.execute("SELECT data FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if not row:
            return None
        value = json.loads(row[0] if not isinstance(row, sqlite3.Row) else row["data"])
        return value

    def request_approval(self, context, session, entry, inputs, max_requests, ttl_seconds):
        if _legacy._check_secrets(json.dumps(inputs, sort_keys=True)):
            raise InterfaceError("invalid_input", "Approval inputs contain secret-like material")
        record = {
            "schema_version": "1.0",
            "id": str(uuid.uuid4()),
            "kind": "approval_request",
            "status": "requested",
            "program_id": context["program_id"],
            "campaign_id": context["campaign_id"],
            "principal": self.principal,
            "session_id": session["session_id"],
            "provider": session["provider"],
            "client_version": session["client_version"],
            "context_revision": context["context_revision"],
            "scope_digest": context["scope_digest"],
            "policy_digest": context["policy_digest"],
            "skill": entry["skill"],
            "workflow": entry["workflow"],
            "workflow_digest": entry["workflow_digest"],
            "inputs_digest": digest(inputs),
            "inputs": _redact(inputs),
            "max_requests": max_requests,
            "ttl_seconds": ttl_seconds,
            "requested_at": now(),
        }
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("INSERT INTO approvals VALUES(?,?,?,?)", (record["id"], "approval_request", "requested", json.dumps(record, sort_keys=True)))
            self.audit(
                "proposed",
                record["program_id"],
                record["campaign_id"],
                {"approval_request_id": record["id"], "skill": record["skill"], "workflow": record["workflow"]},
                principal=self.principal,
            )
        return record

    def grant_approval(self, request_id, ttl_seconds, max_requests=None):
        """Operator transport only: mint a grant bound to the exact request."""
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
            raise InterfaceError("invalid_input", "Approval TTL must be 1-86400 seconds")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            request = self._approval_row(request_id)
            if not request or request.get("kind") != "approval_request" or request.get("status") != "requested":
                raise InterfaceError("invalid_input", "Unknown or already consumed approval request")
            limit = request["max_requests"] if max_requests is None else min(max_requests, request["max_requests"])
            if type(limit) is not int or not 1 <= limit <= 100000:
                raise InterfaceError("invalid_input", "Invalid request budget")
            grant = {
                "schema_version": "1.0",
                "id": str(uuid.uuid4()),
                "kind": "approval_grant",
                "status": "active",
                "request_id": request_id,
                "requester_principal": request["principal"],
                "program_id": request["program_id"],
                "campaign_id": request["campaign_id"],
                "context_revision": request["context_revision"],
                "scope_digest": request["scope_digest"],
                "policy_digest": request["policy_digest"],
                "skill": request["skill"],
                "workflow": request["workflow"],
                "workflow_digest": request["workflow_digest"],
                "inputs_digest": request["inputs_digest"],
                "issued_by": self.principal,
                "issued_at": now(),
                "expires_at": time.time() + ttl_seconds,
                "max_requests": limit,
                "requests_used": 0,
                "allowed_methods": list(ALLOWED_METHODS),
                "revoked": False,
            }
            self.db.execute("UPDATE approvals SET status=? WHERE id=?", ("granted", request_id))
            self.db.execute("INSERT INTO approvals VALUES(?,?,?,?)", (grant["id"], "approval_grant", "active", json.dumps(grant, sort_keys=True)))
            self.audit(
                "approved",
                grant["program_id"],
                grant["campaign_id"],
                {"grant_id": grant["id"], "request_id": request_id, "max_requests": limit, "expires_in": ttl_seconds},
                principal=self.principal,
            )
        return grant

    def revoke_approval(self, grant_id, reason):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1024:
            raise InterfaceError("invalid_input", "Revocation reason required")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            grant = self._approval_row(grant_id)
            if not grant or grant.get("kind") != "approval_grant":
                raise InterfaceError("invalid_input", "Unknown approval grant")
            if grant.get("revoked"):
                raise InterfaceError("invalid_input", "Grant already revoked")
            grant.update(status="revoked", revoked=True, revoked_at=now(), revocation_reason=reason[:256])
            self.db.execute("UPDATE approvals SET status=?,data=? WHERE id=?", ("revoked", json.dumps(grant, sort_keys=True), grant_id))
            self.audit("revoked", grant["program_id"], grant["campaign_id"], {"grant_id": grant_id, "reason": reason[:256]})
        return {"revoked": True, "grant_id": grant_id}

    def list_approvals(self, program, campaign):
        rows = self.db.execute(
            "SELECT data FROM approvals WHERE json_extract(data,'$.program_id')=? AND json_extract(data,'$.campaign_id')=? ORDER BY rowid DESC LIMIT 50",
            (program, campaign),
        ).fetchall()
        visible = []
        for row in rows:
            record = json.loads(row["data"] if isinstance(row, sqlite3.Row) else row[0])
            if record.get("kind") == "approval_request" and record.get("principal") != self.principal:
                continue
            if record.get("kind") == "approval_grant" and record.get("requester_principal") != self.principal and record.get("issued_by") != self.principal:
                continue
            visible.append({key: record.get(key) for key in (
                "id", "kind", "status", "skill", "workflow", "workflow_digest", "inputs_digest",
                "max_requests", "requests_used", "expires_at", "revoked", "request_id", "issued_by", "requested_at",
            )})
        return {"approvals": visible, "total_returned": len(visible)}

    # ---------------------------------------------------------------- budgets

    def set_budget(self, program, campaign, max_requests, max_concurrent=None, min_interval_ms=0):
        if type(max_requests) is not int or not 1 <= max_requests <= 1000000:
            raise InterfaceError("invalid_input", "max_requests must be 1-1000000")
        if max_concurrent is not None and (type(max_concurrent) is not int or not 1 <= max_concurrent <= 10000):
            raise InterfaceError("invalid_input", "max_concurrent must be 1-10000")
        if type(min_interval_ms) is not int or not 0 <= min_interval_ms <= 3600000:
            raise InterfaceError("invalid_input", "min_interval_ms must be 0-3600000")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "INSERT INTO budgets VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(program,campaign) DO UPDATE SET max_requests=excluded.max_requests,"
                "max_concurrent=excluded.max_concurrent,min_interval_ms=excluded.min_interval_ms,updated_at=excluded.updated_at",
                (program, campaign, max_requests, 0, max_concurrent, min_interval_ms, None, now()),
            )
            self.audit("configured", program, campaign, {"max_requests": max_requests, "max_concurrent": max_concurrent, "min_interval_ms": min_interval_ms})
        return self.budget_status(program, campaign)

    def budget_status(self, program, campaign):
        row = self.db.execute(
            "SELECT max_requests,requests_used,max_concurrent,min_interval_ms,last_dispatch_at FROM budgets WHERE program=? AND campaign=?",
            (program, campaign),
        ).fetchone()
        if not row:
            return {"configured": False, "program_id": program, "campaign_id": campaign}
        return {
            "configured": True,
            "program_id": program,
            "campaign_id": campaign,
            "max_requests": row["max_requests"],
            "requests_used": row["requests_used"],
            "requests_remaining": row["max_requests"] - row["requests_used"],
            "max_concurrent": row["max_concurrent"],
            "min_interval_ms": row["min_interval_ms"],
            "last_dispatch_at": row["last_dispatch_at"],
        }

    def budget_row(self, program, campaign):
        row = self.db.execute(
            "SELECT max_requests,requests_used,max_concurrent,min_interval_ms,last_dispatch_at FROM budgets WHERE program=? AND campaign=?",
            (program, campaign),
        ).fetchone()
        return dict(row) if row else None

    def running_count(self, program, campaign):
        return self.db.execute(
            "SELECT count(*) FROM tasks WHERE program=? AND campaign=? AND json_extract(result,'$.status')='running'",
            (program, campaign),
        ).fetchone()[0]

    def enforce_concurrency(self, context):
        budget = self.budget_row(context["program_id"], context["campaign_id"])
        if budget and budget["max_concurrent"] is not None and self.running_count(context["program_id"], context["campaign_id"]) >= budget["max_concurrent"]:
            raise InterfaceError("budget_exhaustion", "Campaign concurrency budget exhausted")

    # ------------------------------------------------------------- dispatch

    def _scope_rules(self, context):
        row = self.db.execute(
            "SELECT rules FROM scopes WHERE program=? AND campaign=? AND principal=? AND scope_digest=?",
            (context["program_id"], context["campaign_id"], self.principal, context["scope_digest"]),
        ).fetchone()
        if not row:
            raise InterfaceError("scope_denial", "No registered scope rules for this context")
        rules = json.loads(row["rules"] if not isinstance(row, sqlite3.Row) else row[0])
        if rules.get("schema_version") != "1.0" or not isinstance(rules.get("allow"), list):
            raise InterfaceError("internal_failure", "Stored scope rules are unreadable")
        return rules

    @staticmethod
    def _grant_live(grant, context, session_principal):
        if grant.get("kind") != "approval_grant" or grant.get("status") != "active" or grant.get("revoked"):
            return "grant is not active"
        if grant.get("expires_at", 0) <= time.time():
            return "grant expired"
        if grant.get("requester_principal") != session_principal:
            return "grant belongs to another principal"
        for key in ("program_id", "campaign_id", "scope_digest", "policy_digest"):
            if grant.get(key) != context.get(key):
                return "grant context binding changed"
        if grant.get("context_revision") != context.get("context_revision"):
            return "grant context revision is stale"
        if grant.get("requests_used", 0) >= grant.get("max_requests", 0):
            return "grant request budget exhausted"
        return None

    def authorize(self, context, session, grant_id, url, method):
        """Per-request boundary decision. Audits proposed + approved/denied atomically."""
        if not isinstance(grant_id, str) or not grant_id or not isinstance(url, str) or not isinstance(method, str):
            raise InterfaceError("invalid_input", "grant_id, url and method are required")
        method = method.strip().upper()
        grant = self._approval_row(grant_id)
        if not grant or grant.get("kind") != "approval_grant":
            raise InterfaceError("scope_denial", "Unknown approval grant")
        proposal = {"grant_id": grant_id, "target": scope.safe_target(url), "method": method}
        reason = self._grant_live(grant, context, self.principal)
        budget = self.budget_row(context["program_id"], context["campaign_id"]) if reason is None else None
        decision = None
        if reason is None:
            if budget is None:
                reason = "no aggregate budget configured for this campaign"
            elif grant["requests_used"] >= grant["max_requests"]:
                reason = "grant request budget exhausted"
            elif grant["requests_used"] >= grant["max_requests"] or budget["requests_used"] >= budget["max_requests"]:
                reason = "request budget exhausted"
            elif method not in grant.get("allowed_methods", []):
                reason = "method is not permitted by the grant"
            elif budget["min_interval_ms"] and budget["last_dispatch_at"] is not None and time.time() - budget["last_dispatch_at"] < budget["min_interval_ms"] / 1000:
                reason = "rate limit interval has not elapsed"
            else:
                try:
                    rules = self._scope_rules(context)
                except InterfaceError as exc:
                    reason = exc.message
                else:
                    decision = scope.check_url(rules, url)
                    if not decision["allowed"]:
                        reason = decision["reason"]
        allowed = reason is None
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            fresh = self._approval_row(grant_id)
            if allowed:
                # Re-verify mutable state inside the write transaction.
                stale = self._grant_live(fresh, context, self.principal) if fresh else "grant disappeared"
                current = self.budget_row(context["program_id"], context["campaign_id"])
                if stale is None and (current is None or current["requests_used"] >= current["max_requests"]):
                    stale = "request budget exhausted"
                if stale is None and current["min_interval_ms"] and current["last_dispatch_at"] is not None and time.time() - current["last_dispatch_at"] < current["min_interval_ms"] / 1000:
                    stale = "rate limit interval has not elapsed"
                if stale is not None:
                    allowed, reason = False, stale
            if allowed:
                self.db.execute(
                    "UPDATE approvals SET data=? WHERE id=?",
                    (json.dumps({**fresh, "requests_used": fresh["requests_used"] + 1}, sort_keys=True), grant_id),
                )
                self.db.execute(
                    "UPDATE budgets SET requests_used=requests_used+1,last_dispatch_at=? WHERE program=? AND campaign=?",
                    (time.time(), context["program_id"], context["campaign_id"]),
                )
            self.audit("approved" if allowed else "denied", context["program_id"], context["campaign_id"], {**proposal, "reason": reason or "in scope"})
        return {"allowed": allowed, "reason": reason or "in scope", "grant_id": grant_id, "addresses": (decision or {}).get("addresses", [])}

    # ------------------------------------------------------------ submission

    def grant_submission(self, finding, report_sha256, ttl_seconds):
        """Operator transport only: human grant bound to the exact report hash."""
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
            raise InterfaceError("invalid_input", "Submission grant TTL must be 1-86400 seconds")
        if not isinstance(report_sha256, str) or len(report_sha256) != 64:
            raise InterfaceError("invalid_input", "Report digest required")
        grant = {
            "schema_version": "1.0",
            "id": str(uuid.uuid4()),
            "kind": "submission_grant",
            "status": "active",
            "finding_id": finding["id"],
            "program_id": finding["program_id"],
            "campaign_id": finding["campaign_id"],
            "submitter_principal": finding["author_principal"],
            "finding_revision": finding["revision"],
            "report_sha256": report_sha256,
            "issued_by": self.principal,
            "issued_at": now(),
            "expires_at": time.time() + ttl_seconds,
            "consumed": False,
            "revoked": False,
        }
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("INSERT INTO approvals VALUES(?,?,?,?)", (grant["id"], "submission_grant", "active", json.dumps(grant, sort_keys=True)))
            self.audit("approved", grant["program_id"], grant["campaign_id"], {"submission_grant_id": grant["id"], "finding_id": finding["id"], "report_sha256": report_sha256})
        return grant

    def revoke_submission(self, grant_id, reason):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1024:
            raise InterfaceError("invalid_input", "Revocation reason required")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            grant = self._approval_row(grant_id)
            if not grant or grant.get("kind") != "submission_grant":
                raise InterfaceError("invalid_input", "Unknown submission grant")
            if grant.get("revoked"):
                raise InterfaceError("invalid_input", "Grant already revoked")
            grant.update(status="revoked", revoked=True, revoked_at=now(), revocation_reason=reason[:256])
            self.db.execute("UPDATE approvals SET status=?,data=? WHERE id=?", ("revoked", json.dumps(grant, sort_keys=True), grant_id))
            self.audit("revoked", grant["program_id"], grant["campaign_id"], {"submission_grant_id": grant_id, "reason": reason[:256]})
        return {"revoked": True, "grant_id": grant_id}

    def submit_report(self, context, session, finding, report):
        """Authorize one report submission against a single-use human grant."""
        sha = report_digest(report)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            reason = None
            grant = None
            for row in self.db.execute(
                "SELECT data FROM approvals WHERE json_extract(data,'$.kind')='submission_grant' AND json_extract(data,'$.finding_id')=?",
                (finding["id"],),
            ):
                candidate = json.loads(row["data"] if isinstance(row, sqlite3.Row) else row[0])
                if (
                    candidate.get("status") == "active"
                    and not candidate.get("revoked")
                    and not candidate.get("consumed")
                    and candidate.get("expires_at", 0) > time.time()
                    and candidate.get("report_sha256") == sha
                    and candidate.get("submitter_principal") == self.principal
                    and candidate.get("finding_revision") == finding["revision"]
                    and candidate.get("program_id") == context["program_id"]
                    and candidate.get("campaign_id") == context["campaign_id"]
                ):
                    grant = candidate
                    break
                if candidate.get("report_sha256") == sha and candidate.get("submitter_principal") == self.principal:
                    reason = reason or "matching grant is expired, revoked, consumed or stale"
            if grant is None and reason is None:
                reason = "no human submission grant matches this report"
            if grant is not None:
                grant["consumed"] = True
                grant["consumed_at"] = now()
                self.db.execute("UPDATE approvals SET status=?,data=? WHERE id=?", ("consumed", json.dumps(grant, sort_keys=True), grant["id"]))
                submission = {
                    "schema_version": "1.0",
                    "id": str(uuid.uuid4()),
                    "finding_id": finding["id"],
                    "grant_id": grant["id"],
                    "program_id": context["program_id"],
                    "campaign_id": context["campaign_id"],
                    "principal": self.principal,
                    "report_sha256": sha,
                    "delivered": False,
                    "submitted_at": now(),
                }
                self.db.execute("INSERT INTO submissions VALUES(?,?,?)", (submission["id"], finding["id"], json.dumps(submission, sort_keys=True)))
                self.audit("submitted", context["program_id"], context["campaign_id"], {"submission_id": submission["id"], "finding_id": finding["id"], "report_sha256": sha})
                return {"submitted": True, "submission": submission, "delivered": False}
            self.audit("denied", context["program_id"], context["campaign_id"], {"finding_id": finding["id"], "report_sha256": sha, "reason": reason})
            return {"submitted": False, "reason": reason}
