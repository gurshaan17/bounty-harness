"""Bounded local evidence import; artifacts are copied as inert immutable bytes."""

import hashlib
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

PROFILES = {
    "http_response": {"request", "response"},
    "access_boundary": {"request", "response"},
    "browser_execution": {"request", "response", "screenshot"},
    "static_analysis": {"source_excerpt", "tool_output"},
    "oob_callback": {"request", "callback_record"},
}
KINDS = {"request", "response", "screenshot", "tool_output", "source_excerpt", "callback_record", "policy_excerpt"}
MAX_FILE = 1024 * 1024
MAX_TOTAL = 8 * MAX_FILE


def exact(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError("Unknown or missing fields")


def stamp(value):
    if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
        raise ValueError("Capture timestamp must include a timezone")


def scan_bytes(data, scan):
    # Preserve overlap so credentials straddling chunk boundaries are still detected.
    content = data.decode("latin1")
    for offset in range(0, len(content), 4096):
        scan(content[max(0, offset - 512) : offset + 4096])


def read_artifact(root, item, scan):
    exact(item, ("id", "path", "kind", "sha256", "size", "captured_at", "source_run_id", "tool_versions", "redaction"))
    if not isinstance(item["id"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", item["id"]) or item["kind"] not in KINDS:
        raise ValueError("Invalid evidence identifier/type")
    if type(item["size"]) is not int or not 0 < item["size"] <= MAX_FILE or not isinstance(item["sha256"], str) or not re.fullmatch("[a-f0-9]{64}", item["sha256"]):
        raise ValueError("Invalid evidence size/hash")
    stamp(item["captured_at"])
    if not isinstance(item["tool_versions"], dict) or not item["tool_versions"] or any(not isinstance(k, str) or not isinstance(v, str) or not v for k, v in item["tool_versions"].items()):
        raise ValueError("Tool versions required")
    exact(item["redaction"], ("status", "notes"))
    if item["redaction"]["status"] not in ("redacted", "no_secrets_detected") or not isinstance(item["redaction"]["notes"], str) or not item["redaction"]["notes"].strip():
        raise ValueError("Explicit redaction metadata required")
    relative = Path(item["path"])
    if relative.is_absolute() or not relative.parts or any(part in (".", "..") for part in relative.parts):
        raise ValueError("Evidence paths must be contained relative files")
    # openat plus O_NOFOLLOW on every component prevents symlink substitution and traversal.
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o111 or info.st_size != item["size"] or info.st_nlink != 1:
                raise ValueError("Evidence must be a non-executable regular file of declared size")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read(MAX_FILE + 1)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)
    if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise ValueError("Evidence integrity mismatch")
    if data.startswith((b"#!", b"MZ", b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf")):
        raise ValueError("Executable evidence is unsupported; provide inert excerpts")
    if item["kind"] == "screenshot":
        if not data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
            raise ValueError("Screenshots must be PNG/JPEG bytes")
        media_type = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
    else:
        content = data.decode("utf-8")
        if item["kind"] in ("request", "response"):
            if "\n\n" not in content.replace("\r\n", "\n"):
                raise ValueError("HTTP evidence requires headers/body boundary")
            pattern = r"[A-Z]+ \S+ HTTP/\d" if item["kind"] == "request" else r"HTTP/\d(?:\.\d)? \d{3}"
            if not re.match(pattern, content):
                raise ValueError("Invalid raw HTTP evidence")
        media_type = "text/plain; charset=utf-8"
    scan_bytes(data, scan)
    return {k: v for k, v in item.items() if k != "path"} | {"media_type": media_type, "origin": "operator_import"}, data


def import_files(root, manifest, profile, scan, verify_run):
    exact(manifest, ("schema_version", "scope_digest", "policy_digest", "source_run_ids", "reproduction", "artifacts", "associations"))
    scan(manifest)
    if manifest["schema_version"] != "1.0":
        raise ValueError("Unsupported evidence manifest version")
    if not isinstance(manifest["reproduction"], str) or not manifest["reproduction"].strip():
        raise ValueError("Reproduction instructions required; never automatically executed")
    if not isinstance(manifest["source_run_ids"], list) or not 1 <= len(manifest["source_run_ids"]) <= 32:
        raise ValueError("Bound source runs required")
    for run in manifest["source_run_ids"]:
        verify_run(run)
    if not isinstance(manifest["artifacts"], list) or not 1 <= len(manifest["artifacts"]) <= 16:
        raise ValueError("Between one and sixteen artifacts required")
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Evidence root must be a real directory")
    artifacts, blobs = [], {}
    total = 0
    for item in manifest["artifacts"]:
        descriptor, data = read_artifact(root, item, scan)
        if descriptor["id"] in blobs or descriptor["source_run_id"] not in manifest["source_run_ids"]:
            raise ValueError("Duplicate artifact or unbound source run")
        total += len(data)
        if total > MAX_TOTAL:
            raise ValueError("Evidence bundle exceeds byte budget")
        artifacts.append(descriptor)
        blobs[descriptor["id"]] = data
    if not isinstance(manifest["associations"], list) or len(manifest["associations"]) > 16:
        raise ValueError("Invalid request/response associations")
    kinds = {a["id"]: a["kind"] for a in artifacts}
    pairs = set()
    for pair in manifest["associations"]:
        exact(pair, ("request_id", "response_id"))
        if kinds.get(pair["request_id"]) != "request" or kinds.get(pair["response_id"]) != "response":
            raise ValueError("Association must bind a request and response in this bundle")
        pairs.add((pair["request_id"], pair["response_id"]))
    for artifact in artifacts:
        if artifact["kind"] in ("request", "response") and not any(artifact["id"] in pair for pair in pairs):
            raise ValueError("Every HTTP artifact requires an explicit association")
    required = PROFILES[profile]
    presence = {kind: {"required": kind in required, "present": kind in kinds.values()} for kind in sorted(KINDS)}
    missing = sorted(kind for kind in required if not presence[kind]["present"])
    if profile in ("http_response", "browser_execution") and not pairs:
        missing.append("request_response_pair")
    if profile == "access_boundary" and (len({x for x, _ in pairs}) < 2 or len({y for _, y in pairs}) < 2):
        missing.append("comparison_pair")
    return {
        **manifest,
        "artifacts": artifacts,
        "presence": presence,
        "missing": missing,
        "captured_at": sorted(a["captured_at"] for a in artifacts),
        "imported_at": datetime.now(UTC).isoformat(),
    }, blobs


if __name__ == "__main__":
    import argparse

    argparse.ArgumentParser(description=__doc__, epilog="Used by the operator evidence-import command; never executes evidence.").parse_args()
