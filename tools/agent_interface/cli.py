"""One bounded JSON request per invocation; no service identity in request JSON."""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from .catalog import ROOT, capabilities, catalog
from .contracts import MAX_MESSAGE, InterfaceError, decode, validate_named
from .service import Service, fields


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / ".bb/agent-interface.sqlite3", help="Local state database (operator transport configuration)")
    parser.add_argument(
        "--data-root", type=Path, default=Path(os.environ["BB_DATA_ROOT"]) if os.environ.get("BB_DATA_ROOT") else None, help="Private local data directory; selects interface and memory databases"
    )
    sub = parser.add_subparsers(dest="command")
    register = sub.add_parser("register", help="Operator: register an offline program/campaign scope snapshot")
    register.add_argument("--program-id", required=True)
    register.add_argument("--campaign-id", required=True)
    register.add_argument("--scope-file", required=True, type=Path)
    for command in ("queue-adjust", "queue-dispose"):
        control = sub.add_parser(command, help="Operator: reorder/defer or dispose research work without granting execution/review")
        control.add_argument("--session-id", required=True)
        control.add_argument("--task-id", required=True)
        control.add_argument("--expected-revision", required=True, type=int)
        control.add_argument("--reason", required=True)
        if command == "queue-adjust":
            control.add_argument("--position", required=True, type=int)
            control.add_argument("--deferred", action=argparse.BooleanOptionalAction, default=False)
    evidence = sub.add_parser("evidence-import", help="Operator: import a bounded immutable evidence bundle")
    evidence.add_argument("--session-id", required=True)
    evidence.add_argument("--finding-id", required=True)
    evidence.add_argument("--expected-revision", required=True, type=int)
    evidence.add_argument("--manifest", required=True, type=Path)
    evidence.add_argument("--evidence-root", required=True, type=Path)
    legacy = sub.add_parser("legacy-import", help="Operator: import one legacy JSON finding as an unreviewed claim")
    legacy.add_argument("--session-id", required=True)
    legacy.add_argument("--hypothesis-id", required=True)
    legacy.add_argument("--profile", required=True)
    legacy.add_argument("--claim", required=True)
    legacy.add_argument("--impact-class", required=True)
    legacy.add_argument("--file", required=True, type=Path)
    assign = sub.add_parser("review-assign", help="Operator: grant another authenticated principal a read-only review snapshot")
    assign.add_argument("--session-id", required=True)
    assign.add_argument("--finding-id", required=True)
    assign.add_argument("--reviewer-principal", required=True)
    assign.add_argument("--ttl", type=int, default=3600)
    assign.add_argument("--reason", required=True)
    revoke = sub.add_parser("review-revoke", help="Operator: revoke a read-only review grant")
    revoke.add_argument("--session-id", required=True)
    revoke.add_argument("--grant-id", required=True)
    revoke.add_argument("--reason", required=True)
    human = sub.add_parser("review-human", help="Operator: record a human decision; never grants submission")
    human.add_argument("--review-session-id", required=True)
    human.add_argument("--expected-revision", required=True, type=int)
    human.add_argument("--assessment", required=True, type=Path)
    human.add_argument("--decision", required=True, choices=["confirm", "reject", "needs_evidence"])
    human.add_argument("--reason", required=True)
    approval = sub.add_parser("approval-grant", help="Operator: issue an execution grant bound to an approval request")
    approval.add_argument("--request-id", required=True)
    approval.add_argument("--ttl", type=int, default=3600)
    approval.add_argument("--max-requests", type=int, default=None)
    approval_revoke = sub.add_parser("approval-revoke", help="Operator: revoke an execution grant")
    approval_revoke.add_argument("--grant-id", required=True)
    approval_revoke.add_argument("--reason", required=True)
    budget = sub.add_parser("budget-set", help="Operator: set aggregate request/concurrency/rate budgets for a campaign")
    budget.add_argument("--program-id", required=True)
    budget.add_argument("--campaign-id", required=True)
    budget.add_argument("--max-requests", required=True, type=int)
    budget.add_argument("--max-concurrent", type=int, default=None)
    budget.add_argument("--min-interval-ms", type=int, default=0)
    subgrant = sub.add_parser("submission-grant", help="Operator: grant submission of one exact report hash for a finding")
    subgrant.add_argument("--finding-id", required=True)
    subgrant.add_argument("--report-file", required=True, type=Path)
    subgrant.add_argument("--ttl", type=int, default=3600)
    subrevoke = sub.add_parser("submission-revoke", help="Operator: revoke a submission grant")
    subrevoke.add_argument("--grant-id", required=True)
    subrevoke.add_argument("--reason", required=True)
    audit = sub.add_parser("audit-list", help="Operator: list redacted audit events")
    audit.add_argument("--program-id", default=None)
    audit.add_argument("--limit", type=int, default=50)
    sub.add_parser("audit-verify", help="Operator: verify the audit hash chain")
    args = parser.parse_args()
    if args.data_root:
        if not args.data_root.is_absolute() or args.data_root.resolve().is_relative_to(ROOT):
            parser.error("--data-root must be absolute and outside the repository")
        args.database = args.data_root / "agent-interface.sqlite3"
    service = None
    try:
        request = None
        if args.command is None:
            request = decode(sys.stdin.buffer.read(MAX_MESSAGE + 1))
            validate_named(request, "request")
            if request["operation"] in ("capabilities", "discover"):
                fields(request["params"], [])
                print(json.dumps(capabilities() if request["operation"] == "capabilities" else catalog(), sort_keys=True))
                return 0
        service = Service(args.database, f"uid:{os.getuid()}")
        if args.command in ("evidence-import", "legacy-import", "review-assign", "review-revoke", "review-human"):

            def document(path):
                with path.open("rb") as stream:
                    return decode(stream.read(MAX_MESSAGE + 1))

            if args.command == "evidence-import":
                result = service.verification(
                    {"session_id": args.session_id, "finding_id": args.finding_id, "expected_revision": args.expected_revision, "manifest": document(args.manifest)},
                    "operator.evidence",
                    operator=True,
                    evidence_root=args.evidence_root,
                )
            elif args.command == "legacy-import":
                original = document(args.file)
                result = service.verification(
                    {
                        "session_id": args.session_id,
                        "hypothesis_id": args.hypothesis_id,
                        "profile": args.profile,
                        "claim": args.claim,
                        "impact_class": args.impact_class,
                        "legacy": {"original": original, "sha256": hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()},
                    },
                    "operator.legacy",
                    operator=True,
                )
            elif args.command == "review-assign":
                result = service.verification(
                    {"session_id": args.session_id, "finding_id": args.finding_id, "reviewer_principal": args.reviewer_principal, "ttl": args.ttl, "reason": args.reason},
                    "operator.review_assign",
                    operator=True,
                )
            elif args.command == "review-revoke":
                result = service.verification({"session_id": args.session_id, "grant_id": args.grant_id, "reason": args.reason}, "operator.review_revoke", operator=True)
            else:
                result = service.verification(
                    {
                        "review_session_id": args.review_session_id,
                        "expected_revision": args.expected_revision,
                        "assessment": document(args.assessment),
                        "decision": args.decision,
                        "reason": args.reason,
                    },
                    "operator.human_review",
                    operator=True,
                )
        elif args.command in ("queue-adjust", "queue-dispose"):
            params = {"session_id": args.session_id, "task_id": args.task_id, "expected_revision": args.expected_revision, "reason": args.reason}
            if args.command == "queue-adjust":
                params.update(position=args.position, deferred=args.deferred)
            result = service.planning(params, "operator.adjust" if args.command == "queue-adjust" else "operator.dispose", operator=True)
        elif args.command == "register":
            scope = args.scope_file.read_bytes()
            if not scope.strip() or len(scope) > MAX_MESSAGE:
                raise InterfaceError("invalid_input", "Scope snapshot must be nonempty and at most 64 KiB")
            try:
                scope_text = scope.decode("utf-8")
            except UnicodeDecodeError:
                raise InterfaceError("invalid_input", "Scope snapshot must be UTF-8") from None
            result = service.register(args.program_id, args.campaign_id, hashlib.sha256(scope).hexdigest(), scope_text=scope_text)
        elif args.command == "approval-grant":
            result = service.control().grant_approval(args.request_id, args.ttl, args.max_requests)
        elif args.command == "approval-revoke":
            result = service.control().revoke_approval(args.grant_id, args.reason)
        elif args.command == "budget-set":
            result = service.control().set_budget(args.program_id, args.campaign_id, args.max_requests, args.max_concurrent, args.min_interval_ms)
        elif args.command == "submission-grant":
            if not args.report_file.is_file():
                raise InterfaceError("invalid_input", "Report file is required")
            with args.report_file.open("rb") as stream:
                report_raw = stream.read(MAX_MESSAGE + 1)
            result = service.operator_submission_grant(args.finding_id, report_raw, args.ttl)
        elif args.command == "submission-revoke":
            result = service.control().revoke_submission(args.grant_id, args.reason)
        elif args.command == "audit-list":
            result = service.control().audit_list(program=args.program_id, limit=args.limit)
        elif args.command == "audit-verify":
            result = service.control().audit_verify()
        else:
            result = service.handle(request)
        print(json.dumps(result, sort_keys=True))
        return 1 if result.get("status") in ("denied", "failed", "interrupted") else 0
    except InterfaceError as exc:
        print(json.dumps({"schema_version": "1.0", "error": {"code": exc.code, "message": exc.message}}))
        return 2
    except Exception:
        print(json.dumps({"schema_version": "1.0", "error": {"code": "internal_failure", "message": "Local interface unavailable"}}))
        return 2
    finally:
        if service:
            service.db.close()
