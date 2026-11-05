#!/usr/bin/env python3
"""Autonomous bug-bounty campaign runner.

Drives the full toolkit end-to-end from a single target: context init, recon,
domain modeling, planning, then priority-ordered execution of every applicable
skill workflow (filtered by safety tier), then reporting. Bounded by a wall-clock
time budget, gated so intrusive testing requires a scope/authorization file,
resumable, and resilient (one failing workflow never aborts the campaign).

This is the deterministic engine. The judgement parts — web-search target
understanding, triage, and impact verification — are driven by the agent per
.claude/skills/campaign/SKILL.md.

Usage:
    campaign_runner.py hunt --target example.com [--program NAME] [--scope-file FILE]
        [--max-tier passive|active-safe|intrusive] [--time-budget 2h]
        [--skills auth,api,xss] [--rate-limit N] [--workflow-timeout 600]
        [--resume CAMPAIGN_ID] [--no-init] [--dry-run]
    campaign_runner.py status [--campaign CAMPAIGN_ID]

Run from the repository root (same as bin/bb-run).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import hashlib
import shlex
import uuid
import signal
import fcntl
from recovery import Recovery, atomic_json, hash_value
from datetime import datetime, timezone
from pathlib import Path

TIERS = {"passive": 0, "active-safe": 1, "intrusive": 2, "destructive-manual": 3}
_private_root = os.environ.get('BB_DATA_ROOT')
if _private_root and (not Path(_private_root).is_absolute() or Path(_private_root).resolve().is_relative_to(Path(__file__).resolve().parents[4])):
    raise ValueError('BB_DATA_ROOT must be absolute and outside the repository')
CAMPAIGN_ROOT = Path(_private_root) / 'campaigns' if _private_root else Path('.bb/campaigns')

# Plan/skill category -> actual skill directory name.
SKILL_ALIASES = {
    "authorization": "cross-account",
    "authz": "cross-account",
    "oob": "oob-infra",
    "business": "business-logic",
    "llm": "ai-llm",
}

# Backbone: always run, in this order, regardless of plan. (skill, workflow, min_tier_to_include)
# min_tier "" means always include.
BACKBONE = [
    ("recon", "passive-subdomains", ""),
    ("recon", "live-discovery", ""),
    ("recon", "url-discovery", ""),
    ("recon", "js-recon", ""),
    ("recon", "dns-resolution", ""),
    ("recon", "tls-profile", ""),
    ("recon", "port-scan", "active-safe"),
    ("domain-model", "classify", ""),
    ("domain-model", "map-surfaces", ""),
    ("domain-model", "profile", ""),
    ("technique-kb", "match", ""),
]

# Skills excluded from the auto-execute sweep (backbone/meta/support, not target-probing).
EXECUTE_SKILL_DENYLIST = {
    "recon", "domain-model", "technique-kb", "planner", "reporting",
    "standard-catalog", "coverage", "auto-research", "evaluation-harness",
    "skill-scientist", "program-memory", "asset-graph", "traffic-corpus",
    "persona", "scope-manager", "vuln-intel", "impact-verifier", "campaign",
    "agent-safety", "osint",
}

# Default skill priority if no plan is available.
DEFAULT_SKILL_ORDER = [
    "cloud", "cors-csrf", "ssrf", "auth", "api", "xss", "sqli", "rce",
    "file-upload", "http-protocol", "cross-account", "business-logic",
    "race-condition", "ai-llm", "modern-browser", "oob-infra", "nuclei-scanner",
]


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_duration(s):
    """'2h' -> 7200, '90m' -> 5400, '45s' -> 45, '3600' -> 3600."""
    s = str(s).strip().lower()
    m = re.fullmatch(r"(\d+)\s*([hms]?)", s)
    if not m:
        raise ValueError(f"invalid duration: {s!r} (use e.g. 2h, 90m, 3600)")
    n, unit = int(m.group(1)), m.group(2) or "s"
    return n * {"h": 3600, "m": 60, "s": 1}[unit]


def load_context():
    ctx = {}
    p = Path(".bb/context.json")
    if p.is_file():
        try:
            ctx = json.loads(p.read_text())
        except Exception:
            pass
    return ctx


def list_workflows(skill):
    """Return [(workflow_name, safety_tier)] for a skill, or [] if none."""
    p = Path(".claude/skills") / skill / "skill.yaml"
    if not p.is_file():
        return []
    try:
        import yaml
        data = yaml.safe_load(p.read_text()) or {}
    except Exception:
        return []
    skill_tier = data.get("safety_tier", "")
    out = []
    for name, wf in (data.get("workflows") or {}).items():
        wf = wf or {}
        tier = wf.get("safety_tier") or skill_tier or "active-safe"
        out.append((name, tier))
    return out


def ranked_skills_from_plan(outdir):
    """Parse plan.json -> ordered unique list of real skill dir names by priority."""
    plan = Path(outdir) / "planner" / "plan.json"
    if not plan.is_file():
        return []
    try:
        data = json.loads(plan.read_text())
    except Exception:
        return []
    items = data.get("plan_items") or []
    PRI = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    items = sorted(
        items,
        key=lambda it: (PRI.get(str(it.get("priority", "low")).lower(), 5),
                        -float(it.get("score", 0) or 0)),
    )
    ordered = []
    for it in items:
        raw = str(it.get("skill", "")).strip()
        skill = SKILL_ALIASES.get(raw, raw)
        if not skill or not (Path(".claude/skills") / skill).is_dir():
            continue
        if skill not in ordered:
            ordered.append(skill)
    return ordered


class Campaign:
    def __init__(self, args):
        self.args = args
        self.max_tier = args.max_tier
        self.budget = parse_duration(args.time_budget)
        self.wf_timeout = int(args.workflow_timeout)
        self.dry = args.dry_run
        self.start = time.monotonic()
        self.completed = set()  # {(skill, workflow)}
        self.results = []
        self.campaign_id = None
        self.dir = None
        self.log_fh = None

    # ---- time budget ----
    def elapsed(self):
        return time.monotonic() - self.start

    def budget_left(self):
        return self.budget - self.elapsed()

    def over_budget(self):
        return self.elapsed() >= self.budget

    # ---- logging / state ----
    def log(self, msg):
        line = f"[{now_iso()}] +{int(self.elapsed())}s {msg}"
        print(line, flush=True)
        if self.log_fh:
            self.log_fh.write(line + "\n")
            self.log_fh.flush()

    def setup_dir(self, target, resume_id=None):
        if resume_id and not re.fullmatch(r"[A-Za-z0-9_.-]+", resume_id):
            raise ValueError("Invalid campaign ID")
        CAMPAIGN_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        if resume_id:
            self.campaign_id = resume_id
            self.dir = CAMPAIGN_ROOT / resume_id
            if not self.dir.is_dir():
                sys.exit(f"ERROR: campaign {resume_id} not found under {CAMPAIGN_ROOT}")
            # Execution database, not partial JSONL, owns completed task identity.
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", target)
            self.campaign_id = f"{safe}_{stamp}_{uuid.uuid4().hex[:8]}"
            self.dir = CAMPAIGN_ROOT / self.campaign_id
            self.dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.lock_fh = open(self.dir / 'campaign.lock', 'a')
        try:
            fcntl.flock(self.lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_fh.close()
            raise ValueError('Campaign is already running')
        self.recovery = Recovery(self.dir)
        self.log_fh = open(self.dir / "campaign.log", "a")
        self.results_fh = open(self.dir / "results.jsonl", "a")

    def _load_completed(self):
        rp = self.dir / "results.jsonl"
        if rp.is_file():
            for line in rp.read_text().splitlines():
                try:
                    r = json.loads(line)
                    if r.get("exit_code") == 0:
                        self.completed.add((r["skill"], r["workflow"]))
                except Exception:
                    pass

    def write_status(self, phase, extra=None):
        status = {
            "campaign_id": self.campaign_id,
            "target": self.args.target,
            "program": getattr(self, "program_id", getattr(self.args, "program", None)),
            "max_tier": self.max_tier,
            "time_budget_s": self.budget,
            "elapsed_s": int(self.elapsed()),
            "phase": phase,
            "workflows_run": len(self.results),
            "workflows_ok": sum(1 for r in self.results if r["exit_code"] == 0),
            "workflows_failed": sum(1 for r in self.results if r["exit_code"] != 0),
            "updated_at": now_iso(),
        }
        if extra:
            status.update(extra)
        if self.dir:
            atomic_json(self.dir / "status.json", status)
        return status

    # Explicit FSM: RECON->PLAN->EXECUTE->VERIFY->REPORT persisted so external
    # tooling and resumed runs read deterministic phase transitions.
    FSM_PHASES = ["init", "backbone", "execute", "report", "completed", "blocked", "budget_exhausted", "operator_stopped", "previewed"]

    def fsm_transition(self, new_phase, note=""):
        state_path = self.dir / "state.json" if self.dir else None
        try:
            state = json.loads(state_path.read_text()) if state_path and state_path.exists() else {
                "campaign_id": self.campaign_id, "history": []}
        except Exception:
            state = {"campaign_id": self.campaign_id, "history": []}
        prev = state.get("phase")
        entry = {"from": prev, "to": new_phase, "at": now_iso(), "note": note}
        if prev == new_phase:
            state["last_seen"] = now_iso()
            state["resumed_in_phase"] = True
        else:
            state["phase"] = new_phase
            state.setdefault("history", []).append(entry)
            if len(state["history"]) > 50:
                state["history"] = state["history"][-50:]
        state["updated_at"] = now_iso()
        atomic_json(state_path, state)

    # ---- execution primitives ----
    def run_cmd(self, argv, label, timeout=None):
        """Run a subprocess, stream nothing (capture), log result. Returns exit code."""
        if self.dry:
            self.log(f"DRY-RUN would execute: {' '.join(argv)}")
            return 0
        try:
            environment = os.environ.copy()
            if hasattr(self, 'context_file'):
                environment['BB_CONTEXT_FILE'] = str(self.context_file.resolve())
            process = subprocess.Popen(argv, env=environment, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
            deadline = time.monotonic() + (timeout or self.wf_timeout)
            try:
                while process.poll() is None:
                    if getattr(self, 'current_task', None):
                        self.recovery.heartbeat(self.current_task)
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    time.sleep(0.1)
                return process.returncode
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
        except subprocess.TimeoutExpired:
            self.log(f"    TIMEOUT after {timeout}s: {label}")
            return 124
        except Exception as e:
            self.log(f"    ERROR launching {label}: {e}")
            return 1

    def run_workflow(self, skill, workflow):
        if (skill, workflow) in self.completed:
            self.log(f"SKIP (already done): {skill}/{workflow}")
            return
        self.expect_workflow(skill, workflow)
        if self.over_budget():
            return
        remaining = max(1, int(self.budget_left()))
        timeout = min(self.wf_timeout, remaining)
        self.log(f"RUN  {skill}/{workflow}  (tier-ok, timeout {timeout}s)")
        task = skill + '/' + workflow
        package = Path('.claude/skills') / skill / 'skill.yaml'
        workflow_digest = hashlib.sha256(package.read_bytes()).hexdigest()
        if not self.dry and not self.recovery.claim(task, workflow_digest):
            self.completed.add((skill, workflow))
            return
        self.current_task = task if not self.dry else None
        code = self.run_cmd(["bin/bb-run", skill, workflow], f"{skill}/{workflow}", timeout=timeout)
        if not self.dry:
            self.recovery.finish(task, code)
        self.current_task = None
        rec = {"skill": skill, "workflow": workflow, "exit_code": code, "at": now_iso(),
               "elapsed_s": int(self.elapsed())}
        self.results.append(rec)
        if not self.dry:
            self.results_fh.write(json.dumps(rec) + "\n")
            self.results_fh.flush()
        if code == 0:
            self.completed.add((skill, workflow))
            self.log(f"  OK  {skill}/{workflow}")
        else:
            self.log(f"  FAIL(exit {code}) {skill}/{workflow} — continuing")

    def expect_workflow(self, skill, workflow):
        import yaml
        package = Path('.claude/skills') / skill / 'skill.yaml'
        data = yaml.safe_load(package.read_text())
        outputs = data['workflows'][workflow].get('outputs', [])
        outdir = getattr(self, 'output_root', '')
        resolved = [value.replace('${OUTDIR}', outdir).replace('$OUTDIR', outdir) if isinstance(value, str) else '<unresolved output descriptor>' for value in outputs]
        self.recovery.expect('workflow:' + skill + '/' + workflow, {'outputs': resolved})

    def assess_completion(self, program, stop_reason=None):
        import importlib.util
        memory_path = CAMPAIGN_ROOT.parent / 'memory.sqlite'
        obligations = self.recovery.completion()
        assessment = {'obligations': [], 'status': 'completed'}
        root = Path(__file__).resolve().parents[4]
        def load(name, relative):
            spec = importlib.util.spec_from_file_location(name, root / relative)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        if memory_path.is_file():
            memory = load('campaign_memory', '.claude/skills/program-memory/scripts/research_store.py')
            planning = load('campaign_planning', '.claude/skills/planner/scripts/research_planner.py')
            store = memory.ResearchStore(memory_path)
            try:
                planner = planning.Planner(store, program, self.campaign_id, {'actor': 'campaign-runner'})
                tasks = planner.listing()
                gate = load('campaign_completion', '.claude/skills/campaign/scripts/completion_gate.py')
                review_module = load('campaign_reviews', '.claude/skills/impact-verifier/scripts/review_store.py')
                authority = review_module.registered_context(CAMPAIGN_ROOT.parent / 'agent-interface.sqlite3', program, self.campaign_id, f'uid:{os.getuid()}')
                snapshot = self.dir / 'context.snapshot.json'
                if authority and snapshot.is_file() and authority['scope_digest'] != json.loads(snapshot.read_text())['identity']['scope_digest']:
                    authority = None
                assessment = gate.evaluate(store, program, self.campaign_id, tasks, review_context=authority)
            finally:
                store.db.close()
        obligations += assessment['obligations']
        status = 'operator_stopped' if stop_reason else 'budget_exhausted' if self.over_budget() else 'previewed' if self.dry else 'blocked' if obligations else 'completed'
        result = {'status': status, 'can_complete': status == 'completed', 'obligations': obligations,
                  'stop_reason': stop_reason, 'grants_execution': False, 'submission_approved': False}
        atomic_json(self.dir / 'completion.json', result)
        return result

    def tier_ok(self, tier):
        return TIERS.get(tier, 1) <= TIERS.get(self.max_tier, 1)

    # ---- phases ----
    def phase_backbone(self):
        self.log("=== PHASE: recon + domain model + plan ===")
        self.fsm_transition("backbone", note="recon+model+plan phase start")
        self.write_status("backbone")
        plan_wf = "generate-plan-safe" if self.max_tier == "passive" else "generate-plan"
        for skill, wf, tier in BACKBONE + [("planner", plan_wf, ""), ("planner", "visualize-plan", "")]:
            if not tier or self.tier_ok(tier):
                self.expect_workflow(skill, wf)
        for skill, wf, min_tier in BACKBONE:
            if self.over_budget():
                self.log("Time budget exhausted during backbone."); return
            if min_tier and not self.tier_ok(min_tier):
                self.log(f"SKIP {skill}/{wf} (needs {min_tier}, ceiling {self.max_tier})")
                continue
            self.run_workflow(skill, wf)
        # planner: full plan when active/intrusive, safe plan when passive
        if self.over_budget():
            return
        plan_wf = "generate-plan-safe" if self.max_tier == "passive" else "generate-plan"
        self.run_workflow("planner", plan_wf)
        self.run_workflow("planner", "visualize-plan")
        if not self.over_budget():
            self.recovery.satisfy("phase:backbone")

    def phase_execute(self, outdir):
        self.log("=== PHASE: execute vulnerability skills (priority order) ===")
        self.fsm_transition("execute", note="priority skill execution start")
        order = ranked_skills_from_plan(outdir)
        if order:
            self.log(f"Plan-ranked skills: {', '.join(order)}")
        else:
            order = list(DEFAULT_SKILL_ORDER)
            self.log("No usable plan; using default skill order.")
        # append any applicable skills not in plan so coverage is complete
        for s in DEFAULT_SKILL_ORDER:
            if s not in order:
                order.append(s)
        if self.args.skills:
            wanted = {s.strip() for s in self.args.skills.split(",") if s.strip()}
            order = [s for s in order if s in wanted]
            self.log(f"Restricted by --skills to: {', '.join(order)}")

        for skill in order:
            if skill not in EXECUTE_SKILL_DENYLIST:
                for name, tier in list_workflows(skill):
                    if self.tier_ok(tier):
                        self.expect_workflow(skill, name)
        for skill in order:
            if self.over_budget():
                self.log("Time budget exhausted; stopping execute phase."); break
            if skill in EXECUTE_SKILL_DENYLIST:
                continue
            wfs = list_workflows(skill)
            runnable = [(n, t) for (n, t) in wfs if self.tier_ok(t)]
            skipped = [(n, t) for (n, t) in wfs if not self.tier_ok(t)]
            if not runnable:
                continue
            self.log(f"-- skill {skill}: {len(runnable)} workflow(s) at/under {self.max_tier}"
                     + (f", {len(skipped)} above ceiling skipped" if skipped else ""))
            for name, tier in runnable:
                if self.over_budget():
                    break
                self.run_workflow(skill, name)
            self.write_status("execute", {"current_skill": skill})
        if not self.over_budget():
            self.recovery.satisfy("phase:execute")

    def phase_report(self):
        self.log("=== PHASE: reporting + readiness ===")
        self.fsm_transition("report", note="report phase start")
        self.write_status("report")
        
        # Reporting follows the same leases and budget as every other workflow.
        for skill, wf in [("reporting", "batch-generate"),
                          ("impact-verifier", "collect-candidates"),
                          ("impact-verifier", "report-readiness")]:
            if (Path(".claude/skills") / skill / "skill.yaml").is_file():
                self.run_workflow(skill, wf)
        if not self.over_budget():
            self.recovery.satisfy("phase:report")

    # ---- orchestration ----
    def init_context(self):
        a = self.args
        argv = ["bin/bb-init", a.init_domain]
        if a.program:
            argv += ["--program", a.program]
        if a.scope_file:
            argv += ["--scope-file", a.scope_file]
        if a.scheme:
            argv += ["--scheme", a.scheme]
        if a.port:
            argv += ["--port", str(a.port)]
        self.log(f"init: {' '.join(argv)}")
        if not self.dry:
            r = subprocess.run(argv, capture_output=True, text=True, env={**os.environ, 'BB_CONTEXT_DIR': str((self.dir / 'bootstrap').resolve())})
            if r.returncode != 0:
                sys.exit(f"ERROR: bb-init failed:\n{r.stdout}\n{r.stderr}")
        # bb-validate (non-fatal)
        if not self.dry:
            subprocess.run(['bin/bb-validate'], capture_output=True, text=True, env={**os.environ, 'BB_CONTEXT_FILE': str((self.dir / 'bootstrap/context.env').resolve())})

    def hunt(self):
        a = self.args
        # ---- safety / authorization gate ----
        scope_ok = bool(a.scope_file and Path(a.scope_file).is_file()
                        and Path(a.scope_file).stat().st_size > 0)
        requested = a.max_tier
        if TIERS.get(requested, 1) >= TIERS["intrusive"] and not scope_ok:
            self.max_tier = "active-safe"
            print("=" * 64)
            print("AUTHORIZATION GATE: intrusive testing requires a scope/authorization")
            print("file. None found (--scope-file). Capping this run at 'active-safe'.")
            print("To run intrusive blackbox testing, pass --scope-file <authorization>.")
            print("=" * 64)
        else:
            self.max_tier = requested

        self.setup_dir(a.target, resume_id=a.resume)
        self.log(f"campaign {self.campaign_id} | target={a.target} | ceiling={self.max_tier} "
                 f"| budget={self.budget}s | scope={'yes' if scope_ok else 'no'} "
                 f"| {'RESUME' if a.resume else 'NEW'}{' | DRY-RUN' if self.dry else ''}")

        terminal, outdir = 'failed', ''
        try:
            if not a.no_init and not a.resume:
                self.init_context()

            snapshot = self.dir / 'context.snapshot.json'
            ctx = json.loads(snapshot.read_text())['context'] if a.resume and snapshot.exists() else json.loads((self.dir / 'bootstrap/context.json').read_text()) if (self.dir / 'bootstrap/context.json').exists() else load_context()
            if ctx.get('target_host') and ctx['target_host'] != getattr(a, 'init_domain', a.target):
                raise ValueError('Selected context target does not match campaign target')
            if a.program and ctx.get('program') and a.program != ctx['program']:
                raise ValueError('Selected context program does not match campaign program')
            scope_path = Path(a.scope_file or ctx.get('scope_file') or '')
            scope_digest = hashlib.sha256(scope_path.read_bytes()).hexdigest() if scope_path.is_file() else ''
            if not a.resume:
                ctx = {k: v for k, v in ctx.items() if k.lower() not in ('auth_header', 'cookie_jar')}
                ctx['outdir'] = str((self.dir / 'output').resolve())
                ctx['evidence_dir'] = str((self.dir / 'output/evidence').resolve())
            ctx = self.recovery.bind(ctx, a.target, a.program or ctx.get('program', ''), scope_digest,
                                     hash_value({'max_tier': self.max_tier, 'rate_limit': a.rate_limit}), resume=bool(a.resume))
            outdir = ctx['outdir']
            self.output_root = outdir
            self.program_id = a.program or ctx.get('program', '')
            for phase in ('backbone', 'execute', 'report'):
                self.recovery.expect('phase:' + phase, {})
            self.context_file = self.dir / ('context-' + self.recovery.owner + '.env')
            values = {k.upper(): str(v) for k, v in ctx.items() if isinstance(v, (str, int))}
            values['OUTDIR'] = outdir
            values['EVIDENCE_DIR'] = str(Path(outdir) / 'evidence')
            values['COOKIE_JAR'] = str((self.dir / ('cookies-' + self.recovery.owner + '.txt')).resolve())
            if a.rate_limit:
                values['RATE_LIMIT'] = str(int(a.rate_limit))
            self.context_file.write_text(''.join('export ' + k + '=' + shlex.quote(v) + '\n' for k, v in values.items()
                                               if re.fullmatch('[A-Z_][A-Z_0-9]*', k)))
            self.context_file.chmod(0o600)
            self.phase_backbone()
            if not self.over_budget():
                self.phase_execute(outdir)
            if not self.over_budget():
                self.phase_report()
            completion = self.assess_completion(a.program or ctx.get('program', ''))
            terminal = completion['status']
        except KeyboardInterrupt:
            completion = self.assess_completion(getattr(self, 'program_id', a.program or ''), stop_reason='Operator interrupt')
            terminal = completion['status']
            raise
        finally:
            self.fsm_transition(terminal, note="campaign terminal state")
            final = self.write_status(terminal, {"completion": completion} if "completion" in locals() else None)
            self.log(f"=== CAMPAIGN {terminal.upper()} === ran={final['workflows_run']} "
                     f"ok={final['workflows_ok']} failed={final['workflows_failed']} "
                     f"elapsed={final['elapsed_s']}s")
            self.log(f"State: {self.dir}/  (status.json, campaign.log, results.jsonl)")
            self.log(f"Findings/evidence under: {outdir}")
            if self.log_fh:
                self.log_fh.close()
            self.results_fh.close()
            self.recovery.db.close()
            self.lock_fh.close()



def cmd_status(args):
    cid = args.campaign
    if not cid:
        if not CAMPAIGN_ROOT.is_dir():
            print("No campaigns yet."); return
        dirs = sorted([d for d in CAMPAIGN_ROOT.iterdir() if d.is_dir()])
        if not dirs:
            print("No campaigns yet."); return
        cid = dirs[-1].name
    sp = CAMPAIGN_ROOT / cid / "status.json"
    if not sp.is_file():
        print(f"No status for campaign {cid}"); return
    print(sp.read_text())


def build_parser():
    p = argparse.ArgumentParser(
        prog="campaign_runner.py",
        description="Autonomous bug-bounty campaign runner (recon -> plan -> execute -> report).")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("hunt", help="Run a full autonomous campaign against a target.")
    h.add_argument("--target", required=True, help="Target URL or domain (e.g. example.com).")
    h.add_argument("--program", default="", help="Bug bounty program name.")
    h.add_argument("--scope-file", default="", help="Authorization/scope file (REQUIRED to unlock intrusive).")
    h.add_argument("--max-tier", default="intrusive",
                   choices=["passive", "active-safe", "intrusive"],
                   help="Safety ceiling (default intrusive; auto-capped to active-safe without a scope file).")
    h.add_argument("--time-budget", default="2h", help="Wall-clock budget, e.g. 2h, 90m, 3600 (default 2h).")
    h.add_argument("--skills", default="", help="Comma-separated subset of skills to execute (default: all applicable).")
    h.add_argument("--rate-limit", default="", help="Override RATE_LIMIT in context.")
    h.add_argument("--workflow-timeout", default="600", help="Per-workflow timeout in seconds (default 600).")
    h.add_argument("--scheme", default="https")
    h.add_argument("--port", default="")
    h.add_argument("--resume", default="", help="Resume an existing campaign id (skips init + completed workflows).")
    h.add_argument("--no-init", action="store_true", help="Reuse existing .bb/context (skip bb-init).")
    h.add_argument("--dry-run", action="store_true", help="Print the full plan of action without executing.")

    s = sub.add_parser("status", help="Show latest (or named) campaign status.")
    s.add_argument("--campaign", default="", help="Campaign id (default: most recent).")
    reconcile = sub.add_parser('reconcile', help='Operator: record an interrupted task outcome without retrying it')
    reconcile.add_argument('--campaign', required=True)
    reconcile.add_argument('--task', required=True)
    reconcile.add_argument('--outcome', required=True, choices=['succeeded', 'failed'])
    return p


def main():
    args = build_parser().parse_args()
    if args.cmd == 'reconcile':
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', args.campaign):
            raise ValueError('Invalid campaign ID')
        recovery = Recovery(CAMPAIGN_ROOT / args.campaign)
        try:
            recovery.reconcile(args.task, args.outcome == 'succeeded')
        finally:
            recovery.db.close()
        return
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "hunt":
        # derive bare domain for bb-init from a URL-ish target
        t = args.target.strip()
        m = re.match(r"^(?:(https?)://)?([^/:]+)(?::(\d+))?", t)
        if m:
            if m.group(1):
                args.scheme = m.group(1)
            args.init_domain = m.group(2)
            if m.group(3) and not args.port:
                args.port = m.group(3)
        else:
            args.init_domain = t
        Campaign(args).hunt()


if __name__ == "__main__":
    main()
