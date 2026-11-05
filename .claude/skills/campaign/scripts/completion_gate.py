"""Deterministic completion assessment. Model declarations cannot discharge obligations."""


def evaluate(store, program, campaign, queue, stop_reason=None, budget_exhausted=False):
    obligations = []
    for task in queue:
        if task["state"] not in ("investigated", "closed", "cancelled"):
            obligations.append({"kind": "task", "id": task["id"], "reason": task.get("reason") or task["state"]})
        elif task["state"] == "investigated":
            if task.get("hypothesis_verdict") in ("unresolved", "inconclusive", "needs_review"):
                obligations.append(
                    {"kind": "review" if task.get("hypothesis_verdict") == "needs_review" else "hypothesis", "id": task["id"], "reason": "Investigation leaves an unresolved obligation"}
                )
            for source in task.get("result_ids", []):
                if not store.get(program, source):
                    obligations.append({"kind": "artifact", "id": source, "reason": "Expected result record missing"})
    records, _ = store.snapshot(program)
    # Bind research obligations by service-established campaign provenance, not model details.
    campaign_ids = {row[0] for row in store.db.execute("SELECT entity FROM research_events WHERE program=? AND json_extract(data, '$.provenance.campaign_id')=?", (program, campaign))}
    hidden_obligations = store.db.execute(
        "SELECT count(*) FROM research_entities WHERE program=? AND id IN "
        "(SELECT entity FROM research_events WHERE program=? AND json_extract(data, '$.provenance.campaign_id')=?) "
        "AND json_extract(data, '$.kind') IN ('hypothesis','question')",
        (program, program, campaign),
    ).fetchone()[0]
    visible_obligations = sum(r["id"] in campaign_ids and r["kind"] in ("hypothesis", "question") for r in records)
    if hidden_obligations > visible_obligations:
        obligations.append({"kind": "restricted_obligation", "id": "restricted", "reason": "Restricted or unsafe research requires operator reconciliation"})
    for record in records:
        if record["id"] not in campaign_ids or record["kind"] not in ("hypothesis", "question"):
            continue
        related = [q for q in queue if record["id"] in q["source_revisions"] or q["opportunity"].get("hypothesis_id") == record["id"]]
        if record.get("details", {}).get("obligation") == "review":
            resolved = any(q["state"] == "closed" and q.get("operator_disposed") for q in related)
        else:
            resolved = any(q["state"] in ("closed", "cancelled") or (q["state"] == "investigated" and q["hypothesis_verdict"] in ("supported", "rejected")) for q in related)
        if not resolved:
            obligations.append({"kind": "review" if record.get("details", {}).get("obligation") == "review" else record["kind"], "id": record["id"], "reason": "No recorded resolution or disposition"})
    status = "operator_stopped" if stop_reason else "budget_exhausted" if budget_exhausted else "blocked" if obligations else "completed"
    return {"status": status, "can_complete": status == "completed", "obligations": obligations, "stop_reason": stop_reason, "grants_execution": False, "submission_approved": False}


if __name__ == "__main__":
    import argparse

    argparse.ArgumentParser(description=__doc__, epilog="Used by campaign_runner.py and bin/bb-agent campaign.evaluate.").parse_args()
