import hashlib
import importlib.util
import ipaddress
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.agent_interface.catalog import ROOT, capabilities, catalog
from tools.agent_interface.contracts import InterfaceError
from tools.agent_interface.service import Service
from tools.execution_control import scope


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCOPE_TEXT = """# Engagement scope
# In Scope
example.test
*.apps.example.test
https://portal.example.test:8443/login
198.51.100.10

# Out of Scope
admin.example.test
https://example.test/logout
"""

def public_resolver(host):
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        return ["93.184.216.34"]


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.rules = scope.parse(SCOPE_TEXT)

    def check(self, url, resolver=public_resolver):
        return scope.check_url(self.rules, url, resolver=resolver)

    def test_documented_headings_and_exact_hosts(self):
        self.assertTrue(self.check("https://example.test/")["allowed"])
        self.assertFalse(self.check("https://www.example.test/")["allowed"])
        self.assertTrue(self.check("https://a.b.apps.example.test/")["allowed"])
        self.assertFalse(self.check("https://apps.example.test/")["allowed"])

    def test_exclusions_win_over_inclusions(self):
        denied = self.check("https://admin.example.test/")
        self.assertFalse(denied["allowed"])
        self.assertIn("out of scope", denied["reason"])
        self.assertFalse(self.check("https://example.test/logout")["allowed"])
        self.assertTrue(self.check("https://example.test/other")["allowed"])

    def test_url_rule_pins_scheme_port_and_path(self):
        self.assertTrue(self.check("https://portal.example.test:8443/login")["allowed"])
        self.assertFalse(self.check("https://portal.example.test:8443/other")["allowed"])
        self.assertFalse(self.check("https://portal.example.test/login")["allowed"])
        self.assertFalse(self.check("http://portal.example.test:8443/login")["allowed"])
        # Path prefix must not bleed into sibling segments.
        rules = scope.parse("# In Scope\nhttps://example.test/api\n")
        self.assertFalse(scope.check_url(rules, "https://example.test/apis", resolver=public_resolver)["allowed"])
        self.assertTrue(scope.check_url(rules, "https://example.test/api/v1", resolver=public_resolver)["allowed"])

    def test_scheme_userinfo_fragment_and_ip_literals(self):
        for url in ("ftp://example.test/", "gopher://example.test/", "https://user:pw@example.test/", "https://example.test/#frag"):
            self.assertFalse(self.check(url)["allowed"], url)
        self.assertTrue(self.check("https://198.51.100.10/")["allowed"])
        self.assertFalse(self.check("https://192.0.2.5/")["allowed"])

    def test_dns_resolution_and_private_addresses(self):
        self.assertFalse(self.check("https://example.test/", resolver=lambda h: [])["allowed"])
        self.assertFalse(self.check("https://example.test/", resolver=lambda h: ["10.1.2.3"])["allowed"])
        rules = scope.parse("# In Scope\nexample.test\n10.1.2.0/24\n")
        self.assertTrue(scope.check_url(rules, "https://example.test/", resolver=lambda h: ["10.1.2.3"])["allowed"])
        mixed = scope.check_url(rules, "https://example.test/", resolver=lambda h: ["10.1.2.3", "93.184.216.34"])
        self.assertTrue(mixed["allowed"])

    def test_malformed_entries_fail_closed(self):
        for bad in ("", "# only comments\n", "# In Scope\n*\n", "# In Scope\nhttps://example.test/?q=1\n", "# In Scope\nfoo/bar\n"):
            with self.assertRaises(ValueError, msg=bad):
                scope.parse(bad)


class Phase6Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.scope_digest = hashlib.sha256(SCOPE_TEXT.encode()).hexdigest()
        self.service = Service(self.path / "agent-interface.sqlite3", "uid:agent")
        self.operator = Service(self.path / "agent-interface.sqlite3", "uid:operator")
        self.addCleanup(self.service.db.close)
        self.addCleanup(self.operator.db.close)
        self.context = self.service.register("fixture", "campaign", self.scope_digest, scope_text=SCOPE_TEXT)
        self.sid = self.service.open_session(dict(program_id="fixture", campaign_id="campaign", provider="codex", client_version="test"))["session"]["session_id"]
        self.workflow = next(e for e in catalog()["workflows"] if e["id"] == "standard-catalog/search")
        self.network = patch("tools.execution_control.scope.resolve_host", side_effect=public_resolver)
        self.network.start()
        self.addCleanup(self.network.stop)

    def call(self, operation, **params):
        return self.service.handle({"schema_version": "1.0", "operation": operation, "params": {"session_id": self.sid, **params}})

    def operator_call(self, operation, **params):
        return self.operator.handle({"schema_version": "1.0", "operation": operation, "params": params})

    def request(self, **extra):
        params = dict(skill="recon", workflow="live-discovery", inputs={"OUTDIR": "/tmp/bb-fixture-out"}, max_requests=5, ttl_seconds=600)
        params.update(extra)
        return self.call("approval.request", **params)

    def grant(self, request_id, **extra):
        return self.operator.control().grant_approval(request_id, extra.pop("ttl", 600), extra.pop("max_requests", None))

    def authorize(self, grant_id, url="https://example.test/", method="GET"):
        return self.call("dispatch.authorize", grant_id=grant_id, url=url, method=method)

    def test_scope_registration_and_missing_rules(self):
        bare = Service(self.path / "bare.sqlite3", "uid:agent")
        self.addCleanup(bare.db.close)
        bare.register("fixture", "campaign", "b" * 64)
        sid = bare.open_session(dict(program_id="fixture", campaign_id="campaign", provider="codex", client_version="t"))["session"]["session_id"]
        with self.assertRaises(InterfaceError) as caught:
            bare.handle({"schema_version": "1.0", "operation": "dispatch.authorize", "params": {"session_id": sid, "grant_id": "x", "url": "https://example.test/", "method": "GET"}})
        self.assertEqual("scope_denial", caught.exception.code)

    def test_approval_lifecycle_and_boundary(self):
        request = self.request()
        self.assertEqual("requested", request["status"])
        grant = self.grant(request["id"])
        self.assertEqual("active", grant["status"])
        self.assertEqual(["GET", "HEAD"], grant["allowed_methods"])
        self.operator.control().set_budget("fixture", "campaign", 10)
        allowed = self.authorize(grant["id"])
        self.assertTrue(allowed["allowed"], allowed)
        denied = self.authorize(grant["id"], url="https://evil.test/")
        self.assertFalse(denied["allowed"])
        self.assertIn("out of scope", denied["reason"]) if "evil" in SCOPE_TEXT else self.assertIn("no in-scope", denied["reason"])
        listed = self.call("approval.list")
        self.assertEqual(2, listed["total_returned"])

    def test_no_target_traffic_without_grant_budget_and_scope(self):
        request = self.request()
        # No grant yet: unknown grant id is denied outright.
        with self.assertRaises(InterfaceError):
            self.authorize("missing-grant")
        grant = self.grant(request["id"])
        # Without a configured budget every dispatch is denied.
        denied = self.authorize(grant["id"])
        self.assertFalse(denied["allowed"])
        self.assertTrue(denied["reason"].startswith("no aggregate budget"))
        self.operator.control().set_budget("fixture", "campaign", 10)
        self.assertTrue(self.authorize(grant["id"])["allowed"])

    def test_budget_exhaustion_and_restart(self):
        request = self.request(max_requests=2)
        grant = self.grant(request["id"], max_requests=2)
        self.operator.control().set_budget("fixture", "campaign", 2)
        self.assertTrue(self.authorize(grant["id"])["allowed"])
        self.assertTrue(self.authorize(grant["id"])["allowed"])
        exhausted = self.authorize(grant["id"])
        self.assertFalse(exhausted["allowed"])
        self.assertEqual("grant request budget exhausted", exhausted["reason"])
        restarted = Service(self.path / "agent-interface.sqlite3", "uid:agent")
        self.addCleanup(restarted.db.close)
        sid = restarted.session(self.sid)["session_id"]
        decision = restarted.control().authorize(restarted.context("fixture", "campaign"), {"session_id": sid}, grant["id"], "https://example.test/", "GET")
        self.assertFalse(decision["allowed"])
        status = restarted.control().budget_status("fixture", "campaign")
        self.assertEqual(2, status["requests_used"])

    def test_grant_binding_tampering_and_revocation(self):
        request = self.request()
        grant = self.grant(request["id"])
        self.operator.control().set_budget("fixture", "campaign", 10)
        # Method outside the grant allowlist.
        self.assertEqual("method is not permitted by the grant", self.authorize(grant["id"], method="POST")["reason"])
        # Out-of-scope URL.
        self.assertFalse(self.authorize(grant["id"], url="https://admin.example.test/")["allowed"])
        # Revoke then authorize.
        self.operator.control().revoke_approval(grant["id"], "test revocation")
        self.assertEqual("grant is not active", self.authorize(grant["id"])["reason"])
        # Context revision bump invalidates grants.
        request2 = self.request(skill="recon", workflow="js-recon", inputs={"OUTDIR": "/tmp/bb-fixture-out"})
        grant2 = self.grant(request2["id"])
        self.service.register("fixture", "campaign", "c" * 64, scope_text=SCOPE_TEXT)
        sid = self.service.open_session(dict(program_id="fixture", campaign_id="campaign", provider="codex", client_version="t2"))["session"]["session_id"]
        decision = self.service.control().authorize(
            self.service.context("fixture", "campaign"), {"session_id": sid}, grant2["id"], "https://example.test/", "GET"
        )
        self.assertFalse(decision["allowed"])
        self.assertIn("grant", decision["reason"])

    def test_concurrent_budget_consumption(self):
        request = self.request(max_requests=20)
        grant = self.grant(request["id"], max_requests=20)
        self.operator.control().set_budget("fixture", "campaign", 7)
        results = []
        barrier = threading.Barrier(8)

        def worker(index):
            service = Service(self.path / "agent-interface.sqlite3", "uid:agent")
            try:
                barrier.wait(timeout=10)
                sid = service.session(self.sid)["session_id"]
                context = service.context("fixture", "campaign")
                results.append(service.control().authorize(context, {"session_id": sid}, grant["id"], "https://example.test/", "GET")["allowed"])
            finally:
                service.db.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(8, len(results))
        self.assertEqual(7, sum(results))
        self.assertEqual(7, self.operator.control().budget_status("fixture", "campaign")["requests_used"])

    def test_audit_chain_redaction_and_tamper_detection(self):
        request = self.request()
        grant = self.grant(request["id"])
        self.operator.control().set_budget("fixture", "campaign", 10)
        self.authorize(grant["id"], url="https://example.test/?api_key=supersecretvalue123")
        self.authorize(grant["id"], url="https://evil.test/")
        verify = self.operator.control().audit_verify()
        self.assertTrue(verify["ok"], verify)
        self.assertGreaterEqual(verify["verified_events"], 5)
        events = self.operator.control().audit_list(program="fixture")["events"]
        blob = json.dumps(events)
        self.assertNotIn("supersecretvalue123", blob)
        types = {event["type"] for event in events}
        self.assertTrue({"proposed", "approved", "denied"} <= types, types)
        # Audit rows are trigger-protected against UPDATE and DELETE.
        row = self.operator.control().db.execute("SELECT seq,data FROM audit_events ORDER BY seq LIMIT 1").fetchone()
        event = json.loads(row["data"])
        event["detail"] = {"tampered": True}
        with self.assertRaises(sqlite3.Error):
            self.operator.control().db.execute("UPDATE audit_events SET data=? WHERE seq=?", (json.dumps(event), row["seq"]))
        self.operator.control().db.rollback()
        with self.assertRaises(sqlite3.Error):
            self.operator.control().db.execute("DELETE FROM audit_events WHERE seq=?", (row["seq"],))
        self.operator.control().db.rollback()

    def test_task_lifecycle_audit_events(self):
        task = dict(
            schema_version="1.0", task_id="t1", idempotency_key="k1", program_id="fixture", campaign_id="campaign",
            session_id=self.sid, skill="standard-catalog", workflow="search", inputs={"QUERY": "injection"},
            expected_context_revision=self.context["context_revision"], scope_digest=self.context["scope_digest"],
            policy_digest=self.context["policy_digest"], workflow_digest=self.workflow["workflow_digest"],
        )
        result = self.service.submit(task)
        self.assertEqual("succeeded", result["status"])
        types = [event["type"] for event in self.operator.control().audit_list(program="fixture")["events"]]
        self.assertIn("proposed", types)
        self.assertIn("started", types)
        self.assertIn("completed", types)

    def test_rate_limit_and_concurrency_budget(self):
        request = self.request(max_requests=10)
        grant = self.grant(request["id"], max_requests=10)
        self.operator.control().set_budget("fixture", "campaign", 10, min_interval_ms=60000)
        self.assertTrue(self.authorize(grant["id"])["allowed"])
        self.assertEqual("rate limit interval has not elapsed", self.authorize(grant["id"])["reason"])

    def test_submission_requires_exact_hash_grant(self):
        # Build a minimal finding through the governed review path.
        hypothesis = self.call("research.append", record={"kind": "hypothesis", "content": "Fixture boundary hypothesis"})
        finding = self.call("finding.create", hypothesis_id=hypothesis["id"], claim="Fixture claim", impact_class="data_exposure", profile="http_response")
        report = {"title": "Fixture report", "severity": "medium", "detail": "Synthetic report body"}
        denied = self.call("report.submit", finding_id=finding["id"], report=report)
        self.assertFalse(denied["submitted"])
        # Grant bound to a different report hash does not authorize.
        other = self.path / "other-report.json"
        other.write_text(json.dumps({"title": "Different"}))
        self.operator.operator_submission_grant(finding["id"], other.read_bytes(), 600)
        self.assertFalse(self.call("report.submit", finding_id=finding["id"], report=report)["submitted"])
        # Exact-hash grant authorizes once, then is consumed.
        report_file = self.path / "report.json"
        report_file.write_text(json.dumps(report, sort_keys=True))
        self.operator.operator_submission_grant(finding["id"], report_file.read_bytes(), 600)
        first = self.call("report.submit", finding_id=finding["id"], report=report)
        self.assertTrue(first["submitted"], first)
        self.assertFalse(first["delivered"])
        second = self.call("report.submit", finding_id=finding["id"], report=report)
        self.assertFalse(second["submitted"])
        # Revocation blocks a fresh grant.
        grant2 = self.operator.operator_submission_grant(finding["id"], report_file.read_bytes(), 600)
        self.operator.control().revoke_submission(grant2["id"], "withdrawn")
        self.assertFalse(self.call("report.submit", finding_id=finding["id"], report=report)["submitted"])

    def test_submission_grant_binds_finding_revision(self):
        hypothesis = self.call("research.append", record={"kind": "hypothesis", "content": "Revision-bound hypothesis"})
        finding = self.call("finding.create", hypothesis_id=hypothesis["id"], claim="Revision claim", impact_class="data_exposure", profile="http_response")
        report = {"title": "R", "detail": "d"}
        report_file = self.path / "r.json"
        report_file.write_text(json.dumps(report, sort_keys=True))
        self.operator.operator_submission_grant(finding["id"], report_file.read_bytes(), 600)
        # A finding change bumps revision and invalidates the grant.
        self.call("finding.history", finding_id=finding["id"])
        module, store = self.service.memory()
        try:
            reviews = load("bh_reviews_rev", ".claude/skills/impact-verifier/scripts/review_store.py").Reviews(store, "uid:agent")
            candidate = reviews.owned(finding["id"], self.context, {"session_id": self.sid})
            with store.db:
                reviews.save({**candidate, "claim": "Amended claim"}, "test_amendment", {"principal": "uid:agent"})
        finally:
            store.db.close()
        self.assertFalse(self.call("report.submit", finding_id=finding["id"], report=report)["submitted"])

    def test_cli_enforcement_commands(self):
        import subprocess
        import sys

        command = [sys.executable, str(ROOT / "bin/bb-agent"), "--database", str(self.path / "agent-interface.sqlite3")]
        request = self.request()
        result = subprocess.run(command + ["approval-grant", "--request-id", request["id"], "--ttl", "600"], text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)
        grant = json.loads(result.stdout)
        self.assertEqual("active", grant["status"])
        result = subprocess.run(command + ["budget-set", "--program-id", "fixture", "--campaign-id", "campaign", "--max-requests", "5"], text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)
        result = subprocess.run(command + ["audit-verify"], text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])
        result = subprocess.run(command + ["approval-revoke", "--grant-id", grant["id"], "--reason", "cli test"], text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_capabilities_advertise_enforcement(self):
        caps = capabilities()
        for op in ("approval.request", "dispatch.authorize", "report.submit", "budget.status"):
            self.assertIn(op, caps["operations"])
        self.assertNotIn("report.submit", caps["unavailable_operations"])
        self.assertFalse(caps["isolation_available"])


if __name__ == "__main__":
    unittest.main()
