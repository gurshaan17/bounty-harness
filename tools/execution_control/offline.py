"""No subprocess, environment interpolation, network, or caller-selected imports."""

import importlib.util

from tools.agent_interface.contracts import InterfaceError


def execute(root, entry, inputs):
    if entry["id"] != "standard-catalog/search" or entry["execution_modes"] != ["offline"]:
        raise InterfaceError("unsupported_capability", "Workflow requires unavailable constrained execution")
    path = root / ".claude/skills/standard-catalog/scripts/search_standards.py"
    spec = importlib.util.spec_from_file_location("bh_standards_search", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.search_catalogs(root / ".claude/skills/standard-catalog/catalogs", inputs.get("QUERY", "injection"))
