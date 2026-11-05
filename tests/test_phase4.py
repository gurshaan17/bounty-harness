import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.agent_interface.catalog import ROOT, capabilities
from tools.agent_interface.contracts import InterfaceError
from tools.agent_interface.service import Service


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


planning = load("phase4_planner", ".claude/skills/planner/scripts/research_planner.py")
memory = load("phase4_memory", ".claude/skills/program-memory/scripts/research_store.py")
gate = load("phase4_gate", ".claude/skills/campaign/scripts/completion_gate.py")


class Phase4Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.service = Service(self.path / "interface.sqlite", "uid:test")
        self.addCleanup(self.service.db.close)
        self.service.register("fixture", "campaign", "a" * 64)
        self.sid = self.service.open_session(dict(program_id="fixture", campaign_id="campaign", provider="codex", client_version="test"))["session"]["session_id"]

    def call(self, op, **params):
        return self.service.handle({"schema_version": "1.0", "operation": op, "params": {"session_id": self.sid, **params}})

    def append(self, kind, content, **fields):
        return self.call("research.append", record={"kind": kind, "content": content, **fields})

    def application(self):
        self.append("component", "Fixture document service", details={"trust_boundaries": ["tenant"]})
        self.append("role", "Fixture tenant member")
        self.append("role", "Fixture tenant administrator")
        return self.append(
            "functionality",
            "Read fixture document",
            details={"surface": "/documents/{id}", "transitions": ["draft", "published"], "invariants": ["owner controls publication"]},
            external_refs=[{"system": "traffic-corpus", "id": "route:documents"}],
        )

    def tasks(self):
        return self.call("queue.list")["tasks"]

    def ready_claim(self, task):
        ready = self.call("queue.transition", task_id=task["id"], expected_revision=task["revision"], state="ready", reason="Review offline source evidence")
        return self.call("queue.claim", task_id=task["id"], expected_revision=ready["revision"])

    def complete(self, task, verdict="rejected"):
        return self.call(
            "queue.complete",
            task_id=task["id"],
            expected_revision=task["revision"],
            lease_token=task["lease"]["token"],
            result="Local fixture evidence supports the recorded boundary under tested conditions",
            verdict=verdict,
        )

    def test_unknown_architecture_and_deterministic_boundary_planning(self):
        empty = self.call("plan.generate")
        self.assertEqual("unknown architecture", empty["opportunities"][0]["boundary"])
        self.assertEqual({"kind": "offline_analysis"}, empty["opportunities"][0]["method"])
        self.application()
        plan = self.call("plan.generate")
        self.assertEqual(3, len(plan["opportunities"]))
        self.assertTrue(all("/documents/{id}" in x["question"] for x in plan["opportunities"]))
        again = self.call("plan.generate")
        self.assertEqual([(x["identity"], x["score"]) for x in plan["opportunities"]], [(x["identity"], x["score"]) for x in again["opportunities"]])
        self.assertIn("Offline proposals", plan["markdown"])
        self.assertFalse(plan["grants_execution"])
        self.assertTrue(plan["input_revisions"])
        for item in plan["opportunities"]:
            self.assertEqual({"impact", "evidence_quality", "research_value", "effort"}, set(item["score_components"]))

    def test_negative_suppression_changed_facts_and_expiry_reopen(self):
        function = self.application()
        plan = self.call("plan.generate")
        task = self.tasks()[0]
        finished = self.complete(self.ready_claim(task))
        regenerated = self.call("plan.generate")
        self.assertIn(task["identity"], [r["identity"] for r in regenerated["suppressed"]])
        self.assertEqual("rejected", finished["hypothesis_verdict"])
        self.call("research.correct", entity_id=function["id"], expected_revision=1, content="Updated fixture behavior", reason="Local fixture version changed")
        changed = self.call("plan.generate")
        reopened = next(r for r in changed["opportunities"] if r["task_id"] == task["id"])
        self.assertEqual("Application/source facts changed", reopened["reopen_reason"])
        self.assertGreater(len(self.call("queue.history", task_id=task["id"])["events"]), 3)
        self.assertTrue(self.call("plan.get", plan_id=plan["id"])["stale"])

    def test_leases_switch_conflicts_and_expired_unknown_outcome(self):
        self.application()
        self.call("plan.generate")
        claimed = self.ready_claim(self.tasks()[0])
        with self.assertRaises(InterfaceError):
            self.call("queue.claim", task_id=claimed["id"], expected_revision=claimed["revision"])
        first = self.sid
        self.sid = self.service.open_session(dict(program_id="fixture", campaign_id="campaign", provider="opencode", client_version="test", predecessor_session_id=first))["session"]["session_id"]
        with self.assertRaises(InterfaceError):
            self.complete(claimed)
        self.sid = first
        heartbeat = self.call("queue.heartbeat", task_id=claimed["id"], expected_revision=claimed["revision"], lease_token=claimed["lease"]["token"])
        self.assertGreater(heartbeat["revision"], claimed["revision"])
        with patch.object(planning.time, "time", return_value=heartbeat["lease"]["expires_at"] + 1):
            # All modules share the Python time module, including service-loaded planner.
            blocked = next(t for t in self.tasks() if t["id"] == claimed["id"])
        self.assertEqual("blocked", blocked["state"])
        self.assertTrue(blocked["unknown_outcome"])
        with self.assertRaises(InterfaceError):
            self.call("queue.transition", task_id=blocked["id"], expected_revision=blocked["revision"], state="proposed", reason="Retry")

    def test_dependencies_cycles_operator_defer_and_revision_checks(self):
        self.application()
        self.call("plan.generate")
        first, second = self.tasks()[:2]
        first = self.call("queue.dependencies", task_id=first["id"], expected_revision=first["revision"], dependencies=[second["id"]], reason="Need related boundary assessment")
        with self.assertRaises(InterfaceError):
            self.call("queue.dependencies", task_id=second["id"], expected_revision=second["revision"], dependencies=[first["id"]], reason="Cycle")
        with self.assertRaises(InterfaceError):
            self.ready_claim(first)
        adjusted = self.service.planning(
            {"session_id": self.sid, "task_id": second["id"], "expected_revision": second["revision"], "position": 0, "deferred": True, "reason": "Operator defers analysis"},
            "operator.adjust",
            operator=True,
        )
        with self.assertRaises(InterfaceError):
            self.ready_claim(adjusted)
        with self.assertRaises(InterfaceError):
            self.call("operator.adjust", task_id=second["id"], expected_revision=second["revision"], position=0, deferred=False, reason="Agent override")
        with self.assertRaises(InterfaceError):
            self.call("queue.transition", task_id=first["id"], expected_revision=1, state="closed", reason="Stale writer")

    def test_safety_filter_and_schema_and_program_isolation(self):
        self.application()
        plan = self.call("plan.generate")
        original = plan["opportunities"][0]
        allowed = {
            "question",
            "hypothesis",
            "hypothesis_id",
            "surface",
            "boundary",
            "roles",
            "potential_impact",
            "supporting",
            "contradicting",
            "alternative_explanations",
            "prerequisites",
            "method",
            "expected_artifacts",
            "stop_condition",
            "dependencies",
        }
        proposal = {k: original[k] for k in allowed}
        proposal["hypothesis"] += " unsafe"
        proposal["method"] = {"kind": "shell", "command": "anything"}
        filtered = self.call("plan.generate", proposals=[proposal])
        self.assertFalse(filtered["opportunities"])
        self.assertEqual(1, len(filtered["ineligible"]))
        for record in ({**proposal, "score": 1}, {**proposal, "state": "running"}, {**proposal, "supporting": ["wrong-program"]}):
            with self.assertRaises(InterfaceError):
                self.call("plan.generate", proposals=[record])
        with self.assertRaises(InterfaceError):
            self.call("plan.generate", weights={"impact": -1})
        self.service.register("other", "campaign", "a" * 64)
        self.sid = self.service.open_session(dict(program_id="other", campaign_id="campaign", provider="codex", client_version="test"))["session"]["session_id"]
        self.assertEqual([], self.tasks())
        with self.assertRaises(InterfaceError):
            self.call("plan.get", plan_id=plan["id"])

    def test_atomic_completion_rollback_and_report_gate(self):
        self.application()
        self.call("plan.generate")
        claimed = self.ready_claim(self.tasks()[0])
        with sqlite3.connect(self.service.memory_path) as db:
            before = db.execute("SELECT count(*) FROM research_events").fetchone()[0]
            db.execute("CREATE TRIGGER reject_coverage BEFORE INSERT ON research_events WHEN json_extract(NEW.data, '$.record.kind')='coverage' BEGIN SELECT RAISE(ABORT, 'fixture rollback'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.complete(claimed)
        with sqlite3.connect(self.service.memory_path) as db:
            self.assertEqual(before, db.execute("SELECT count(*) FROM research_events").fetchone()[0])
            db.execute("DROP TRIGGER reject_coverage")
        self.assertEqual("running", next(t for t in self.tasks() if t["id"] == claimed["id"])["state"])
        done = self.complete(claimed, verdict="needs_review")
        self.assertEqual("investigated", done["state"])
        self.assertEqual(2, len(done["result_ids"]))
        remaining = self.call("plan.generate")["opportunities"]
        self.assertTrue(all(item["score_components"]["research_value"] == 1 for item in remaining))
        assessment = self.call("campaign.evaluate")
        self.assertFalse(assessment["can_complete"])
        self.assertIn({"kind": "review", "id": done["id"], "reason": "Investigation leaves an unresolved obligation"}, assessment["obligations"])
        with self.assertRaises(InterfaceError):
            self.call("campaign.evaluate", complete=True)
        module, store = self.service.memory()
        self.addCleanup(store.db.close)
        self.assertEqual("budget_exhausted", gate.evaluate(store, "fixture", "campaign", self.tasks(), budget_exhausted=True)["status"])
        self.assertEqual("operator_stopped", gate.evaluate(store, "fixture", "campaign", self.tasks(), stop_reason="operator stop")["status"])

    def test_migration_v2_preserves_history_and_backup(self):
        path = self.path / "v2.sqlite"
        store = memory.ResearchStore(path)
        original = store.append("p", {"kind": "observation", "content": "fixture record"}, {})
        with store.db:
            for table in ("research_plans", "research_queue", "queue_events"):
                store.db.execute(f"DROP TABLE {table}")
            store.db.execute("DELETE FROM memory_schema WHERE version=3")
        store.db.close()
        upgraded = memory.ResearchStore(path)
        self.addCleanup(upgraded.db.close)
        self.assertEqual(original, upgraded.get("p", original["id"]))
        self.assertTrue(list(self.path.glob("v2.sqlite.pre-v3-*.sqlite")))
        self.assertEqual(3, upgraded.db.execute("SELECT max(version) FROM memory_schema").fetchone()[0])

    def test_capabilities_expose_only_offline_queue_controls(self):
        info = capabilities()
        self.assertIn("queue.claim", info["operations"])
        self.assertIn("campaign.evaluate", info["operations"])
        self.assertNotIn("operator.adjust", info["operations"])
        self.assertIn("active_dispatch", info["unavailable_operations"])



class Phase4IntegrationTests(unittest.TestCase):
    setUp = Phase4Tests.setUp
    call = Phase4Tests.call
    append = Phase4Tests.append
    application = Phase4Tests.application
    tasks = Phase4Tests.tasks
    ready_claim = Phase4Tests.ready_claim
    complete = Phase4Tests.complete

    def test_expired_negative_and_different_method_reopen(self):
        self.application()
        original = self.call("plan.generate")["opportunities"][0]
        fields = (
            "question",
            "hypothesis",
            "hypothesis_id",
            "surface",
            "boundary",
            "roles",
            "potential_impact",
            "supporting",
            "contradicting",
            "alternative_explanations",
            "prerequisites",
            "method",
            "expected_artifacts",
            "stop_condition",
            "dependencies",
        )
        proposal = {k: original[k] for k in fields}
        self.append(
            "negative_result",
            "Expired fixture analysis",
            details={k: original[k] for k in ("identity", "surface", "boundary", "hypothesis", "method")},
            sources=original["supporting"],
            expires_at="2000-01-01T00:00:00+00:00",
        )
        task = next(t for t in self.tasks() if t["id"] == original["task_id"])
        self.call("queue.transition", task_id=task["id"], expected_revision=task["revision"], state="closed", reason="Prior analysis disposition")
        reopened = self.call("plan.generate", proposals=[proposal])
        self.assertEqual("Negative evidence expired", reopened["opportunities"][0]["reopen_reason"])
        task = next(t for t in self.tasks() if t["id"] == original["task_id"])
        self.call("queue.transition", task_id=task["id"], expected_revision=task["revision"], state="closed", reason="Prior method complete")
        proposal["method"] = {"kind": "workflow", "skill": "standard-catalog", "workflow": "search"}
        changed = self.call("plan.generate", proposals=[proposal])
        self.assertEqual("Substantially different registered method", changed["opportunities"][0]["reopen_reason"])
        task = next(t for t in self.tasks() if t["id"] == original["task_id"])
        ready = self.call("queue.transition", task_id=task["id"], expected_revision=task["revision"], state="ready", reason="Propose method only")
        with self.assertRaises(InterfaceError):
            self.call("queue.claim", task_id=ready["id"], expected_revision=ready["revision"])
        approval = self.call("queue.transition", task_id=ready["id"], expected_revision=ready["revision"], state="awaiting_approval", reason="No execution service available")
        with self.assertRaises(InterfaceError):
            self.call("queue.transition", task_id=approval["id"], expected_revision=approval["revision"], state="running", reason="Agent issued approval")

    def test_simultaneous_claims_and_restart_recover_same_identity(self):
        import concurrent.futures

        self.application()
        self.call("plan.generate")
        task = self.tasks()[0]
        ready = self.call("queue.transition", task_id=task["id"], expected_revision=task["revision"], state="ready", reason="Concurrent fixture")

        def claim():
            service = Service(self.path / "interface.sqlite", "uid:test")
            try:
                return service.handle({"schema_version": "1.0", "operation": "queue.claim", "params": {"session_id": self.sid, "task_id": ready["id"], "expected_revision": ready["revision"]}})[
                    "state"
                ]
            except InterfaceError:
                return "denied"
            finally:
                service.db.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(["denied", "running"], sorted(outcomes))
        self.assertEqual("running", next(t for t in self.tasks() if t["id"] == task["id"])["state"])

    def test_program_dedup_across_campaigns_and_changed_weights(self):
        self.application()
        original = self.call("plan.generate")
        adjusted = self.call("plan.generate", weights={"impact": 0, "evidence_quality": 0, "research_value": 1, "effort": 0})
        self.assertTrue(all(item["score"] == 1 for item in adjusted["opportunities"]))
        self.assertNotEqual(original["opportunities"][0]["score"], adjusted["opportunities"][0]["score"])
        self.service.register("fixture", "second", "a" * 64)
        self.sid = self.service.open_session(dict(program_id="fixture", campaign_id="second", provider="opencode", client_version="test"))["session"]["session_id"]
        plan = self.call("plan.generate")
        self.assertFalse(plan["opportunities"])
        self.assertEqual(3, len(plan["suppressed"]))
        self.assertTrue(all("another campaign" in entry["reason"] for entry in plan["suppressed"]))

    def test_completion_needs_review_operator_disposition_and_missing_artifacts(self):
        self.application()
        self.call("plan.generate")
        done = self.complete(self.ready_claim(self.tasks()[0]), verdict="needs_review")
        for task in self.tasks():
            if task["id"] != done["id"]:
                self.call("queue.transition", task_id=task["id"], expected_revision=task["revision"], state="closed", reason="Focused fixture excludes remaining questions")
        self.assertEqual("blocked", self.call("campaign.evaluate")["status"])
        self.service.planning(
            {"session_id": self.sid, "task_id": done["id"], "expected_revision": done["revision"], "reason": "Operator closes incomplete investigation; no confirmed finding"},
            "operator.dispose",
            operator=True,
        )
        self.assertEqual("completed", self.call("campaign.evaluate")["status"])
        review = self.append("question", "Pending independent review", details={"obligation": "review"})
        assessment = self.call("campaign.evaluate")
        self.assertEqual("blocked", assessment["status"])
        self.assertTrue(any(item["id"] == review["id"] and item["kind"] == "review" for item in assessment["obligations"]))
        with self.assertRaises(InterfaceError):
            self.call("research.append", record={"kind": "decision", "content": "Human approved", "basis": "human"})

    def test_restricted_obligations_and_secrets_are_not_lost(self):
        self.append("hypothesis", "Restricted fixture uncertainty", sensitivity="high-sensitivity")
        assessment = self.call("campaign.evaluate")
        self.assertEqual("blocked", assessment["status"])
        self.assertEqual("restricted_obligation", assessment["obligations"][0]["kind"])
        self.application()
        self.call("plan.generate")
        task = self.tasks()[0]
        with self.assertRaises(InterfaceError):
            self.call("queue.transition", task_id=task["id"], expected_revision=task["revision"], state="closed", reason="Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(1, next(t for t in self.tasks() if t["id"] == task["id"])["revision"])

    def test_scope_revision_invalidates_prior_readiness_even_for_new_session(self):
        self.application()
        plan = self.call("plan.generate")
        task = self.tasks()[0]
        ready = self.call("queue.transition", task_id=task["id"], expected_revision=task["revision"], state="ready", reason="Review current offline inputs")
        self.service.register("fixture", "campaign", "b" * 64)
        self.sid = self.service.open_session(dict(program_id="fixture", campaign_id="campaign", provider="opencode", client_version="test"))["session"]["session_id"]
        self.assertTrue(self.call("plan.get", plan_id=plan["id"])["stale"])
        with self.assertRaises(InterfaceError) as error:
            self.call("queue.claim", task_id=ready["id"], expected_revision=ready["revision"])
        self.assertEqual("context_conflict", error.exception.code)
        refreshed = self.call("plan.generate")
        self.assertEqual(2, refreshed["context"]["context_revision"])
        self.assertEqual("proposed", next(t for t in self.tasks() if t["id"] == task["id"])["state"])

    def test_required_review_cannot_be_disposed_by_agent_and_live_work_can_stop(self):
        self.application()
        review = self.append("question", "Review fixture evidence independently", details={"obligation": "review"})
        original = self.call("plan.generate")["opportunities"][0]
        fields = (
            "question",
            "hypothesis",
            "hypothesis_id",
            "surface",
            "boundary",
            "roles",
            "potential_impact",
            "supporting",
            "contradicting",
            "alternative_explanations",
            "prerequisites",
            "method",
            "expected_artifacts",
            "stop_condition",
            "dependencies",
        )
        proposal = {key: original[key] for key in fields}
        proposal["hypothesis"] = "Review obligation remains pending"
        proposal["supporting"] = [review["id"]]
        planned = self.call("plan.generate", proposals=[proposal])["opportunities"][0]
        task = next(t for t in self.tasks() if t["id"] == planned["task_id"])
        claimed = self.ready_claim(task)
        handoff = self.call("session.handoff")
        self.assertTrue(any(t["id"] == claimed["id"] for t in handoff["research_queue"]["tasks"]))
        stopped = self.call("queue.stop", task_id=claimed["id"], expected_revision=claimed["revision"], lease_token=claimed["lease"]["token"], state="cancelled", reason="Offline analysis stopped")
        assessment = self.call("campaign.evaluate")
        self.assertTrue(any(item["id"] == review["id"] for item in assessment["obligations"]))
        self.service.planning(
            {"session_id": self.sid, "task_id": stopped["id"], "expected_revision": stopped["revision"], "reason": "Operator explicitly disposes this incomplete review"},
            "operator.dispose",
            operator=True,
        )
        assessment = self.call("campaign.evaluate")
        self.assertFalse(any(item["id"] == review["id"] for item in assessment["obligations"]))
        self.assertFalse(assessment["submission_approved"])

    def test_expected_result_loss_blocks_completion(self):
        self.application()
        self.call("plan.generate")
        done = self.complete(self.ready_claim(self.tasks()[0]), verdict="supported")
        with sqlite3.connect(self.service.memory_path) as db:
            db.execute("DELETE FROM research_entities WHERE id=?", (done["result_ids"][0],))
        assessment = self.call("campaign.evaluate")
        self.assertTrue(any(item["kind"] == "artifact" and item["id"] == done["result_ids"][0] for item in assessment["obligations"]))
        self.assertFalse(assessment["can_complete"])

    def test_registered_workflow_cli_emits_private_json_and_markdown(self):
        import json
        import os
        import subprocess
        import sys

        service = Service(self.path / "agent-interface.sqlite3", f"uid:{os.getuid()}")
        self.addCleanup(service.db.close)
        service.register("cli", "fixture", "c" * 64)
        sid = service.open_session(dict(program_id="cli", campaign_id="fixture", provider="codex", client_version="test"))["session"]["session_id"]
        request = self.path / "request.json"
        request.write_text(json.dumps({"schema_version": "1.0", "operation": "plan.generate", "params": {"session_id": sid}}))
        output, markdown = self.path / "plan.json", self.path / "plan.md"
        process = subprocess.run(
            [sys.executable, str(ROOT / ".claude/skills/planner/scripts/research_workflow.py"), "--request", str(request), "--output", str(output), "--markdown", str(markdown)],
            env={**os.environ, "BB_DATA_ROOT": str(self.path)},
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, process.returncode, process.stderr)
        plan = json.loads(output.read_text())
        self.assertEqual("unknown architecture", plan["opportunities"][0]["boundary"])
        self.assertEqual(plan["markdown"] + "\n", markdown.read_text())
        self.assertEqual(0o600, output.stat().st_mode & 0o777)
        self.assertEqual(0o600, markdown.stat().st_mode & 0o777)


class Phase4CampaignTests(unittest.TestCase):
    def setUp(self):
        import sys

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.recovery_module = load("phase4_recovery", ".claude/skills/campaign/scripts/recovery.py")
        sys.modules["recovery"] = self.recovery_module
        self.campaign_module = load("phase4_campaign", ".claude/skills/campaign/scripts/campaign_runner.py")

    def test_execution_completion_checks_durable_phases_runs_and_outputs(self):
        directory = self.path / "run"
        directory.mkdir()
        recovery = self.recovery_module.Recovery(directory)
        self.addCleanup(recovery.db.close)
        output = directory / "result.json"
        recovery.expect("phase:report", {})
        recovery.expect("workflow:fixture/review", {"outputs": [str(output)]})
        recovery.claim("fixture/review", "digest")
        recovery.finish("fixture/review", 0)
        self.assertEqual({"phase", "artifact"}, {r["kind"] for r in recovery.completion()})
        output.write_text("")
        self.assertTrue(any(r["kind"] == "artifact" for r in recovery.completion()))
        output.write_text("fixture evidence")
        recovery.satisfy("phase:report")
        self.assertEqual([], recovery.completion())
        reopened = self.recovery_module.Recovery(directory)
        self.addCleanup(reopened.db.close)
        self.assertEqual([], reopened.completion())
        output.unlink()
        self.assertEqual("artifact", reopened.completion()[0]["kind"])

    def runner(self):
        args = self.campaign_module.build_parser().parse_args(["hunt", "--target", "fixture.test", "--program", "fixture", "--max-tier", "passive", "--no-init"])
        return self.campaign_module.Campaign(args)

    def test_hunt_cannot_complete_with_pending_review(self):
        import json

        runner = self.runner()

        def backbone():
            service = Service(self.path / "agent-interface.sqlite3", "uid:test")
            try:
                service.register("fixture", runner.campaign_id, "a" * 64)
                sid = service.open_session(dict(program_id="fixture", campaign_id=runner.campaign_id, provider="codex", client_version="test"))["session"]["session_id"]
                service.handle(
                    {
                        "schema_version": "1.0",
                        "operation": "research.append",
                        "params": {"session_id": sid, "record": {"kind": "question", "content": "Pending fixture review", "details": {"obligation": "review"}}},
                    }
                )
            finally:
                service.db.close()
            runner.recovery.satisfy("phase:backbone")

        with (
            patch.object(self.campaign_module, "CAMPAIGN_ROOT", self.path / "campaigns"),
            patch.object(self.campaign_module, "load_context", return_value={"program": "fixture"}),
            patch.object(runner, "phase_backbone", side_effect=backbone),
            patch.object(runner, "phase_execute", side_effect=lambda _: runner.recovery.satisfy("phase:execute")),
            patch.object(runner, "phase_report", side_effect=lambda: runner.recovery.satisfy("phase:report")),
        ):
            runner.hunt()
        status = json.loads((runner.dir / "status.json").read_text())
        self.assertEqual("blocked", status["phase"])
        self.assertFalse(status["completion"]["can_complete"])
        self.assertTrue(any(x["kind"] == "review" for x in status["completion"]["obligations"]))
        self.assertEqual(status["completion"], json.loads((runner.dir / "completion.json").read_text()))

    def test_budget_and_operator_stop_preserve_obligations_without_continuation(self):
        import json

        for stop in ("budget", "operator"):
            with self.subTest(stop=stop):
                runner = self.runner()

                def backbone():
                    if stop == "operator":
                        raise KeyboardInterrupt()
                    runner.start -= runner.budget + 1

                with (
                    patch.object(self.campaign_module, "CAMPAIGN_ROOT", self.path / "campaigns"),
                    patch.object(self.campaign_module, "load_context", return_value={"program": "fixture"}),
                    patch.object(runner, "phase_backbone", side_effect=backbone),
                    patch.object(runner, "phase_execute") as execute,
                    patch.object(runner, "phase_report") as report,
                ):
                    if stop == "operator":
                        with self.assertRaises(KeyboardInterrupt):
                            runner.hunt()
                    else:
                        runner.hunt()
                    execute.assert_not_called()
                    report.assert_not_called()
                status = json.loads((runner.dir / "status.json").read_text())
                self.assertEqual("budget_exhausted" if stop == "budget" else "operator_stopped", status["phase"])
                self.assertEqual(3, len(status["completion"]["obligations"]))
                self.assertFalse(status["completion"]["can_complete"])


if __name__ == "__main__":
    unittest.main()
