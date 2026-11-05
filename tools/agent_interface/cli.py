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
        if args.command in ("queue-adjust", "queue-dispose"):
            params = {"session_id": args.session_id, "task_id": args.task_id, "expected_revision": args.expected_revision, "reason": args.reason}
            if args.command == "queue-adjust":
                params.update(position=args.position, deferred=args.deferred)
            result = service.planning(params, "operator.adjust" if args.command == "queue-adjust" else "operator.dispose", operator=True)
        elif args.command == "register":
            scope = args.scope_file.read_bytes()
            if not scope.strip() or len(scope) > MAX_MESSAGE:
                raise InterfaceError("invalid_input", "Scope snapshot must be nonempty and at most 64 KiB")
            result = service.register(args.program_id, args.campaign_id, hashlib.sha256(scope).hexdigest())
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
