"""Durable local sessions and tasks. Local identity is established outside JSON."""

import hashlib
import importlib.util
import json
import sqlite3
import time
import uuid
from datetime import UTC, datetime

from tools.execution_control import scope as scope_rules
from tools.execution_control.enforcement import Control, report_digest
from tools.execution_control.offline import execute

from .catalog import PROVIDERS, ROOT, capabilities, catalog
from .contracts import InterfaceError, decode, digest, validate, validate_named

POLICY_DIGEST = digest({"version": 1, "mode": "offline", "handler": "standards-search-v1"})
ID = {"type": "string", "pattern": "[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", "maxLength": 128}


def now():
    return datetime.now(UTC).isoformat()


def fields(value, required, optional=()):
    validate(value, {"type": "object", "additionalProperties": False, "required": required, "properties": {k: ID for k in [*required, *optional]}})


class Service:
    def __init__(self, database, principal, root=ROOT):
        self.root, self.principal = root, principal
        self.memory_path = database.parent / "memory.sqlite"
        database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if database.is_symlink():
            raise InterfaceError("invalid_input", "Database symlinks are unsupported")
        self.db = sqlite3.connect(database, timeout=10)
        database.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS contexts (
                program TEXT, campaign TEXT, principal TEXT, revision INTEGER,
                scope TEXT, policy TEXT, PRIMARY KEY(program,campaign));
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, principal TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, key TEXT, principal TEXT, program TEXT, campaign TEXT,
                payload_hash TEXT, result TEXT, UNIQUE(principal,program,campaign,key));
            CREATE TABLE IF NOT EXISTS task_leases (task TEXT PRIMARY KEY, expires REAL);
            CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, task TEXT, data BLOB);
            CREATE TABLE IF NOT EXISTS scopes (program TEXT, campaign TEXT, principal TEXT, scope_digest TEXT, rules TEXT, PRIMARY KEY(program,campaign,principal));
            CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, kind TEXT, status TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS budgets (
                program TEXT, campaign TEXT, max_requests INTEGER, requests_used INTEGER,
                max_concurrent INTEGER, min_interval_ms INTEGER, last_dispatch_at REAL,
                updated_at TEXT, PRIMARY KEY(program,campaign));
            CREATE TABLE IF NOT EXISTS audit_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS submissions (id TEXT PRIMARY KEY, finding TEXT, data TEXT);
            CREATE TRIGGER IF NOT EXISTS protect_audit_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT, 'immutable audit history'); END;
            CREATE TRIGGER IF NOT EXISTS protect_audit_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT, 'immutable audit history'); END;
        """)

    def control(self):
        return Control(self.db, self.principal)

    def recover_expired(self):
        with self.db:
            rows = self.db.execute(
                "SELECT tasks.id,tasks.result,tasks.principal,tasks.program,tasks.campaign"
                " FROM tasks JOIN task_leases ON tasks.id=task_leases.task WHERE task_leases.expires<?",
                (time.time(),),
            ).fetchall()
            for row in rows:
                result = json.loads(row["result"])
                if result["status"] == "running":
                    result.update(status="interrupted", ended_at=now(), error={"code": "interrupted", "message": "Worker lease expired; reconcile artifacts before proposing a new task"})
                    self.save_result(result)
                    Control(self.db, row["principal"]).audit(
                        "interrupted", row["program"], row["campaign"], {"task_id": row["id"], "reason": "worker lease expired"}, task_id=row["id"], principal=row["principal"]
                    )
                self.db.execute("DELETE FROM task_leases WHERE task=?", (row["id"],))

    def register(self, program, campaign, scope_digest, scope_text=None):
        """Operator CLI only. Explicit scope snapshot import, never a JSON operation."""
        fields({"program": program, "campaign": campaign}, ["program", "campaign"])
        rules = None
        if scope_text is not None:
            try:
                rules = {"schema_version": "1.0", **scope_rules.parse(scope_text)}
            except ValueError as exc:
                raise InterfaceError("invalid_input", f"Scope snapshot rejected: {exc}") from None
        with self.db:
            row = self.db.execute("SELECT * FROM contexts WHERE program=? AND campaign=?", (program, campaign)).fetchone()
            if row and row["principal"] != self.principal:
                raise InterfaceError("scope_denial", "Context belongs to another principal")
            revision = row["revision"] if row else 1
            if row and (row["scope"] != scope_digest or row["policy"] != POLICY_DIGEST):
                revision += 1
            self.db.execute("INSERT OR REPLACE INTO contexts VALUES(?,?,?,?,?,?)", (program, campaign, self.principal, revision, scope_digest, POLICY_DIGEST))
            if rules is not None:
                self.db.execute(
                    "INSERT OR REPLACE INTO scopes VALUES(?,?,?,?,?)",
                    (program, campaign, self.principal, scope_digest, json.dumps(rules, sort_keys=True)),
                )
                self.control().audit("configured", program, campaign, {"scope_registered": True, "scope_digest": scope_digest})
        return self.context(program, campaign)

    def context(self, program, campaign):
        row = self.db.execute("SELECT * FROM contexts WHERE program=? AND campaign=? AND principal=?", (program, campaign, self.principal)).fetchone()
        if not row:
            raise InterfaceError("scope_denial", "Unknown or inaccessible program/campaign")
        return {"program_id": program, "campaign_id": campaign, "context_revision": row["revision"], "scope_digest": row["scope"], "policy_digest": row["policy"]}

    def session(self, sid):
        row = self.db.execute("SELECT data FROM sessions WHERE id=? AND principal=?", (sid, self.principal)).fetchone()
        if not row:
            raise InterfaceError("scope_denial", "Unknown or inaccessible session")
        return json.loads(row["data"])

    def open_session(self, params):
        fields(params, ["program_id", "campaign_id", "provider", "client_version"], ["predecessor_session_id"])
        if params["provider"] not in PROVIDERS:
            raise InterfaceError("unsupported_capability", "Unknown provider")
        context = self.context(params["program_id"], params["campaign_id"])
        predecessor = params.get("predecessor_session_id")
        if predecessor:
            old = self.session(predecessor)
            if any(old[k] != params[k] for k in ("program_id", "campaign_id")):
                raise InterfaceError("scope_denial", "Predecessor belongs to another campaign")
        stamp = now()
        session = {
            "schema_version": "1.0",
            "session_id": str(uuid.uuid4()),
            **params,
            "principal": self.principal,
            "created_at": stamp,
            "last_seen": stamp,
            "context_revision": context["context_revision"],
            "predecessor_session_id": predecessor,
        }
        validate_named(session, "session")
        with self.db:
            self.db.execute("INSERT INTO sessions VALUES(?,?,?)", (session["session_id"], self.principal, json.dumps(session)))
        return {"session": session, "context": context}

    def memory(self):
        import importlib.util

        path = self.root / ".claude/skills/program-memory/scripts/research_store.py"
        spec = importlib.util.spec_from_file_location("bh_research", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, module.ResearchStore(self.memory_path)

    def planning(self, params, operation, operator=False):
        import importlib.util

        definitions = {
            "plan.generate": ({"session_id"}, {"proposals", "weights"}),
            "plan.get": ({"session_id", "plan_id"}, set()),
            "queue.list": ({"session_id"}, set()),
            "queue.history": ({"session_id", "task_id"}, set()),
            "queue.transition": ({"session_id", "task_id", "expected_revision", "state", "reason"}, set()),
            "queue.claim": ({"session_id", "task_id", "expected_revision"}, {"ttl"}),
            "queue.heartbeat": ({"session_id", "task_id", "expected_revision", "lease_token"}, {"ttl"}),
            "queue.dependencies": ({"session_id", "task_id", "expected_revision", "dependencies", "reason"}, set()),
            "queue.stop": ({"session_id", "task_id", "expected_revision", "lease_token", "state", "reason"}, set()),
            "queue.complete": ({"session_id", "task_id", "expected_revision", "lease_token", "result", "verdict"}, set()),
            "campaign.evaluate": ({"session_id"}, set()),
            "operator.adjust": ({"session_id", "task_id", "expected_revision", "position", "deferred", "reason"}, set()),
            "operator.dispose": ({"session_id", "task_id", "expected_revision", "reason"}, set()),
        }
        required, optional = definitions[operation]
        if not isinstance(params, dict) or not required <= params.keys() or params.keys() - required - optional:
            raise InterfaceError("invalid_input", "Unknown or missing planning fields")
        if operation == "plan.generate" and "proposals" in params:
            schema = json.loads((self.root / "tools/schemas/research-opportunity.schema.json").read_text())
            if not isinstance(params["proposals"], list):
                raise InterfaceError("invalid_input", "Proposals must be an array")
            for proposal in params["proposals"]:
                validate(proposal, schema)
        if operation.startswith("operator.") and not operator:
            raise InterfaceError("scope_denial", "Operator transport required")
        properties = {k: ID for k in ("session_id", "task_id", "plan_id", "lease_token")}
        properties.update({k: {"type": "integer", "minimum": 1} for k in ("expected_revision", "ttl")})
        properties.update({k: {"type": "string", "minLength": 1, "maxLength": 8192} for k in ("state", "reason", "result", "verdict")})
        properties.update(
            proposals={"type": "array", "maxItems": 100, "items": {"type": "object"}},
            weights={"type": "object"},
            position={"type": "integer", "minimum": 0},
            deferred={"type": "boolean"},
            dependencies={"type": "array", "maxItems": 32, "items": ID},
        )
        validate(params, {"type": "object", "additionalProperties": False, "required": list(required), "properties": {k: properties[k] for k in required | optional}})
        session = self.session(params["session_id"])
        context = self.context(session["program_id"], session["campaign_id"])
        if session["context_revision"] != context["context_revision"]:
            raise InterfaceError("context_conflict", "Session context is stale")
        module, store = self.memory()
        path = self.root / ".claude/skills/planner/scripts/research_planner.py"
        spec = importlib.util.spec_from_file_location("bh_planning", path)
        planning = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(planning)
        provenance = {
            "actor": self.principal,
            "session_id": session["session_id"],
            "campaign_id": session["campaign_id"],
            "provider": session["provider"],
            "client_version": session["client_version"],
            **{k: context[k] for k in ("context_revision", "scope_digest", "policy_digest")},
        }
        planner = planning.Planner(store, session["program_id"], session["campaign_id"], provenance, catalog(self.root)["workflows"] if operation == "plan.generate" else ())
        try:
            if operation == "plan.generate":
                return planner.generate(params.get("proposals"), params.get("weights"))
            if operation == "plan.get":
                return planner.plan(params["plan_id"])
            if operation == "queue.list":
                return {"tasks": planner.listing()}
            if operation == "queue.history":
                return {"events": planner.history(params["task_id"])}
            if operation == "queue.transition":
                return planner.transition(params["task_id"], params["expected_revision"], params["state"], params["reason"])
            if operation == "queue.claim":
                return planner.claim(params["task_id"], params["expected_revision"], params.get("ttl", 30))
            if operation == "queue.heartbeat":
                return planner.heartbeat(params["task_id"], params["expected_revision"], params["lease_token"], params.get("ttl", 30))
            if operation == "queue.dependencies":
                return planner.dependencies(params["task_id"], params["expected_revision"], params["dependencies"], params["reason"])
            if operation == "queue.stop":
                return planner.stop(params["task_id"], params["expected_revision"], params["lease_token"], params["state"], params["reason"])
            if operation == "queue.complete":
                return planner.complete(params["task_id"], params["expected_revision"], params["lease_token"], params["result"], params["verdict"])
            if operation == "operator.adjust":
                return planner.adjust(params["task_id"], params["expected_revision"], params["position"], params["deferred"], params["reason"])
            if operation == "operator.dispose":
                return planner.dispose(params["task_id"], params["expected_revision"], params["reason"])
            path = self.root / ".claude/skills/campaign/scripts/completion_gate.py"
            spec = importlib.util.spec_from_file_location("bh_completion", path)
            gate = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(gate)
            planner.listing()
            with store.db:
                store.db.execute("BEGIN")
                assessment = gate.evaluate(store, session["program_id"], session["campaign_id"], planner.rows(), review_context=context)
            return assessment
        except planning.PlanningConflict:
            raise InterfaceError("context_conflict", "Queue revision or research inputs changed") from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise InterfaceError("invalid_input", "Invalid planning input, stale revision, or unavailable operation") from None
        finally:
            store.db.close()

    def verification(self, params, operation, operator=False, evidence_root=None):
        import base64
        import importlib.util

        definitions = {
            "finding.create": ({"session_id", "hypothesis_id", "claim", "impact_class", "profile"}, set()),
            "finding.list": ({"session_id"}, {"offset"}),
            "evidence.manifest": ({"session_id", "finding_id"}, {"bundle_id"}),
            "evidence.read": ({"session_id", "finding_id", "artifact_id"}, {"bundle_id"}),
            "finding.get": ({"session_id", "finding_id"}, set()),
            "finding.history": ({"session_id", "finding_id"}, set()),
            "operator.evidence": ({"session_id", "finding_id", "expected_revision", "manifest"}, set()),
            "operator.legacy": ({"session_id", "hypothesis_id", "claim", "impact_class", "profile", "legacy"}, set()),
            "operator.review_assign": ({"session_id", "finding_id", "reviewer_principal", "ttl", "reason"}, set()),
            "operator.review_revoke": ({"session_id", "grant_id", "reason"}, set()),
            "review.open": ({"grant_id", "provider", "client_version"}, set()),
            "review.snapshot": ({"review_session_id"}, set()),
            "review.artifact": ({"review_session_id", "artifact_id"}, set()),
            "review.record": ({"review_session_id", "expected_revision", "assessment"}, set()),
            "operator.human_review": ({"review_session_id", "expected_revision", "assessment", "decision", "reason"}, set()),
        }
        required, optional = definitions[operation]
        properties = {k: ID for k in ("session_id", "finding_id", "hypothesis_id", "reviewer_principal", "grant_id", "review_session_id", "artifact_id", "provider", "client_version", "bundle_id")}
        properties.update({k: {"type": "string", "minLength": 1, "maxLength": 8192} for k in ("claim", "impact_class", "profile", "reason", "decision")})
        properties.update(
            expected_revision={"type": "integer", "minimum": 1}, ttl={"type": "integer", "minimum": 1}, manifest={"type": "object"}, assessment={"type": "object"}, legacy={"type": "object"}
        )
        properties["bundle_id"] = ID
        properties["offset"] = {"type": "integer", "minimum": 0}
        validate(params, {"type": "object", "required": list(required), "additionalProperties": False, "properties": {k: properties[k] for k in required | optional}})
        if operation.startswith("operator.") and not operator:
            raise InterfaceError("scope_denial", "Operator transport required")
        if "assessment" in params:
            validate(params["assessment"], json.loads((self.root / "tools/schemas/evidence-review.schema.json").read_text()))
        module, store = self.memory()
        path = self.root / ".claude/skills/impact-verifier/scripts/review_store.py"
        spec = importlib.util.spec_from_file_location("bh_reviews", path)
        review_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(review_module)
        reviews = review_module.Reviews(store, self.principal)

        def current_context(program, campaign):
            row = self.db.execute("SELECT * FROM contexts WHERE program=? AND campaign=?", (program, campaign)).fetchone()
            if not row:
                raise ValueError("Review context missing")
            return {"program_id": program, "campaign_id": campaign, "context_revision": row["revision"], "scope_digest": row["scope"], "policy_digest": row["policy"]}

        try:
            if operation == "review.open":
                if params["provider"] not in PROVIDERS:
                    raise ValueError("Unknown reviewer provider")
                return reviews.open(params["grant_id"], params["provider"], params["client_version"])
            if "review_session_id" in params:
                sid = params["review_session_id"]
                if operation in ("review.record", "operator.human_review"):
                    return reviews.record(
                        sid,
                        params["expected_revision"],
                        params["assessment"],
                        current_context,
                        human=operation == "operator.human_review",
                        decision=params.get("decision"),
                        reason=params.get("reason"),
                    )
                if operation == "review.snapshot":
                    return reviews.snapshot(sid, current_context)
                session, grant, candidate, bundle = reviews.reviewer(sid, current_context)
                artifact, data = reviews.artifact_bytes(bundle, params["artifact_id"])
                return {"artifact": artifact, "encoding": "base64", "data": base64.b64encode(data).decode(), "executable": False}
            session = self.session(params["session_id"])
            context = self.context(session["program_id"], session["campaign_id"])
            if session["context_revision"] != context["context_revision"]:
                raise review_module.ReviewConflict("Session context changed")
            if operation in ("finding.create", "operator.legacy"):
                return reviews.create(context, session, params["hypothesis_id"], params["claim"], params["impact_class"], params["profile"], params.get("legacy"))
            if operation == "operator.review_revoke":
                return reviews.revoke(params["grant_id"], context, params["reason"])
            if operation == "finding.list":
                return reviews.listing(context, params.get("offset", 0))
            candidate = reviews.owned(params["finding_id"], context, session)
            if operation in ("evidence.manifest", "evidence.read"):
                selected = {**candidate, "bundle_id": params.get("bundle_id", candidate["bundle_id"])}
                bundle = reviews.bundle(selected)
                if operation == "evidence.manifest":
                    return {"manifest": bundle, "trust": "untrusted_evidence", "submission_approved": False}
                artifact, data = reviews.artifact_bytes(bundle, params["artifact_id"])
                return {"artifact": artifact, "encoding": "base64", "data": base64.b64encode(data).decode(), "executable": False}
            if operation == "finding.history":
                return {"events": reviews.history(candidate["id"])}
            if operation == "finding.get":
                return reviews.status(candidate, context)
            if operation == "operator.review_assign":
                return reviews.assign(candidate, context, params["reviewer_principal"], params["ttl"], params["reason"])

            def verify_run(run_id):
                if not isinstance(run_id, str):
                    raise ValueError("Invalid source run")
                rows = self.db.execute("SELECT result FROM tasks WHERE program=? AND campaign=? AND principal=?", (context["program_id"], context["campaign_id"], self.principal))
                if not any((result := json.loads(row[0]))["run_id"] == run_id and result["status"] == "succeeded" for row in rows):
                    raise ValueError("Unknown or unsuccessful source run")

            return reviews.import_bundle(context, session, candidate["id"], params["expected_revision"], params["manifest"], evidence_root, verify_run)
        except review_module.ReviewConflict:
            raise InterfaceError("context_conflict", "Finding, evidence or context revision changed") from None
        except (ValueError, TypeError, KeyError, AttributeError, OSError):
            raise InterfaceError("invalid_input", "Invalid evidence/review input, ownership, integrity or authority") from None
        finally:
            store.db.close()

    def handoff(self, session):
        self.recover_expired()
        context = self.context(session["program_id"], session["campaign_id"])
        rows = self.db.execute(
            "SELECT result FROM tasks WHERE principal=? AND program=? AND campaign=? ORDER BY rowid DESC LIMIT 25", (self.principal, session["program_id"], session["campaign_id"])
        ).fetchall()
        tasks = [json.loads(row[0]) for row in rows]
        module, store = self.memory()
        try:
            research = store.summary(session["program_id"])
        finally:
            store.db.close()
        queue = self.planning({"session_id": session["session_id"]}, "queue.list")["tasks"] if session["context_revision"] == context["context_revision"] else []
        return {
            "schema_version": "1.0",
            "context": context,
            "session_id": session["session_id"],
            "research_queue": {"tasks": queue[:20], "total": len(queue), "truncated_to": 20},
            "finding_review": self.verification({"session_id": session["session_id"]}, "finding.list")
            if session["context_revision"] == context["context_revision"]
            else {"findings": [], "stale_context": True},
            "sensitivity": "internal",
            "trust": "derived",
            "source_ids": [t["task_id"] for t in tasks],
            "tasks": tasks,
            "truncated_to": 25,
            "research": research,
            "active_leases": [{"task_id": t["task_id"], "status": t["status"]} for t in tasks if t["status"] == "running"],
            "restrictions": "Offline only; approvals cannot enable active dispatch",
        }

    def task(self, params):
        fields(params, ["session_id", "task_id"])
        session = self.session(params["session_id"])
        row = self.db.execute(
            "SELECT * FROM tasks WHERE id=? AND principal=? AND program=? AND campaign=?", (params["task_id"], self.principal, session["program_id"], session["campaign_id"])
        ).fetchone()
        if not row:
            raise InterfaceError("scope_denial", "Unknown or inaccessible task")
        return json.loads(row["result"])

    def save_result(self, result):
        validate_named(result, "result")
        self.db.execute("UPDATE tasks SET result=? WHERE id=?", (json.dumps(result), result["task_id"]))

    def submit(self, task):
        validate_named(task, "task")
        self.recover_expired()
        session = self.session(task["session_id"])
        if any(session[k] != task[k] for k in ("program_id", "campaign_id")):
            raise InterfaceError("scope_denial", "Session ownership mismatch")
        # Session switches preserve identity: omit session provenance from replay identity.
        payload_hash = digest({k: v for k, v in task.items() if k != "session_id"})
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old = self.db.execute(
                "SELECT * FROM tasks WHERE id=? OR (principal=? AND program=? AND campaign=? AND key=?)",
                (task["task_id"], self.principal, task["program_id"], task["campaign_id"], task["idempotency_key"]),
            ).fetchall()
            if old:
                if len(old) != 1 or old[0]["principal"] != self.principal or old[0]["payload_hash"] != payload_hash:
                    raise InterfaceError("context_conflict", "Task identity was already used with a different payload")
                self.db.commit()
                return json.loads(old[0]["result"])
            result = {
                "schema_version": "1.0",
                "task_id": task["task_id"],
                "run_id": str(uuid.uuid4()),
                "status": "queued",
                "exit_code": None,
                "error": None,
                "created_at": now(),
                "started_at": None,
                "ended_at": None,
                "budgets": {"requests": 0, "artifact_bytes": 0},
                "provenance": {
                    "principal": self.principal,
                    "session_id": session["session_id"],
                    "provider": session["provider"],
                    "client_version": session["client_version"],
                    "workflow_digest": task["workflow_digest"],
                    "scope_digest": task["scope_digest"],
                    "policy_digest": task["policy_digest"],
                },
                "artifacts": [],
                "diagnostic": "",
            }
            self.db.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,?,?,?)", (task["task_id"], task["idempotency_key"], self.principal, task["program_id"], task["campaign_id"], payload_hash, json.dumps(result))
            )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        with self.db:
            self.control().audit("proposed", task["program_id"], task["campaign_id"], {"task_id": task["task_id"], "skill": task["skill"], "workflow": task["workflow"]}, task_id=task["task_id"])
        try:
            context = self.context(task["program_id"], task["campaign_id"])
            if task["expected_context_revision"] != context["context_revision"] or any(task[k] != context[k] for k in ("scope_digest", "policy_digest")):
                raise InterfaceError("context_conflict", "Context changed; open a fresh session and reconcile")
            if session["context_revision"] != context["context_revision"]:
                raise InterfaceError("context_conflict", "Session context is stale")
            entry = next((e for e in catalog(self.root)["workflows"] if e["skill"] == task["skill"] and e["workflow"] == task["workflow"]), None)
            if entry is None:
                raise InterfaceError("invalid_input", "Unknown workflow")
            if task["workflow_digest"] != entry["workflow_digest"]:
                raise InterfaceError("context_conflict", "Workflow changed")
            if entry["execution_modes"] != ["offline"]:
                raise InterfaceError("unsupported_capability", "Constrained active execution is unavailable")
            validate(task["inputs"], entry["inputs"])
            self.control().enforce_concurrency({"program_id": task["program_id"], "campaign_id": task["campaign_id"]})
            result.update(status="running", started_at=now())
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                current = self.task({"session_id": task["session_id"], "task_id": task["task_id"]})
                if current["status"] == "cancelled":
                    return current
                latest = self.context(task["program_id"], task["campaign_id"])
                if latest != context:
                    raise InterfaceError("context_conflict", "Context changed before launch")
                self.control().enforce_concurrency({"program_id": task["program_id"], "campaign_id": task["campaign_id"]})
                self.save_result(result)
                self.db.execute("INSERT OR REPLACE INTO task_leases VALUES(?,?)", (task["task_id"], time.time() + 30))
                self.control().audit("started", task["program_id"], task["campaign_id"], {"task_id": task["task_id"], "run_id": result["run_id"]}, task_id=task["task_id"])
            data = json.dumps(execute(self.root, entry, task["inputs"]), sort_keys=True).encode()
            if len(data) > 1048576:
                raise InterfaceError("budget_exhaustion", "Offline artifact exceeds 1 MiB limit")
            artifact = {"id": str(uuid.uuid4()), "kind": "json", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            # Artifacts are opaque DB blobs: no input path or symlink can select a file.
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                current = self.task({"session_id": task["session_id"], "task_id": task["task_id"]})
                if current["status"] == "cancelled":
                    return current
                self.db.execute("INSERT INTO artifacts VALUES(?,?,?)", (artifact["id"], task["task_id"], data))
                result.update(status="succeeded", exit_code=0, artifacts=[artifact])
                result["budgets"]["artifact_bytes"] = len(data)
                result["ended_at"] = now()
                self.save_result(result)
                self.control().audit("completed", task["program_id"], task["campaign_id"], {"task_id": task["task_id"], "run_id": result["run_id"]}, task_id=task["task_id"])
            return result
        except InterfaceError as exc:
            result.update(status="denied" if result["started_at"] is None else "failed", error={"code": exc.code, "message": exc.message})
        except KeyboardInterrupt:
            result.update(status="interrupted", error={"code": "interrupted", "message": "Execution interrupted"})
        except Exception:
            # Do not serialize exception text, input data, credentials, or filesystem paths.
            result.update(status="failed", error={"code": "internal_failure", "message": "Offline execution failed"})
        result["ended_at"] = now()
        terminal = "interrupted" if result["status"] == "interrupted" else ("completed" if result["status"] == "succeeded" else result["status"])
        with self.db:
            self.save_result(result)
            self.control().audit(
                terminal, task["program_id"], task["campaign_id"],
                {"task_id": task["task_id"], "run_id": result["run_id"], "error": (result.get("error") or {}).get("code")},
                task_id=task["task_id"],
            )
        return result

    def enforcement_op(self, op, params):
        if not isinstance(params, dict):
            raise InterfaceError("invalid_input", "Enforcement parameters must be an object")
        if op == "approval.request":
            required = {"session_id", "skill", "workflow", "inputs", "max_requests", "ttl_seconds"}
            if set(params) != required:
                raise InterfaceError("invalid_input", "Unknown or missing approval fields")
            session = self.session(params["session_id"])
            context = self.context(session["program_id"], session["campaign_id"])
            if session["context_revision"] != context["context_revision"]:
                raise InterfaceError("context_conflict", "Session context is stale")
            entry = next((e for e in catalog(self.root)["workflows"] if e["skill"] == params["skill"] and e["workflow"] == params["workflow"]), None)
            if entry is None:
                raise InterfaceError("invalid_input", "Unknown workflow")
            validate(params["inputs"], entry["inputs"])
            if type(params["max_requests"]) is not int or not 1 <= params["max_requests"] <= 100000:
                raise InterfaceError("invalid_input", "max_requests must be 1-100000")
            if type(params["ttl_seconds"]) is not int or not 1 <= params["ttl_seconds"] <= 86400:
                raise InterfaceError("invalid_input", "ttl_seconds must be 1-86400")
            return self.control().request_approval(context, session, entry, params["inputs"], params["max_requests"], params["ttl_seconds"])
        if op in ("approval.list", "budget.status"):
            fields(params, ["session_id"])
            session = self.session(params["session_id"])
            context = self.context(session["program_id"], session["campaign_id"])
            if op == "approval.list":
                return self.control().list_approvals(context["program_id"], context["campaign_id"])
            return self.control().budget_status(context["program_id"], context["campaign_id"])
        if op == "dispatch.authorize":
            if set(params) != {"session_id", "grant_id", "url", "method"}:
                raise InterfaceError("invalid_input", "Unknown or missing dispatch fields")
            for key in ("session_id", "grant_id"):
                validate(params[key], ID)
            if not isinstance(params["url"], str) or len(params["url"]) > 2048 or not isinstance(params["method"], str) or len(params["method"]) > 16:
                raise InterfaceError("invalid_input", "Invalid dispatch url or method")
            session = self.session(params["session_id"])
            context = self.context(session["program_id"], session["campaign_id"])
            if session["context_revision"] != context["context_revision"]:
                raise InterfaceError("context_conflict", "Session context is stale")
            return self.control().authorize(context, session, params["grant_id"], params["url"], params["method"])
        if op == "report.submit":
            if set(params) != {"session_id", "finding_id", "report"}:
                raise InterfaceError("invalid_input", "Unknown or missing report fields")
            validate(params["session_id"], ID)
            validate(params["finding_id"], ID)
            if not isinstance(params["report"], dict) or len(json.dumps(params["report"], sort_keys=True).encode()) > 49152:
                raise InterfaceError("invalid_input", "Report must be an object of at most 48 KiB")
            session = self.session(params["session_id"])
            context = self.context(session["program_id"], session["campaign_id"])
            if session["context_revision"] != context["context_revision"]:
                raise InterfaceError("context_conflict", "Session context is stale")
            module, store = self.memory()
            try:
                path = self.root / ".claude/skills/impact-verifier/scripts/review_store.py"
                spec = importlib.util.spec_from_file_location("bh_reviews_submit", path)
                review_module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(review_module)
                try:
                    candidate = review_module.Reviews(store, self.principal).owned(params["finding_id"], context, session)
                except (ValueError, KeyError, TypeError):
                    raise InterfaceError("scope_denial", "Unknown or inaccessible finding") from None
                store.validate_payload(params["report"])
                return self.control().submit_report(context, session, candidate, params["report"])
            except module.RevisionConflict:
                raise InterfaceError("context_conflict", "Finding revision changed") from None
            except (ValueError, TypeError, KeyError, AttributeError):
                raise InterfaceError("invalid_input", "Invalid report content or finding state") from None
            finally:
                store.db.close()
        raise InterfaceError("unsupported_capability", "Operation is unavailable")

    def operator_submission_grant(self, finding_id, report_raw, ttl_seconds):
        """Operator CLI only: bind a human submission grant to the exact report hash."""
        module, store = self.memory()
        try:
            row = store.db.execute("SELECT data FROM findings WHERE id=?", (finding_id,)).fetchone()
            if not row:
                raise InterfaceError("scope_denial", "Unknown finding")
            finding = json.loads(row["data"] if isinstance(row, sqlite3.Row) else row[0])
            store.validate_payload(finding)
            report = decode(report_raw)
            if not isinstance(report, dict):
                raise InterfaceError("invalid_input", "Report document must be a JSON object")
            store.validate_payload(report)
            return self.control().grant_submission(finding, report_digest(report), ttl_seconds)
        except ValueError:
            raise InterfaceError("invalid_input", "Invalid finding or report content") from None
        finally:
            store.db.close()

    def handle(self, request):
        validate_named(request, "request")
        self.recover_expired()
        op, params = request["operation"], request["params"]
        if op in ("capabilities", "discover"):
            fields(params, [])
            return capabilities() if op == "capabilities" else catalog(self.root)
        if op in ("approval.request", "approval.list", "dispatch.authorize", "budget.status", "report.submit"):
            return self.enforcement_op(op, params)
        if op in ("finding.create", "finding.get", "finding.list", "evidence.manifest", "evidence.read", "finding.history", "review.open", "review.snapshot", "review.artifact", "review.record"):
            return self.verification(params, op)
        if op in (
            "plan.generate",
            "plan.get",
            "queue.list",
            "queue.history",
            "queue.transition",
            "queue.claim",
            "queue.heartbeat",
            "queue.complete",
            "queue.stop",
            "queue.dependencies",
            "campaign.evaluate",
        ):
            return self.planning(params, op)
        if op == "session.open":
            return self.open_session(params)
        if op in ("session.resume", "session.handoff"):
            fields(params, ["session_id"])
            session = self.session(params["session_id"])
            session["last_seen"] = now()
            with self.db:
                self.db.execute("UPDATE sessions SET data=? WHERE id=?", (json.dumps(session), session["session_id"]))
            return self.handoff(session) if op.endswith("handoff") else {"session": session, "handoff": self.handoff(session)}
        if op in ("research.append", "research.correct", "research.query", "research.history", "research.summary", "research.contradictions", "research.export", "research.context"):
            allowed = (
                {"session_id", "record"}
                if op.endswith("append")
                else {"session_id", "entity_id", "expected_revision", "content", "reason"}
                if op.endswith("correct")
                else {"session_id", "entity_id", "max_records", "max_bytes"}
                if op.endswith("context")
                else {"session_id", "query", "kind", "entity_id"}
            )
            if not isinstance(params, dict) or set(params) - allowed or "session_id" not in params:
                raise InterfaceError("invalid_input", "Unknown or missing research fields")
            session = self.session(params["session_id"])
            context = self.context(session["program_id"], session["campaign_id"])
            if session["context_revision"] != context["context_revision"]:
                raise InterfaceError("context_conflict", "Session context is stale")
            module, store = self.memory()
            provenance = {
                "actor": self.principal,
                "session_id": session["session_id"],
                "campaign_id": session["campaign_id"],
                "provider": session["provider"],
                "client_version": session["client_version"],
                "task_id": None,
                "run_id": None,
            }
            try:
                if op.endswith("context"):
                    return store.context_pack(session["program_id"], params.get("entity_id"), params.get("max_records", 12), params.get("max_bytes", 32768))
                if op.endswith("summary"):
                    return store.summary(session["program_id"])
                if op.endswith("contradictions"):
                    return {"pairs": store.contradictions(session["program_id"])}
                if op.endswith("export"):
                    return {"records": store.export(session["program_id"])}
                if op.endswith("append"):
                    record = params.get("record", {})
                    validate(record, json.loads((self.root / "tools/schemas/research-record.schema.json").read_text()))
                    for artifact in record.get("artifacts", []) if isinstance(record, dict) else []:
                        if not isinstance(artifact, dict):
                            raise ValueError("Invalid artifact")
                        row = self.db.execute(
                            "SELECT tasks.result,artifacts.data FROM artifacts JOIN tasks ON tasks.id=artifacts.task WHERE artifacts.id=? AND tasks.program=? AND tasks.principal=?",
                            (artifact.get("id"), session["program_id"], self.principal),
                        ).fetchone()
                        if not row or hashlib.sha256(row["data"]).hexdigest() != artifact.get("sha256"):
                            raise ValueError("Unbound artifact")
                    return store.append(session["program_id"], params.get("record", {}), provenance)
                if op.endswith("correct"):
                    return store.correct(session["program_id"], params.get("entity_id"), params.get("expected_revision"), params.get("content"), params.get("reason"), provenance)
                if op.endswith("history") and not params.get("entity_id"):
                    raise ValueError("History requires entity_id")
                return {"records": store.query(session["program_id"], params.get("query", ""), params.get("kind"), params.get("entity_id") if op.endswith("history") else None)}
            except module.RevisionConflict:
                raise InterfaceError("context_conflict", "Research revision changed") from None
            except (ValueError, TypeError, KeyError, AttributeError):
                raise InterfaceError("invalid_input", "Invalid research input or conflicting revision") from None
            finally:
                store.db.close()
        if op == "artifact.read":
            fields(params, ["session_id", "task_id", "artifact_id"])
            result = self.task({k: params[k] for k in ("session_id", "task_id")})
            artifact = next((a for a in result["artifacts"] if a["id"] == params["artifact_id"]), None)
            if artifact is None:
                raise InterfaceError("scope_denial", "Unknown or inaccessible artifact")
            row = self.db.execute("SELECT data FROM artifacts WHERE id=? AND task=?", (artifact["id"], params["task_id"])).fetchone()
            if not row or hashlib.sha256(row[0]).hexdigest() != artifact["sha256"]:
                raise InterfaceError("internal_failure", "Artifact integrity check failed")
            return {"schema_version": "1.0", "artifact": artifact, "data": json.loads(row[0])}
        if op == "task.submit":
            return self.submit(params)
        if op in ("task.status", "task.cancel"):
            with self.db:
                if op == "task.cancel":
                    self.db.execute("BEGIN IMMEDIATE")
                result = self.task(params)
                if op == "task.cancel" and result["status"] in ("queued", "running", "awaiting_approval"):
                    result.update(status="cancelled", ended_at=now())
                    self.save_result(result)
                    owner = self.db.execute("SELECT program,campaign,principal FROM tasks WHERE id=?", (params["task_id"],)).fetchone()
                    self.control().audit("cancelled", owner["program"], owner["campaign"], {"task_id": params["task_id"]}, task_id=params["task_id"], principal=owner["principal"])
            return result
        raise InterfaceError("unsupported_capability", "Operation is unavailable")
