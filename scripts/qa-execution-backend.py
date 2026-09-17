#!/usr/bin/env python3
"""Run fixed containment probes in an explicitly supplied owned macOS VM."""
import argparse
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reproloop.contracts.versions import bounded_int, exact, require, safe_relative_path, validate_digest, validate_id
from reproloop.core import ContractError
from reproloop.execution.artifacts import ArtifactError, read_regular
from reproloop.execution.backend import ExecutionDenied, QualificationAuthority
from reproloop.execution.journal import RunDenied, RunStore, TERMINAL
from reproloop.execution.qualification import qualify_backend
from reproloop.execution.resources import GuestBundle, ResourceError
from reproloop.execution.wire import ProtocolError, canonical, decode_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", type=Path)
    parser.add_argument("--recover-operation")
    parser.add_argument("--request-digest")
    parser.add_argument("--output-new", type=Path, nargs="?",
                        const=ROOT / "artifacts" / ("execution-qa-" + uuid.uuid4().hex))
    args = parser.parse_args()
    if bool(args.recover_operation) != bool(args.request_digest):
        parser.error("recovery requires both operation identity and request digest")
    report = {"schemaVersion": 1, "status": "blocked-unqualified", "reason": "environment-not-supplied",
              "actualVM": False, "qualified": False, "authority": "none", "probes": []}
    output = None
    try:
        if args.output_new is not None:
            selected = args.output_new.absolute()
            safe_relative_path(selected.as_posix().lstrip("/"))
            selected.mkdir(mode=0o700, parents=False, exist_ok=False)
            output = selected
        if args.environment is not None:
            path = args.environment.absolute()
            value = decode_json(read_regular(Path(path.anchor), path.as_posix().lstrip("/"), maximum=256 * 1024))
            exact(value, ("schemaVersion", "backendId", "bundlePath", "statePath", "diskBudgetBytes"))
            require(type(value["schemaVersion"]) is int and value["schemaVersion"] == 1, "Invalid QA version")
            validate_id(value["backendId"])
            for key in ("bundlePath", "statePath"):
                require(type(value[key]) is str and value[key].startswith("/"), "Explicit absolute path required")
                safe_relative_path(value[key].lstrip("/"))
            bounded_int(value["diskBudgetBytes"], "VM disk budget", 1, 512 * 1024 ** 3)
            bundle = GuestBundle.load(value["bundlePath"])
            store = RunStore(value["statePath"], environment_digest=bundle.environment_digest,
                             disk_limit=value["diskBudgetBytes"])
            if args.recover_operation:
                validate_id(args.recover_operation)
                validate_digest(args.request_digest)
                with store.machine_lease(bundle.machine_digest):
                    was_terminal = store.status(args.recover_operation)["state"] in TERMINAL
                    record = store.reconcile(args.recover_operation, args.request_digest)
                report = {"schemaVersion": 1, "status": "recovered" if record["state"] in TERMINAL else "blocked-quarantined",
                          "operationId": args.recover_operation, "state": record["state"], "qualified": False,
                          "actualVM": False, "authority": "none", "probes": [],
                          "recoveryEvidence": "already-terminal" if was_terminal else "native-termination-record"}
            else:
                outcome = qualify_backend(value["backendId"], QualificationAuthority(), bundle, store)
                report = {**outcome.report, "authority": "none-retained", "liveAuthorityExported": False}
    except (OSError, ContractError, ArtifactError, ExecutionDenied, ResourceError, RunDenied, ProtocolError):
        report = {**report, "status": "blocked-unqualified", "reason": "environment-or-output-rejected",
                  "qualified": False, "authority": "none"}
    if output is not None:
        try:
            with (output / "result.json").open("xb") as stream:
                stream.write(canonical(report) + b"\n")
        except OSError:
            report = {**report, "status": "blocked-unqualified", "reason": "output-write-failed",
                      "qualified": False, "authority": "none"}
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["qualified"] or report["status"] == "recovered" else 2


if __name__ == "__main__":
    sys.exit(main())
