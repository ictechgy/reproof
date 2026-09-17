#!/usr/bin/env python3
"""Run registered release checks. Declared effects select checks, never sandbox them."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "tests/release_inventory.json"
MAX_OUTPUT = 200000
MAX_INVENTORY = 1024 * 1024
EFFECTS = {"filesystem", "process", "loopback", "native-compile", "browser", "owned-device", "owned-vm"}
FIELDS = {"id", "command", "environment", "effects", "timeoutSeconds", "evidence", "gate"}


class InvalidInventory(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise InvalidInventory(message)


def valid_command(command):
    return (type(command) is list and 4 <= len(command) <= 131
            and command[:3] == ["python3", "-m", "unittest"]
            and all(type(item) is str and len(item) <= 512
                    and re.fullmatch(r"tests(?:\.[A-Za-z_][A-Za-z0-9_]*)+", item)
                    for item in command[3:]))


def load_inventory(path):
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_INVENTORY + 1)
        require(len(raw) <= MAX_INVENTORY, "Release inventory size limit exceeded")
        value = json.loads(raw)
    except (OSError, ValueError, UnicodeError):
        raise InvalidInventory("Malformed release inventory") from None
    require(type(value) is dict and set(value) == {"version", "goals"}
            and type(value["version"]) is int and value["version"] == 1,
            "Malformed release inventory")
    goals = value["goals"]
    require(type(goals) is dict and 1 <= len(goals) <= 128, "No release goals registered")
    # Validate the entire registry before executing the first command.
    for name, goal in goals.items():
        require(type(name) is str and re.fullmatch(r"[A-Z][A-Z0-9_-]{0,63}", name),
                "Invalid release goal identity")
        require(type(goal) is dict and type(goal.get("available")) is bool,
                "Malformed release goal")
        if not goal["available"]:
            require(set(goal) == {"available", "reason"} and type(goal["reason"]) is str
                    and 0 < len(goal["reason"]) <= 240, "Malformed unavailable goal")
            continue
        require(set(goal) == {"available", "checks"} and type(goal["checks"]) is list
                and 1 <= len(goal["checks"]) <= 128, "No required checks registered")
        seen = set()
        for check in goal["checks"]:
            require(type(check) is dict and set(check) == FIELDS, "Malformed release check")
            identifier = check["id"]
            require(type(identifier) is str and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", identifier)
                    and identifier not in seen, "Invalid release check identity")
            seen.add(identifier)
            required = check["effects"]
            require(type(required) is list and 1 <= len(required) <= len(EFFECTS)
                    and all(type(item) is str and item in EFFECTS for item in required)
                    and len(required) == len(set(required)), "Malformed release effects")
            require(valid_command(check["command"]), "Unregistered executable command")
            require(type(check["environment"]) is str and 0 < len(check["environment"]) <= 128,
                    "Missing release environment")
            require(check["evidence"] == "unittest-summary", "Unsupported release evidence")
            require(check["gate"] in ("software", "environment"), "Invalid release gate")
            require(type(check["timeoutSeconds"]) is int and 1 <= check["timeoutSeconds"] <= 600,
                    "Invalid release timeout")
    return goals


def stop_group(process):
    # These are only process groups created by this runner with start_new_session.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_check(check):
    output = bytearray()
    reason = None
    started = time.monotonic()
    command = [sys.executable] + check["command"][1:]
    environment = os.environ.copy()
    tests_path = str(ROOT / "tests")
    inherited_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (tests_path + os.pathsep + inherited_path
                                  if inherited_path else tests_path)
    try:
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True, env=environment)
    except OSError:
        return {"id": check["id"], "gate": check["gate"], "status": "fail",
                "reason": "start_failed", "returncode": None, "testsRun": 0}
    try:
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map() or process.poll() is None:
                if time.monotonic() - started >= check["timeoutSeconds"]:
                    reason = "timeout"
                    break
                for key, _ in selector.select(timeout=0.05):
                    chunk = os.read(key.fd, min(65536, MAX_OUTPUT + 1 - len(output)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(chunk)
                        if len(output) > MAX_OUTPUT:
                            reason = "output_limit"
                            break
                if reason:
                    break
    finally:
        stop_group(process)
        process.stdout.close()
    text = bytes(output[:MAX_OUTPUT]).decode("utf-8", "replace")
    matches = re.findall(r"^Ran ([0-9]+) tests?(?: in [^\n]+)?$", text, flags=re.MULTILINE)
    count = int(matches[-1]) if matches else 0
    passed = process.returncode == 0 and count > 0 and reason is None
    item = {"id": check["id"], "gate": check["gate"], "environment": check["environment"],
            "command": check["command"], "requiredEffects": check["effects"],
            "evidence": check["evidence"], "returncode": process.returncode, "testsRun": count,
            "elapsedSeconds": round(time.monotonic() - started, 3), "summary": text[-4000:],
            "status": "pass" if passed else "fail"}
    if not passed:
        item["reason"] = reason or ("test_failure" if process.returncode else "missing_test_evidence")
    return item


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--goal", required=True)
    parser.add_argument("--effects", required=True)
    parser.add_argument("--inventory", type=Path, default=DEFAULT)
    args = parser.parse_args(argv)
    name = args.goal.upper()
    try:
        goals = load_inventory(args.inventory)
        require(name in goals, "Unknown release goal")
        goal = goals[name]
        require(goal["available"], "Goal checks unavailable")
        declared = set(args.effects.split(","))
        require(declared and declared <= EFFECTS, "Invalid declared effects")
        require(all(set(check["effects"]) <= declared for check in goal["checks"]),
                "Missing declared effects")
    except InvalidInventory as error:
        print(json.dumps({"status": "blocked", "goal": name, "reason": str(error)}, sort_keys=True))
        return 2
    checks = [run_check(check) for check in goal["checks"]]
    status = "pass" if all(check["status"] == "pass" for check in checks) else "fail"
    print(json.dumps({"goal": name, "effects": sorted(declared), "checks": checks,
                      "status": status}, sort_keys=True))
    return 0 if status == "pass" else 2


if __name__ == "__main__":
    sys.exit(main())
