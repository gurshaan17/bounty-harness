#!/usr/bin/env python3
"""Render provider entry points, preserving canonical files and manual edits."""

import argparse
import difflib
import hashlib
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from tools.agent_interface.catalog import catalog  # noqa: E402


class Conflict(ValueError):
    pass


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".bb-adapter-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Renderer:
    def __init__(self, root=REPO_ROOT):
        self.root = root.resolve()
        self.directory = self.root / "tools/adapters"
        self.manifest = yaml.safe_load((self.directory / "manifest.yaml").read_text())
        self.state_path = self.safe_path("tools/adapters/state.json")
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {"version": 2, "files": {}}
        if self.state.get("version") != 2:
            raise Conflict("Unknown adapter state version")
        # The old renderer recorded hashes but destroyed originals; never silently adopt it.
        legacy = self.directory / "state.yaml"
        if legacy.exists() and (yaml.safe_load(legacy.read_text()) or {}).get("tracked_files"):
            raise Conflict("Legacy state.yaml needs operator reconciliation; originals will not be overwritten")

    def safe_path(self, relative):
        path = self.root / relative
        allowed = (".agents/skills/", ".codex/", ".opencode/agents/", ".claude/commands/", ".cursor/rules/", "docs/", "tools/adapters/")
        if not relative.startswith(allowed) and relative != ".claude/settings.local.json":
            raise Conflict("Path is outside adapter destinations")
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise Conflict("Adapter path escapes repository")
        if not path.resolve().is_relative_to(self.root):
            raise Conflict("Adapter symlink escapes repository")
        if any(p.is_symlink() for p in [path, *path.parents] if p != self.root.parent):
            raise Conflict("Adapter symlinks are unsupported")
        if relative in ("AGENTS.md", "CLAUDE.md") or relative.startswith((".bb/", "evidence/", "output/", "engagements/")):
            raise Conflict("Canonical instructions and research state cannot be generated")
        return path

    def outputs(self, target):
        if target not in self.manifest["targets"]:
            raise Conflict("Unknown target")
        canonical = (self.root / "AGENTS.md").read_text()
        workflows = catalog(self.root)["workflows"]
        ids = sorted(e["id"] for e in workflows)
        body = (
            "# BountyHarness offline research\n\n"
            "Read root AGENTS.md for shared architecture and .claude/skills/<skill>/SKILL.md "
            "when selecting a workflow. Use bin/bb-agent for provider-neutral research sessions.\n\n"
            "Read docs/agent-interface.md for the JSON protocol and session-switch example. "
            "Start with capabilities and discover; submit only workflows advertising offline mode. "
            "Active execution is unavailable. Do not route denied tasks through legacy bb-run or shell.\n\n"
            "Use session.open with the operator-registered program/campaign. On a provider switch, "
            "set predecessor_session_id; retain task_id and idempotency_key. Read session.handoff "
            "before continuing. A completed task is not a confirmed vulnerability.\n\n"
            f"Shared instruction digest: `{sha(canonical)}`.\n"
            f"Workflow ID digest: `{sha(json.dumps(ids))}` ({len(ids)} workflows).\n"
        )
        outputs = {}
        for file in self.manifest["targets"][target]["files"]:
            path, kind = file["path"], file["kind"]
            self.safe_path(path)
            if kind == "skill":
                header = "---\nname: bounty-harness\ndescription: Discover BountyHarness workflows, dispatch offline research, and resume shared sessions.\n---\n"
                text = header + self.mark(body)
            elif kind in ("research", "reviewer"):
                permissions = [{"action": "*", "resource": "*", "effect": "deny"}, {"action": "read", "resource": "*", "effect": "allow"}]
                if kind == "research":
                    permissions.append({"action": "shell", "resource": "bin/bb-agent", "effect": "allow"})
                header = {"description": "BountyHarness offline " + kind, "mode": "primary", "permissions": permissions}
                text = (
                    "---\n"
                    + yaml.safe_dump(header, sort_keys=False)
                    + "---\n"
                    + self.mark(
                        body
                        + (
                            "\nReview supplied evidence read-only. Record limitations and alternative explanations; independent review storage is unavailable until Phase 5.\n"
                            if kind == "reviewer"
                            else ""
                        )
                    )
                )
            elif kind == "codex-config":
                text = '# BountyHarness generated offline defaults\nsandbox_mode = "read-only"\napproval_policy = "never"\nweb_search = "disabled"\n'
            elif kind == "claude-config":
                text = json.dumps({"permissions": {"allow": ["Read", "Bash(bin/bb-agent)"], "deny": ["Edit", "Write", "WebFetch", "WebSearch"], "defaultMode": "dontAsk"}}, indent=2) + "\n"
            else:
                text = self.mark(body)
            outputs[path] = text
        return outputs

    @staticmethod
    def mark(body):
        return "<!-- BB-ADAPTER-GENERATED [interface-v1] -->\n" + body + "<!-- /BB-ADAPTER-GENERATED [interface-v1] -->\n"

    def plan(self, target, uninstall=False):
        generated = {} if uninstall else self.outputs(target)
        paths = {p for p, info in self.state["files"].items() if info["target"] == target} | set(generated)
        plan = []
        for relative in sorted(paths):
            path = self.safe_path(relative)
            old = path.read_text() if path.exists() else ""
            info = self.state["files"].get(relative)
            new = generated.get(relative, "")
            if info:
                if info["target"] != target or sha(info["content"]) != info["hash"]:
                    raise Conflict("Invalid ownership record: " + relative)
                owned = info["content"]
                if not owned or old.count(owned) != 1:
                    raise Conflict("Generated content was edited or removed: " + relative)
                if path.suffix in (".json", ".toml") and old != owned:
                    raise Conflict("Generated configuration was edited: " + relative)
                merged = old.replace(owned, new, 1)
            else:
                if old:
                    raise Conflict("Unmanaged file already exists: " + relative)
                merged = new
            plan.append((relative, old, merged, new))
        return plan

    def run(self, target, apply=False, uninstall=False):
        # Preflight every file before any mutation; preview and apply use the same merge.
        plan = self.plan(target, uninstall)
        for relative, old, merged, new in plan:
            if old == merged:
                continue
            print("".join(difflib.unified_diff(old.splitlines(True), merged.splitlines(True), fromfile=relative, tofile=relative)), end="")
            if not apply:
                continue
            path = self.safe_path(relative)
            if old:
                backup = self.safe_path("tools/adapters/backups/" + uuid.uuid4().hex + ".bak")
                atomic(backup, old)
            if merged:
                atomic(path, merged)
            elif path.exists():
                path.unlink()
            if new:
                self.state["files"][relative] = {"target": target, "hash": sha(new), "content": new}
            else:
                self.state["files"].pop(relative, None)
            # Each atomic replacement is recoverable via its unique backup. A crash before
            # state replacement yields a conflict, never an unsafe overwrite on the next run.
            atomic(self.state_path, json.dumps(self.state, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["preview", "apply", "uninstall", "list-targets", "validate"])
    parser.add_argument("--target")
    parser.add_argument("--force", action="store_true", help="Compatibility: skip uninstall prompt; never override conflicts")
    args = parser.parse_args()
    try:
        renderer = Renderer()
        if args.command == "list-targets":
            print("\n".join(renderer.manifest["targets"]))
        elif args.command == "validate":
            for target in renderer.manifest["targets"]:
                renderer.outputs(target)
            print("Manifest and canonical catalog are valid")
        else:
            targets = [args.target] if args.target else list(renderer.manifest["targets"]) if args.command == "uninstall" else []
            if not targets:
                raise Conflict("--target is required")
            for target in targets:
                renderer.run(target, apply=args.command != "preview", uninstall=args.command == "uninstall")
    except (Conflict, OSError, ValueError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
