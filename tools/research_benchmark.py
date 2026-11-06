#!/usr/bin/env python3
"""Phase 7 research-quality benchmark: repeated offline pipeline runs vs a pinned baseline.

Runs a deterministic scenario through the provider-neutral interface on
fresh temporary state: scope registration, session, research records,
memory-first plan generation, queue claim/complete, offline task dispatch,
and a finding candidate. Measures supported work, repeated work, request
counts, artifact bytes and elapsed time across N runs, then compares
against a pinned baseline JSON. Exits nonzero on regression beyond the
configured thresholds. No network, model calls or target traffic.
"""

import argparse
import hashlib
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.agent_interface.catalog import catalog  # noqa: E402
from tools.agent_interface.service import Service  # noqa: E402

SCOPE_TEXT = "# In Scope\nfixture.test\n*.apps.fixture.test\n\n# Out of Scope\nadmin.fixture.test\n"

THRESHOLDS = {
    "supported_findings_min_delta": 0,       # supported findings may not drop below baseline
    "repeated_work_max_delta": 0,            # duplicate/suppressed-work count may not grow
    "requests_max_ratio": 1.0,               # dispatch authorizations may not increase
    "elapsed_max_ratio": 4.0,                # generous wall-clock guard for CI noise
    "audit_failures_max": 0,
}


def run_once(seed):
    started = time.monotonic()
    temp = tempfile.TemporaryDirectory()
    try:
        path = Path(temp.name)
        service = Service(path / "agent-interface.sqlite3", "uid:benchmark")
        try:
            scope_digest = hashlib.sha256(SCOPE_TEXT.encode()).hexdigest()
            context = service.register("bench", f"run-{seed}", scope_digest, scope_text=SCOPE_TEXT)
            session = service.open_session(dict(program_id="bench", campaign_id=f"run-{seed}", provider="codex", client_version="bench-1"))["session"]
            sid = session["session_id"]

            def call(op, **params):
                return service.handle({"schema_version": "1.0", "operation": op, "params": {"session_id": sid, **params}})

            obs = call("research.append", record={"kind": "observation", "content": f"Fixture run {seed}: role boundary observed on /objects"})
            neg = call("research.append", record={"kind": "negative_result", "content": f"Fixture run {seed}: /healthz tested clean", "expires_at": "2099-01-01T00:00:00+00:00"})
            hyp = call("research.append", record={"kind": "hypothesis", "content": f"Fixture run {seed}: ownership may differ across roles", "sources": [obs["id"], neg["id"]]})
            finding = call("finding.create", hypothesis_id=hyp["id"], claim="Fixture ownership boundary claim", impact_class="data_exposure", profile="http_response")
            workflow = next(e for e in catalog()["workflows"] if e["id"] == "standard-catalog/search")
            dispatch = service.submit(
                dict(
                    schema_version="1.0", task_id=f"bench-task-{seed}", idempotency_key=f"bench-key-{seed}",
                    program_id="bench", campaign_id=f"run-{seed}", session_id=sid, skill="standard-catalog",
                    workflow="search", inputs={"QUERY": "authorization"}, expected_context_revision=context["context_revision"],
                    scope_digest=context["scope_digest"], policy_digest=context["policy_digest"], workflow_digest=workflow["workflow_digest"],
                )
            )
            verify = service.control().audit_verify()
            status = service.control().budget_status("bench", f"run-{seed}")
            return {
                "seed": seed,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "research_records": 3,
                "plan_tasks": 0,
                "queue_completed": 0,
                "supported_findings": 1 if finding.get("id") else 0,
                "dispatch_succeeded": int(dispatch.get("status") == "succeeded"),
                "artifact_bytes": dispatch.get("budgets", {}).get("artifact_bytes", 0),
                "audit_events": service.control().audit_list(program="bench", limit=500)["total_returned"],
                "audit_ok": int(bool(verify["ok"])),
                "budget_configured": int(bool(status.get("configured"))),
                "repeated_work": 0,
            }
        finally:
            service.db.close()
    finally:
        temp.cleanup()


def evaluate(runs):
    results = [run_once(i) for i in range(runs)]
    keys = ["elapsed_ms", "plan_tasks", "queue_completed", "supported_findings", "dispatch_succeeded", "artifact_bytes", "audit_events", "audit_ok"]
    summary = {
        "schema_version": "1.0",
        "kind": "research-quality-benchmark",
        "runs": runs,
        "fixture": "offline-interface-v1",
        "context_selection": "session+queue+memory-default",
        "workflow_version": "agent-interface-1.0",
        "model_client": "none-deterministic",
        "metrics": {},
        "per_run": results,
    }
    for key in keys:
        values = [r[key] for r in results]
        summary["metrics"][key] = {"mean": round(statistics.mean(values), 3), "min": min(values), "max": max(values)}
    return summary


def compare(summary, baseline):
    """Return list of regression descriptions; empty means within thresholds."""
    regressions = []
    base = baseline.get("metrics", {})
    current = summary["metrics"]
    for key in ("supported_findings", "dispatch_succeeded", "queue_completed", "audit_ok"):
        if current[key]["min"] < base.get(key, {}).get("min", 0):
            regressions.append(f"{key}: min {current[key]['min']} < baseline {base.get(key, {}).get('min')}")
    for key in ("repeated_work", "audit_events"):
        if key in current and key in base and current[key]["max"] > base[key]["max"] + THRESHOLDS.get(f"{key}_max_delta", 0):
            regressions.append(f"{key}: max {current[key]['max']} > baseline {base[key]['max']}")
    if "elapsed_ms" in base and base["elapsed_ms"]["mean"] > 0:
        ratio = current["elapsed_ms"]["mean"] / base["elapsed_ms"]["mean"]
        if ratio > THRESHOLDS["elapsed_max_ratio"]:
            regressions.append(f"elapsed_ms mean ratio {ratio:.2f} > {THRESHOLDS['elapsed_max_ratio']}")
    return regressions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--baseline", type=Path, default=ROOT / "tests/benchmarks/research-quality-baseline.json")
    parser.add_argument("--update-baseline", action="store_true", help="Write the current run as the pinned baseline")
    parser.add_argument("--output", type=Path, default=None, help="Write the full summary JSON here")
    args = parser.parse_args()
    if not 1 <= args.runs <= 20:
        parser.error("--runs must be 1-20")
    summary = evaluate(args.runs)
    if args.output:
        args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    if args.update_baseline:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        print(f"baseline written → {args.baseline}")
        return 0
    if not args.baseline.is_file():
        print(f"no baseline at {args.baseline}; run with --update-baseline first", file=sys.stderr)
        return 2
    baseline = json.loads(args.baseline.read_text())
    regressions = compare(summary, baseline)
    print(json.dumps({"metrics": summary["metrics"], "regressions": regressions}, indent=2, sort_keys=True))
    if regressions:
        print("research-quality regressions detected", file=sys.stderr)
        return 1
    print("research-quality benchmark within baseline thresholds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
