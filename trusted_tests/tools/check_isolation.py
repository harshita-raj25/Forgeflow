"""Trusted isolation probe: runs *inside* the worker container and asserts the sandbox properties the
policy claims (PROJECT-SPEC §9) actually hold at runtime, rather than trusting the docker-run flags alone.
Prints one JSON line and exits 0 only if every property holds.

Usage: python check_isolation.py
"""

from __future__ import annotations

import json
import os
import socket
import sys


def check_no_network() -> tuple[bool, str]:
    """--network none must make outbound connections fail fast, not just be slow."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    try:
        s.connect(("8.8.8.8", 53))
    except OSError as e:
        return True, f"outbound connect refused as expected: {type(e).__name__}: {e}"
    else:
        return False, "outbound connect to 8.8.8.8:53 SUCCEEDED -- network isolation is not effective"
    finally:
        s.close()


def check_unprivileged_uid() -> tuple[bool, str]:
    uid = os.getuid()
    return uid != 0 and uid == 65534, f"uid={uid} (expected 65534, non-root)"


def check_no_docker_socket() -> tuple[bool, str]:
    present = os.path.exists("/var/run/docker.sock")
    return not present, f"/var/run/docker.sock present={present}"


def check_no_provider_credentials() -> tuple[bool, str]:
    leaked = [k for k in os.environ if any(tag in k.upper() for tag in ("OPENAI", "API_KEY", "TOKEN", "SECRET"))]
    return not leaked, f"credential-shaped env vars present: {leaked}" if leaked else "no credential-shaped env vars present"


def check_readonly_root() -> tuple[bool, str]:
    """--read-only should make a write outside /tmp fail."""
    try:
        with open("/work/candidate/.isolation-probe-write-test", "w") as f:
            f.write("x")
        os.remove("/work/candidate/.isolation-probe-write-test")
    except OSError as e:
        return True, f"write to read-only candidate mount refused as expected: {type(e).__name__}: {e}"
    else:
        return False, "write to /work/candidate SUCCEEDED -- read-only mount is not effective"


def main() -> int:
    checks = {
        "no_network": check_no_network(),
        "unprivileged_uid": check_unprivileged_uid(),
        "no_docker_socket": check_no_docker_socket(),
        "no_provider_credentials": check_no_provider_credentials(),
        "readonly_candidate_mount": check_readonly_root(),
    }
    result = {name: {"ok": ok, "detail": detail} for name, (ok, detail) in checks.items()}
    all_ok = all(ok for ok, _ in checks.values())
    print(json.dumps({"ok": all_ok, "checks": result}))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
