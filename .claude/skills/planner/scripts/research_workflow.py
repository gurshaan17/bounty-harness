#!/usr/bin/env python3
"""Run the fixed offline research-planning operation and save JSON plus Markdown."""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def write_private(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path, help="JSON plan.generate request including an existing session_id")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--markdown", required=True, type=Path)
    args = parser.parse_args()
    # Decode through the same bounded contract as the transport.
    import sys

    sys.path.insert(0, str(ROOT))
    from tools.agent_interface.contracts import decode, validate_named

    with args.request.open("rb") as stream:
        raw = stream.read(65537)
    request = decode(raw)
    validate_named(request, "request")
    if request["operation"] != "plan.generate":
        parser.error("Only plan.generate is allowed")
    process = subprocess.run([str(ROOT / "bin/bb-agent")], input=raw, capture_output=True, cwd=ROOT)
    if process.returncode:
        raise SystemExit("Planning rejected; inspect session and request with bin/bb-agent")
    plan = json.loads(process.stdout)
    write_private(args.output, json.dumps(plan, indent=2) + "\n")
    write_private(args.markdown, plan["markdown"] + "\n")
    print("Offline research plan and review document saved")


if __name__ == "__main__":
    main()
