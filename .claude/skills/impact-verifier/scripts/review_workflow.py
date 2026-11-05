#!/usr/bin/env python3
"""Record a structured independent review through the fixed provider-neutral API."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    from tools.agent_interface.contracts import decode, validate_named

    with args.request.open("rb") as stream:
        raw = stream.read(65537)
    request = decode(raw)
    validate_named(request, "request")
    if request["operation"] != "review.record":
        parser.error("Only review.record is allowed")
    result = subprocess.run([str(ROOT / "bin/bb-agent")], input=raw, capture_output=True, cwd=ROOT)
    if result.returncode:
        raise SystemExit("Review rejected; inspect the authenticated session, evidence and assessment")
    response = json.loads(result.stdout)
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(response, stream, indent=2)
        stream.write("\n")
    print("Independent review recorded; human confirmation and submission remain separate")


if __name__ == "__main__":
    main()
