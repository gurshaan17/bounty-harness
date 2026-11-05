"""Durable local sessions and tasks. Local identity is established outside JSON."""

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import UTC, datetime

from tools.execution_control.offline import execute

from .catalog import PROVIDERS, ROOT, capabilities, catalog
from .contracts import InterfaceError, digest, validate, validate_named

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
        """)

    def recover_expired(self):
        with self.db:
            rows = self.db.execute("SELECT tasks.id,tasks.result FROM tasks JOIN task_leases ON tasks.id=task_leases.task WHERE task_leases.expires<?", (time.time(),)).fetchall()
            for row in rows:
                result = json.loads(row["result"])
                if result["status"] == "running":
                    result.update(status="interrupted", ended_at=now(), error={"code": "interrupted", "message": "Worker lease expired; reconcile artifacts before proposing a new task"})
                    self.save_result(result)
                self.db.execute("DELETE FROM task_leases WHERE task=?", (row["id"],))

    def register(self, program, campaign, scope_digest):
        """Operator CLI only. Explicit scope snapshot import, never a JSON operation."""
        fields({"program": program, "campaign": campaign}, ["program", "campaign"])
        with self.db:
            row = self.db.execute("SELECT * FROM contexts WHERE program=? AND campaign=?", (program, campaign)).fetchone()
            if row and row["principal"] != self.principal:
                raise InterfaceError("scope_denial", "Context belongs to another principal")
            revision = row["revision"] if row else 1
            if row and (row["scope"] != scope_digest or row["policy"] != POLICY_DIGEST):
                revision += 1
            self.db.execute("INSERT OR REPLACE INTO contexts VALUES(?,?,?,?,?,?)", (program, campaign, self.principal, revision, scope_digest, POLICY_DIGEST))
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
        return {
            "schema_version": "1.0",
            "context": context,
            "session_id": session["session_id"],
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
            result.update(status="running", started_at=now())
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                current = self.task({"session_id": task["session_id"], "task_id": task["task_id"]})
                if current["status"] == "cancelled":
                    return current
                latest = self.context(task["program_id"], task["campaign_id"])
                if latest != context:
                    raise InterfaceError("context_conflict", "Context changed before launch")
                self.save_result(result)
                self.db.execute("INSERT OR REPLACE INTO task_leases VALUES(?,?)", (task["task_id"], time.time() + 30))
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
            return result
        except InterfaceError as exc:
            result.update(status="denied" if result["started_at"] is None else "failed", error={"code": exc.code, "message": exc.message})
        except KeyboardInterrupt:
            result.update(status="interrupted", error={"code": "interrupted", "message": "Execution interrupted"})
        except Exception:
            # Do not serialize exception text, input data, credentials, or filesystem paths.
            result.update(status="failed", error={"code": "internal_failure", "message": "Offline execution failed"})
        result["ended_at"] = now()
        with self.db:
            self.save_result(result)
        return result

    def handle(self, request):
        validate_named(request, "request")
        self.recover_expired()
        op, params = request["operation"], request["params"]
        if op in ("capabilities", "discover"):
            fields(params, [])
            return capabilities() if op == "capabilities" else catalog(self.root)
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
            return result
        raise InterfaceError("unsupported_capability", "Operation is unavailable")
