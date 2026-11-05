"""Read canonical YAML, without executing commands or probing tools."""

import hashlib
import shutil
from pathlib import Path

import yaml

from .contracts import digest

ROOT = Path(__file__).resolve().parents[2]
PROVIDERS = ["codex", "opencode", "claude-code", "generic", "cursor"]


def package_digest(directory):
    h = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts:
            h.update(str(path.relative_to(directory)).encode())
            h.update(b"\0")
            h.update(path.read_bytes())
    return h.hexdigest()


def catalog(root=ROOT):
    # No cache: package/registry changes are visible on the very next call.
    entries = []
    for path in sorted((root / ".claude/skills").glob("*/skill.yaml")):
        data = yaml.safe_load(path.read_text())
        package = package_digest(path.parent)
        for name, workflow in data.get("workflows", {}).items():
            inputs = workflow.get("input_schema")
            if inputs is None:
                inputs = {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {key: {"type": "string", "maxLength": 4096} for key in workflow.get("inputs", [])},
                    "required": workflow.get("inputs", []),
                }
            tier = workflow.get("safety_tier", data.get("safety_tier", "unknown"))
            auth = workflow.get("auth_required", data.get("auth_required"))
            effects = workflow.get("side_effects", data.get("side_effects"))
            complete = tier != "unknown" and type(auth) is bool and isinstance(effects, list)
            offline = path.parent.name == "standard-catalog" and name == "search" and workflow.get("offline_handler") == "standards-search-v1" and complete and effects == [] and auth is False
            entries.append(
                {
                    "id": f"{path.parent.name}/{name}",
                    "skill": path.parent.name,
                    "workflow": name,
                    "package_version": str(data.get("version", "unknown")),
                    "description": workflow.get("description", data.get("description", "")),
                    "command_digest": hashlib.sha256(workflow["command"].encode()).hexdigest(),
                    "workflow_digest": digest({"package": package, "workflow": workflow}),
                    "inputs": inputs,
                    "outputs": [{"kind": "file", "path": out} if isinstance(out, str) else out for out in workflow.get("outputs", [])],
                    "safety_tier": tier,
                    "auth_required": auth,
                    "side_effects": effects,
                    "security_metadata_complete": complete,
                    "tools": [{"name": tool, "installed": shutil.which(tool) is not None} for tool in workflow.get("tools_required", data.get("tools_required", []))],
                    "execution_modes": ["offline"] if offline else [],
                    "unavailable_reason": None if offline else "No constrained execution backend",
                }
            )
    return {"schema_version": "1.0", "catalog_digest": digest(entries), "registry_digest": package_digest(root / "tools/registry"), "workflows": entries}


def capabilities():
    return {
        "schema_version": "1.0",
        "protocol_versions": ["1.0"],
        "providers": PROVIDERS,
        "transports": ["json-cli"],
        "operations": [
            "capabilities",
            "discover",
            "session.open",
            "session.resume",
            "session.handoff",
            "research.query",
            "research.append",
            "research.correct",
            "research.history",
            "research.summary",
            "research.context",
            "research.contradictions",
            "research.export",
            "artifact.read",
            "task.submit",
            "task.status",
            "task.cancel",
        ],
        "unavailable_operations": ["queue.list", "review.record", "active_dispatch", "mcp"],
        "isolation_available": False,
        "execution_modes": ["offline"],
        "limitations": [
            "Local OS user is the principal; provider labels are provenance only",
            "Only the fixed standards search handler executes; no shell workers",
            "Legacy bb-run is not a production security boundary",
            "Native client permission enforcement is not established",
        ],
    }
