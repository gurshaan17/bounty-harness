"""Strict scope parsing and per-request authorization checks.

Scope files use documented sections. ``# In Scope`` / ``# Out of Scope``
comment headings and plain ``out of scope`` / ``excluded`` marker lines
switch sections; exclusions always take precedence over inclusions.

Rule syntax (no inferred wildcards or target-name authorization):

- ``example.com``        exact host only; subdomains are NOT included
- ``*.example.com``      explicit glob; matches proper subdomains only
- ``https://app.example.com:8443/api``  scheme + host + port + path prefix
- ``192.0.2.10``, ``198.51.100.0/24``   IP / CIDR rules for resolved addresses
- ``api``, ``ip``, ``wildcards`` section keywords keep entries in scope
- ``mobile`` / ``notes`` sections are recorded but never authorize URLs

Only ``http``/``https`` URLs without userinfo or fragments are eligible.
Every resolved address must be global unless an explicit IP rule covers it.
"""

import ipaddress
import json
import re
import socket
from urllib.parse import urlsplit

SECTIONS = {
    "in scope": "allow",
    "in-scope": "allow",
    "included": "allow",
    "include": "allow",
    "wildcards": "allow",
    "api": "allow",
    "apis": "allow",
    "ip": "allow",
    "ips": "allow",
    "out of scope": "deny",
    "out-of-scope": "deny",
    "excluded": "deny",
    "exclude": "deny",
    "oos": "deny",
    "mobile": "other",
    "android": "other",
    "ios": "other",
    "notes": "other",
    "note": "other",
    "comments": "other",
}

_HOST_CHARS = re.compile(r"^[a-z0-9.*_-]+$")
MAX_URL = 2048


def _section_of(line):
    lower = line.strip().lower()
    for keyword in sorted(SECTIONS, key=len, reverse=True):
        if lower == keyword or (lower.startswith(keyword) and lower[len(keyword)] in " :\t-"):
            return SECTIONS[keyword]
    return None


def _host_rule(value):
    host = value.strip().lower().rstrip(".")
    if not host or len(host) > 253 or not _HOST_CHARS.fullmatch(host):
        raise ValueError(f"Invalid scope host: {value!r}")
    if ".." in host or host.startswith(".") or host.endswith(".") or host == "*":
        raise ValueError(f"Invalid scope host: {value!r}")
    return {"kind": "host", "host": host, "glob": "*" in host}


def _url_rule(value):
    if len(value) > MAX_URL or any(c.isspace() for c in value):
        raise ValueError("Invalid scope URL")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("Invalid scope URL") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("Scope URLs require http/https host without credentials")
    if parts.query or parts.fragment:
        raise ValueError("Scope URLs cannot contain query or fragment")
    host = _host_rule(parts.hostname)
    path = parts.path or None
    if path is not None and not path.startswith("/"):
        raise ValueError("Invalid scope path")
    return {"kind": "url", "scheme": scheme, "host": host["host"], "glob": host["glob"], "port": port, "path": path}


def _ip_rule(value):
    candidate = value.strip().strip("[]")
    try:
        network = ipaddress.ip_network(candidate, strict=False)
    except ValueError:
        raise ValueError(f"Invalid scope entry: {value!r}") from None
    return {"kind": "ip", "network": str(network)}


def _entry_rule(value):
    value = value.strip()
    if "://" in value:
        return _url_rule(value)
    try:
        ipaddress.ip_network(value.strip("[]"), strict=False)
        return _ip_rule(value)
    except ValueError:
        pass
    if "/" in value:
        raise ValueError(f"Invalid scope entry: {value!r}")
    return _host_rule(value)


def parse(text):
    """Parse scope text into {'allow': [...], 'deny': [...], 'other': [...]}."""
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > 65536:
        raise ValueError("Scope snapshot must be nonempty and at most 64 KiB")
    rules = {"allow": [], "deny": [], "other": []}
    section = "allow"
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            heading = _section_of(line[1:])
            if heading:
                section = heading
            continue
        switched = _section_of(line)
        if switched:
            section = switched
            continue
        if section == "other":
            rules["other"].append({"kind": "other", "value": line[:256]})
        else:
            rules[section].append(_entry_rule(line))
    if not rules["allow"]:
        raise ValueError("Scope snapshot contains no in-scope rules")
    return rules


def _host_matches(rule, host):
    if rule.get("glob"):
        pattern = re.escape(rule["host"]).replace(r"\*", ".*")
        return re.fullmatch(pattern, host) is not None
    return rule["host"] == host


def _path_matches(rule_path, path):
    if rule_path is None:
        return True
    if path == rule_path:
        return True
    if rule_path.endswith("/"):
        return path.startswith(rule_path)
    return path.startswith(rule_path) and len(path) > len(rule_path) and path[len(rule_path)] == "/"


def _rule_matches(rule, target):
    kind = rule["kind"]
    if kind == "other":
        return False
    if kind == "ip":
        network = ipaddress.ip_network(rule["network"])
        return any(ipaddress.ip_address(a) in network for a in target["addresses"])
    if kind == "url" and rule["scheme"] != target["scheme"]:
        return False
    if rule.get("port") is not None and rule["port"] != target["port"]:
        return False
    if not _host_matches(rule, target["host"]):
        return False
    return _path_matches(rule.get("path"), target["path"])


def resolve_host(host):
    """Default resolver: literal IPs pass through; names use getaddrinfo."""
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def check_url(rules, url, resolver=None):
    """Authorize one URL. Returns a decision dict; never raises for policy."""
    resolver = resolver or resolve_host
    result = {"allowed": False, "reason": "", "rule": None, "addresses": []}
    if not isinstance(url, str) or not url or len(url) > MAX_URL:
        result["reason"] = "invalid URL"
        return result
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        result["reason"] = "invalid URL"
        return result
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        result["reason"] = "unsupported scheme"
        return result
    if parts.username or parts.password or "@" in parts.netloc:
        result["reason"] = "userinfo is not permitted"
        return result
    host = parts.hostname
    if not host:
        result["reason"] = "missing host"
        return result
    host = host.rstrip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        result["reason"] = "invalid host"
        return result
    if parts.fragment:
        result["reason"] = "fragments are not permitted"
        return result
    port = port if port is not None else (443 if scheme == "https" else 80)
    path = parts.path or "/"
    target = {"scheme": scheme, "host": host, "port": port, "path": path, "addresses": []}
    # Name-based exclusions win without requiring DNS.
    for rule in rules.get("deny", []):
        if rule["kind"] != "ip" and _rule_matches(rule, target):
            result.update(reason="explicitly out of scope", rule=rule)
            return result
    try:
        addresses = resolver(host)
    except Exception:
        result.update(reason="host does not resolve")
        return result
    if not addresses:
        result.update(reason="host does not resolve")
        return result
    normalized = []
    for address in addresses:
        try:
            normalized.append(str(ipaddress.ip_address(address.split("%")[0])))
        except ValueError:
            result.update(reason="resolver returned an invalid address")
            return result
    target["addresses"] = normalized
    result["addresses"] = normalized
    for rule in rules.get("deny", []):
        if rule["kind"] == "ip" and _rule_matches(rule, target):
            result.update(reason="explicitly out of scope", rule=rule)
            return result
    matched = next((rule for rule in rules.get("allow", []) if _rule_matches(rule, target)), None)
    if matched is None:
        result["reason"] = "no in-scope rule matches"
        return result
    for address in normalized:
        ip = ipaddress.ip_address(address)
        if ip.is_global:
            continue
        covered = any(
            rule["kind"] == "ip" and ip in ipaddress.ip_network(rule["network"]) for rule in rules.get("allow", [])
        )
        if not covered:
            result.update(reason="resolves to a non-public address outside explicit IP scope", rule=matched)
            return result
    result.update(allowed=True, reason="in scope", rule=matched)
    return result


def safe_target(url):
    """Audit-safe URL form: scheme, host and path only; query is dropped."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
        suffix = f":{port}" if port and port != (443 if parts.scheme == "https" else 80) else ""
        return f"{parts.scheme}://{host}{suffix}{parts.path or '/'}"
    except (ValueError, AttributeError):
        return "[invalid-url]"


def canonical(value):
    return json.dumps(value, sort_keys=True)
