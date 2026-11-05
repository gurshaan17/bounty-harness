"""Memory-first research proposals and a transactional offline queue; no target execution."""

import hashlib
import json
import math
import time
import uuid
from datetime import UTC, datetime

TERMINAL = {"investigated", "closed", "cancelled"}
TRANSITIONS = {
    "proposed": {"ready", "blocked", "closed", "cancelled"},
    "ready": {"awaiting_approval", "blocked", "closed", "cancelled"},
    "awaiting_approval": {"blocked", "closed", "cancelled"},
    "blocked": {"proposed", "closed", "cancelled"},
    "running": set(),
    "investigated": set(),
    "closed": set(),
    "cancelled": set(),
}
WEIGHTS = {"impact": 30, "evidence_quality": 25, "research_value": 30, "effort": 15}


class PlanningConflict(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 8192:
        raise ValueError("Nonempty bounded text required")
    return value.strip()


def strings(values):
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError("Bounded string list required")
    return [text(v) for v in values]


def expired(record):
    return bool(record.get("expires_at") and datetime.fromisoformat(record["expires_at"]) <= datetime.now(UTC))


def identity(program, opportunity):
    return digest([program, *[" ".join(opportunity[k].lower().split()) for k in ("surface", "boundary", "hypothesis")]])


def render(plan):
    lines = ["# Research plan", "", "Offline proposals only; planning grants no execution permission.", "", f"Input snapshot: {plan['input_digest']}", f"Plan: {plan['id']}", ""]
    for item in plan["opportunities"]:
        lines.extend(
            [
                f"## {item['question']}",
                "",
                f"Surface: {item['surface']}; boundary: {item['boundary']}",
                f"Hypothesis: {item['hypothesis']}",
                f"Score: {item['score']}; {item['rationale']}",
                f"Method: {json.dumps(item['method'], sort_keys=True)}",
                f"Stop: {item['stop_condition']}",
                f"Queue task: {item['task_id']}",
                f"Reopened: {item.get('reopen_reason') or 'no'}",
                "",
            ]
        )
    for key in ("suppressed", "ineligible"):
        lines.extend([f"## {key.capitalize()}", "", *[f"- {x['identity']}: {x['reason']}" for x in plan[key]], ""])
    return "\n".join(lines)


class Planner:
    def __init__(self, store, program, campaign, provenance, workflows=()):
        self.store, self.db = store, store.db
        self.program, self.campaign, self.provenance = program, campaign, provenance
        self.workflows = {(w["skill"], w["workflow"]): w for w in workflows}

    def context_digest(self):
        return digest({k: self.provenance.get(k) for k in ("context_revision", "scope_digest", "policy_digest")})

    def rows(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM research_queue WHERE program=? AND campaign=? ORDER BY id", (self.program, self.campaign))]

    def get(self, task_id):
        row = self.db.execute("SELECT data FROM research_queue WHERE program=? AND campaign=? AND id=?", (self.program, self.campaign, task_id)).fetchone()
        if not row:
            raise ValueError("Unknown task")
        return json.loads(row[0])

    def save(self, row, action, reason):
        self.store_scan({"row": row, "reason": reason, "provenance": self.provenance})
        row = {**row, "revision": row.get("revision", 0) + 1}
        event = {"task_id": row["id"], "revision": row["revision"], "action": action, "reason": reason, "at": datetime.now(UTC).isoformat(), "provenance": self.provenance, "record": row}
        self.db.execute("INSERT OR REPLACE INTO research_queue VALUES(?,?,?,?,?)", (self.program, self.campaign, row["id"], row["revision"], json.dumps(row)))
        self.db.execute("INSERT INTO queue_events VALUES(?,?,?,?,?,?)", (str(uuid.uuid4()), self.program, self.campaign, row["id"], row["revision"], json.dumps(event)))
        return row

    def store_scan(self, value):
        # Reuse the canonical recursive scanner, including legacy secret detection.
        self.store.validate_payload(value)

    def history(self, task_id):
        self.get(task_id)
        return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM queue_events WHERE program=? AND campaign=? AND task=? ORDER BY revision", (self.program, self.campaign, task_id))]

    def recover(self):
        for row in self.rows():
            if row["state"] == "running" and row["lease"]["expires_at"] <= time.time():
                row.update(state="blocked", lease=None, reason="Lease expired; outcome unknown; reconcile before new work", unknown_outcome=True)
                self.save(row, "interrupted", row["reason"])

    def listing(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.recover()
            result = sorted(self.rows(), key=lambda r: (r.get("deferred", False), r.get("position", 1000), -r["opportunity"]["score"], r["id"]))
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def _generated(self, records):
        components = [r for r in records if r["kind"] == "component"]
        roles = [r for r in records if r["kind"] == "role"]
        functions = [r for r in records if r["kind"] == "functionality"]
        if not components or not roles or not functions:
            missing = ", ".join(name for name, group in [("components", components), ("roles", roles), ("functionality/routes", functions)] if not group)
            return [
                dict(
                    question=f"What {missing} define this application?",
                    hypothesis="Application boundaries are not sufficiently understood",
                    surface="application",
                    boundary="unknown architecture",
                    roles=[],
                    potential_impact="Unknown until application boundaries are mapped",
                    supporting=[r["id"] for r in (components + roles + functions)[:32]],
                    contradicting=[],
                    alternative_explanations=["Relevant architecture may exist in unlinked source material"],
                    prerequisites=[],
                    method={"kind": "offline_analysis"},
                    expected_artifacts=["application and role map with source references"],
                    stop_condition="Record components, roles and functionality/routes with their trust boundaries",
                    dependencies=[],
                )
            ]
        proposals = []
        role_names = [r["content"] for r in roles[:8]]
        for function in functions[:40]:
            details = function.get("details", {})
            surface = details.get("surface", function["content"])
            if not isinstance(surface, str):
                continue
            boundaries = [("object ownership", "Does object ownership remain enforced for each role?"), ("tenant/role isolation", "Do tenant and role boundaries hold for this functionality?")]
            if details.get("invariants") or details.get("transitions"):
                boundaries.append(("workflow invariants", "Are invariants preserved across workflow transitions?"))
            for boundary, question in boundaries:
                sources = [function["id"]] + [r["id"] for r in (components[:2] + roles[:8])]
                linked = [r for r in records if function["id"] in r.get("sources", []) and r["kind"] in ("evidence", "investigation", "negative_result", "coverage", "hypothesis")]
                hypotheses = [r for r in linked if r["kind"] == "hypothesis"]
                proposals.append(
                    dict(
                        question=f"{question} Surface: {surface}",
                        hypothesis=hypotheses[0]["content"] if hypotheses else f"{boundary} may differ across roles on {surface}",
                        hypothesis_id=hypotheses[0]["id"] if hypotheses else None,
                        surface=surface,
                        boundary=boundary,
                        roles=role_names,
                        potential_impact=f"Unexpected access across {boundary} on {surface}",
                        supporting=(sources + [r["id"] for r in linked if r["kind"] not in ("negative_result", "hypothesis")])[:32],
                        contradicting=[r["id"] for r in linked if r["kind"] == "negative_result"][:32],
                        alternative_explanations=["Documented sharing or role policy may explain the behavior"],
                        prerequisites=["Understand documented role policy and existing traffic before proposing any probe"],
                        method={"kind": "offline_analysis"},
                        expected_artifacts=["source-linked boundary assessment", "smallest permitted follow-up question"],
                        stop_condition="Explain the boundary from existing evidence or record exactly which evidence is missing",
                        dependencies=[],
                    )
                )
        return proposals

    def normalize(self, proposal, records):
        required = {
            "question",
            "hypothesis",
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
        if not isinstance(proposal, dict) or not required <= proposal.keys() or proposal.keys() - required - {"hypothesis_id"}:
            raise ValueError("Invalid opportunity fields; model scores and states are forbidden")
        self.store_scan(proposal)
        result = {k: text(proposal[k]) for k in ("question", "hypothesis", "surface", "boundary", "potential_impact", "stop_condition")}
        for key in ("roles", "supporting", "contradicting", "alternative_explanations", "prerequisites", "expected_artifacts", "dependencies"):
            result[key] = strings(proposal[key])
        if not result["expected_artifacts"] or not result["alternative_explanations"]:
            raise ValueError("Artifacts and alternative explanations required")
        for key in ("supporting", "contradicting"):
            if any(ref not in records for ref in result[key]):
                raise ValueError("References must belong to visible program research")
        hypothesis_id = proposal.get("hypothesis_id")
        if hypothesis_id is not None and (hypothesis_id not in records or records[hypothesis_id]["kind"] != "hypothesis"):
            raise ValueError("Invalid hypothesis reference")
        result["hypothesis_id"] = hypothesis_id
        result["method"] = proposal["method"]
        result["identity"] = identity(self.program, result)
        return result

    def eligible(self, method):
        if not isinstance(method, dict):
            return False
        if method == {"kind": "offline_analysis"}:
            return True
        if set(method) != {"kind", "skill", "workflow"} or method["kind"] != "workflow":
            return False
        entry = self.workflows.get((method["skill"], method["workflow"]))
        return bool(entry and entry.get("security_metadata_complete") and entry.get("safety_tier") in ("passive", "active-safe", "intrusive") and entry.get("side_effects") in ([], ["network_read"]))

    def score(self, opportunity, records, weights):
        refs = [records[r] for r in set(opportunity["supporting"] + opportunity["contradicting"])]
        evidence = [r for r in refs if r["kind"] in ("evidence", "investigation") and not expired(r)]
        coverage = [r for r in refs if r["kind"] == "coverage" and not expired(r) and r.get("details", {}).get("coverage_status") == "adequately_investigated"]
        components = {
            "impact": 0.5 if opportunity["boundary"] == "unknown architecture" else 0.8,
            "evidence_quality": min(1.0, len(evidence) / 3),
            "research_value": 0.4 if coverage else 1.0,
            "effort": 1 / (1 + len(opportunity["prerequisites"])),
        }
        score = round(sum(weights[k] * components[k] for k in weights), 6)
        return {
            **opportunity,
            "workflow_digest": self.workflows.get((opportunity["method"].get("skill"), opportunity["method"].get("workflow")), {}).get("workflow_digest"),
            "score_components": components,
            "score": score,
            "expected_information_gain": "Resolve an undocumented boundary or identify the specific missing evidence",
            "cost": {"prerequisite_count": len(opportunity["prerequisites"]), "method": opportunity["method"]["kind"]},
            "evidence_quality": components["evidence_quality"],
            "rationale": f"Boundary relevance {components['impact']}; {len(evidence)} current evidence/investigation sources; "
            f"{len(coverage)} current coverage sources; {len(opportunity['prerequisites'])} prerequisites. Weights: {weights}",
        }

    def generate(self, proposals=None, weights=None):
        weights = WEIGHTS if weights is None else weights
        if (
            not isinstance(weights, dict)
            or set(weights) != set(WEIGHTS)
            or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in weights.values())
            or not math.isfinite(sum(weights.values()))
            or sum(weights.values()) <= 0
        ):
            raise ValueError("Invalid ranking weights")
        weights = {k: v / sum(weights.values()) for k, v in weights.items()}
        if proposals is not None and (not isinstance(proposals, list) or len(proposals) > 100):
            raise ValueError("Invalid proposals")
        self.store_scan(proposals or [])
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.recover()
            snapshot, fingerprint = self.store.snapshot(self.program)
            records = {r["id"]: r for r in snapshot}
            queue = self.rows()
            queue_revisions = {r["id"]: r["revision"] for r in queue}
            prior_queue = [
                (r["campaign"], json.loads(r["data"]))
                for r in self.db.execute("SELECT campaign,data FROM research_queue WHERE program=? AND campaign!=? ORDER BY rowid DESC", (self.program, self.campaign))
            ]
            generated = self._generated(snapshot) if proposals is None else proposals
            # Missing application knowledge always produces understanding work before supplied probe ideas.
            if proposals is not None and any(not any(r["kind"] == kind for r in snapshot) for kind in ("component", "role", "functionality")):
                generated = self._generated(snapshot)
            opportunities, suppressed, ineligible, seen = [], [], [], set()
            for proposal in generated:
                item = self.normalize(proposal, records)
                key = item["identity"]
                if key in seen:
                    continue
                seen.add(key)
                if not self.eligible(item["method"]):
                    ineligible.append({"identity": key, "reason": "Unknown, unsafe or incomplete method metadata"})
                    continue
                item = self.score(item, records, weights)
                current = next((r for r in queue if r["identity"] == key), None)
                foreign = False
                if not current:
                    current = next((r for _, r in prior_queue if r["identity"] == key), None)
                    foreign = current is not None
                if foreign and current["state"] not in TERMINAL:
                    suppressed.append({"identity": key, "reason": "Existing investigation in another campaign"})
                    continue
                negatives = [r for r in snapshot if r["kind"] == "negative_result" and r.get("details", {}).get("identity") == key]
                # Imported negative results can identify the same surface/boundary/hypothesis without queue IDs.
                negatives += [r for r in snapshot if r["kind"] == "negative_result" and all(r.get("details", {}).get(k) == item[k] for k in ("surface", "boundary", "hypothesis"))]
                method_hash = digest(item["method"])
                pins = {ref: records[ref]["revision"] for ref in set(item["supporting"] + item["contradicting"])}
                if item["hypothesis_id"]:
                    pins[item["hypothesis_id"]] = records[item["hypothesis_id"]]["revision"]
                reopen = None
                if current:
                    if current.get("unknown_outcome"):
                        suppressed.append({"identity": key, "reason": "Unknown remote outcome requires operator reconciliation"})
                        continue
                    if current["state"] == "running":
                        suppressed.append({"identity": key, "reason": "Existing leased investigation"})
                        continue
                    if (
                        current["state"] not in TERMINAL
                        and current["snapshot_digest"] == fingerprint
                        and current.get("context_digest") == self.context_digest()
                        and all(current["opportunity"].get(k) == v for k, v in item.items())
                    ):
                        opportunities.append(current["opportunity"])
                        continue
                    if current["state"] not in TERMINAL:
                        reopen = "Plan assumptions changed; reset pending proposal for review"
                    elif {k: v for k, v in current["source_revisions"].items() if k in records and records[k]["kind"] not in ("negative_result", "investigation", "coverage")} != {
                        k: v for k, v in pins.items() if records[k]["kind"] not in ("negative_result", "investigation", "coverage")
                    }:
                        reopen = "Application/source facts changed"
                    elif digest(current["opportunity"]["method"]) != method_hash:
                        reopen = "Substantially different registered method"
                    elif any(expired(r) for r in negatives):
                        reopen = "Negative evidence expired"
                    else:
                        suppressed.append({"identity": key, "reason": "Same prior task inputs already investigated or disposed"})
                        continue
                elif negatives:
                    latest = negatives[-1]
                    old_pins = latest.get("source_revisions", {})
                    if expired(latest):
                        reopen = "Negative evidence expired"
                    elif old_pins and any(ref in records and records[ref]["revision"] != rev for ref, rev in old_pins.items()):
                        reopen = "Application/source facts changed"
                    elif latest.get("details", {}).get("method") and digest(latest["details"]["method"]) != method_hash:
                        reopen = "Substantially different registered method"
                    else:
                        suppressed.append({"identity": key, "reason": "Current negative result covers the same investigation"})
                        continue
                task_id = current["id"] if current else key
                item.update(task_id=task_id, reopen_reason=reopen)
                row = {
                    "id": task_id,
                    "identity": key,
                    "trust": "agent-unreviewed",
                    "sensitivity": "program-private" if any(records[ref]["sensitivity"] == "program-private" for ref in pins) else "report-safe",
                    "opportunity": item,
                    "state": "proposed",
                    "revision": current["revision"] if current and not foreign else 0,
                    "source_revisions": pins,
                    "snapshot_digest": fingerprint,
                    "context_digest": self.context_digest(),
                    "dependencies": current["dependencies"] if current and not foreign else item["dependencies"],
                    "lease": None,
                    "reason": reopen or "Research proposal",
                    "unknown_outcome": False,
                    "hypothesis_verdict": "unresolved",
                    "position": current.get("position", 1000) if current else 1000,
                    "deferred": current.get("deferred", False) if current else False,
                }
                if task_id in row["dependencies"] or any(dep not in {q["id"] for q in queue} for dep in row["dependencies"]):
                    raise ValueError("Unknown or cyclic dependency")
                graph = {q["id"]: q["dependencies"] for q in queue}
                graph[task_id] = row["dependencies"]
                self.check_dag(graph)
                self.save(row, "reopen" if reopen else "propose", row["reason"])
                opportunities.append(item)
            opportunities.sort(key=lambda r: (-r["score"], r["identity"]))
            plan = {
                "schema_version": "1.0",
                "program_id": self.program,
                "campaign_id": self.campaign,
                "input_digest": fingerprint,
                "context_digest": self.context_digest(),
                "context": {k: self.provenance.get(k) for k in ("context_revision", "scope_digest", "policy_digest")},
                "input_revisions": {r["id"]: r["revision"] for r in snapshot},
                "queue_revisions": queue_revisions,
                "prior_queue_revisions": {campaign + "/" + r["id"]: r["revision"] for campaign, r in prior_queue},
                "weights": weights,
                "opportunities": opportunities,
                "suppressed": suppressed,
                "ineligible": ineligible,
                "grants_execution": False,
            }
            plan["id"] = digest(plan)
            plan["markdown"] = render(plan)
            self.db.execute("INSERT OR IGNORE INTO research_plans VALUES(?,?,?,?)", (self.program, self.campaign, plan["id"], json.dumps(plan)))
            self.db.commit()
            return plan
        except BaseException:
            self.db.rollback()
            raise

    @staticmethod
    def check_dag(graph):
        visiting, visited = set(), set()

        def visit(node):
            if node in visiting:
                raise ValueError("Cyclic dependencies")
            if node in visited:
                return
            visiting.add(node)
            for child in graph.get(node, []):
                visit(child)
            visiting.remove(node)
            visited.add(node)

        for node in graph:
            visit(node)

    def plan(self, plan_id):
        row = self.db.execute("SELECT data FROM research_plans WHERE program=? AND campaign=? AND id=?", (self.program, self.campaign, plan_id)).fetchone()
        if not row:
            raise ValueError("Unknown plan")
        plan = json.loads(row[0])
        _, fingerprint = self.store.snapshot(self.program)
        return {**plan, "stale": fingerprint != plan["input_digest"] or plan.get("context_digest") != self.context_digest()}

    def checked(self, task_id, revision):
        row = self.get(task_id)
        if type(revision) is not int or row["revision"] != revision:
            raise PlanningConflict("Stale queue revision")
        return row

    def fresh(self, row):
        _, fingerprint = self.store.snapshot(self.program)
        if fingerprint != row["snapshot_digest"] or row.get("context_digest") != self.context_digest():
            raise PlanningConflict("Plan inputs changed; reconcile or regenerate proposal")
        if any(self.get(dep)["state"] not in TERMINAL for dep in row["dependencies"]):
            raise ValueError("Unfinished dependencies")
        if row.get("deferred"):
            raise ValueError("Task deferred by operator")

    def transition(self, task_id, revision, state, reason):
        reason = text(reason)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.recover()
            row = self.checked(task_id, revision)
            if state not in TRANSITIONS[row["state"]]:
                raise ValueError("Invalid queue transition")
            if row.get("unknown_outcome") and state == "proposed":
                raise ValueError("Unknown outcome requires operator reconciliation")
            if state in ("ready", "awaiting_approval"):
                self.fresh(row)
            row.update(state=state, reason=reason)
            result = self.save(row, "transition", reason)
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def claim(self, task_id, revision, ttl=30):
        if type(ttl) is not int or not 1 <= ttl <= 300:
            raise ValueError("Invalid lease duration")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.recover()
            row = self.checked(task_id, revision)
            if row["state"] != "ready" or row["opportunity"]["method"] != {"kind": "offline_analysis"}:
                raise ValueError("Only ready offline analysis can be claimed; active execution unavailable")
            self.fresh(row)
            row.update(state="running", lease={"token": str(uuid.uuid4()), "session_id": self.provenance["session_id"], "actor": self.provenance["actor"], "expires_at": time.time() + ttl})
            result = self.save(row, "claim", "Offline analysis lease")
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def leased(self, task_id, revision, token):
        row = self.checked(task_id, revision)
        lease = row.get("lease")
        if (
            row["state"] != "running"
            or not lease
            or lease["token"] != token
            or lease["expires_at"] <= time.time()
            or lease["session_id"] != self.provenance["session_id"]
            or lease["actor"] != self.provenance["actor"]
        ):
            raise ValueError("Invalid or expired lease")
        return row

    def heartbeat(self, task_id, revision, token, ttl=30):
        if type(ttl) is not int or not 1 <= ttl <= 300:
            raise ValueError("Invalid lease duration")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.leased(task_id, revision, token)
            row["lease"]["expires_at"] = time.time() + ttl
            result = self.save(row, "heartbeat", "Offline analysis heartbeat")
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def stop(self, task_id, revision, token, state, reason):
        reason = text(reason)
        if state not in ("cancelled", "blocked"):
            raise ValueError("Offline analysis may stop as cancelled or blocked")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.leased(task_id, revision, token)
            row.update(state=state, lease=None, reason=reason)
            saved = self.save(row, "stop", reason)
            self.db.commit()
            return saved
        except BaseException:
            self.db.rollback()
            raise

    def complete(self, task_id, revision, token, result, verdict):
        result = text(result)
        if verdict not in ("supported", "rejected", "inconclusive", "needs_review"):
            raise ValueError("Invalid hypothesis verdict; confirmation requires later review")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.leased(task_id, revision, token)
            self.fresh(row)
            item = row["opportunity"]
            sources = list(row["source_revisions"])[:32]
            detail = {
                "identity": row["identity"],
                "surface": item["surface"],
                "boundary": item["boundary"],
                "hypothesis": item["hypothesis"],
                "method": item["method"],
                "verdict": verdict,
                "task_id": task_id,
                "limitations": "Offline analysis only; no active verification or human confirmation",
            }
            saved = []
            kinds = ["investigation", "coverage"] + (["negative_result"] if verdict == "rejected" else [])
            for kind in kinds:
                record = self.store.append_in_transaction(
                    self.program, {"kind": kind, "content": result, "sources": sources, "details": {**detail, "coverage_status": "attempted"} if kind == "coverage" else detail}, self.provenance
                )
                saved.append(record)
            row.update(state="investigated", lease=None, hypothesis_verdict=verdict, result_ids=[r["id"] for r in saved])
            row = self.save(row, "complete", "Investigation and coverage recorded atomically")
            self.db.commit()
            return row
        except BaseException:
            self.db.rollback()
            raise

    def dependencies(self, task_id, revision, dependencies, reason):
        dependencies, reason = strings(dependencies), text(reason)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.checked(task_id, revision)
            if row["state"] not in ("proposed", "blocked"):
                raise ValueError("Dependencies can change only before readiness")
            graph = {q["id"]: q["dependencies"] for q in self.rows()}
            if any(dep not in graph for dep in dependencies):
                raise ValueError("Unknown dependency")
            graph[task_id] = dependencies
            self.check_dag(graph)
            row["dependencies"] = dependencies
            saved = self.save(row, "dependencies", reason)
            self.db.commit()
            return saved
        except BaseException:
            self.db.rollback()
            raise

    def dispose(self, task_id, revision, reason):
        """Operator disposition closes work without asserting success or human review."""
        reason = text(reason)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.recover()
            row = self.checked(task_id, revision)
            if row["state"] == "running":
                raise ValueError("Cannot dispose a live lease")
            row.update(state="closed", unknown_outcome=False, lease=None, reason=reason, operator_disposed=True)
            saved = self.save(row, "operator-dispose", reason)
            self.db.commit()
            return saved
        except BaseException:
            self.db.rollback()
            raise

    def adjust(self, task_id, revision, position, deferred, reason):
        """Operator CLI only. Adjustment cannot mint execution or review authority."""
        if type(position) is not int or not 0 <= position <= 100000 or type(deferred) is not bool:
            raise ValueError("Invalid operator queue ordering")
        reason = text(reason)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.checked(task_id, revision)
            if row["state"] == "running":
                raise ValueError("Cannot defer a live task")
            row.update(position=position, deferred=deferred)
            saved = self.save(row, "operator-adjust", reason)
            self.db.commit()
            return saved
        except BaseException:
            self.db.rollback()
            raise


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, epilog="Use bin/bb-agent planning and queue operations; see docs/phase4-planning.md.")
    parser.parse_args()
