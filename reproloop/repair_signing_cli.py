"""Local status and measured recovery for the fixed Android signing owner."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import threading
import time

from . import contracts
from .contracts.versions import require
from .android_signing_tools import (
    AndroidSigningBuildTools, AndroidSigningToolsError, build_android_signing_owner,
)
from .execution.artifacts import ArtifactError
from .execution.journal import RunDenied, TERMINAL
from .execution.wire import ProtocolError
from .repair_android_signing import AndroidSigningError
from .repair_signing_configuration import load_android_signing_configuration
from .repair_signing_recovery import SigningRecoveryError


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, "Invalid Android signing command arguments; use --help\n")


def _build_tools(arguments):
    require(1 <= arguments.timeout_seconds <= 120, "Invalid tool build timeout")
    cancellation = threading.Event()
    previous = {}
    try:
        for selected in (signal.SIGINT, signal.SIGTERM):
            previous[selected] = signal.signal(selected, lambda _number, _frame: cancellation.set())
        tools = AndroidSigningBuildTools(arguments.jdk_home,
            arguments.java, arguments.java_sha256, arguments.javac, arguments.javac_sha256,
            arguments.jar, arguments.jar_sha256, arguments.clang, arguments.clang_sha256,
            arguments.apksigner_jar, arguments.apksigner_jar_sha256)
        result = build_android_signing_owner(arguments.output_new, tools,
            cancellation=cancellation, deadline_monotonic=time.monotonic() + arguments.timeout_seconds)
        return {"kind": "android-signing-tools-v1", "status": "built",
                "manifestDigest": result.manifest_digest, "outputDigest": result.output_digest,
                "ownerDefinitionDigest": result.tools.definition_digest}
    finally:
        for selected, handler in previous.items():
            signal.signal(selected, handler)


def main(argv=None):
    parser = _ArgumentParser(prog="reproloop android-signing",
        description="Build fixed Android signing tools, inspect or recover an existing operation")
    actions = parser.add_subparsers(dest="action", required=True)
    build = actions.add_parser("build-tools")
    build.add_argument("--output-new", type=Path, required=True)
    build.add_argument("--jdk-home", type=Path, required=True)
    for name in ("java", "javac", "jar", "clang", "apksigner-jar"):
        build.add_argument("--" + name, type=Path, required=True)
        build.add_argument("--" + name + "-sha256", required=True)
    build.add_argument("--timeout-seconds", type=float, default=90)
    for name in ("status", "recover"):
        child = actions.add_parser(name)
        child.add_argument("--config", type=Path, required=True,
                           help="Administrator-selected public signing owner JSON")
        child.add_argument("--operation", required=True)
        if name == "recover":
            child.add_argument("--request-digest", required=True,
                               help="Digest of the original admitted signing request")
    args = parser.parse_args(argv)
    operations = None
    report = {"schemaVersion": 1, "kind": "android-signing-recovery-v1"}
    result = 2
    try:
        if args.action == "build-tools":
            report.update(_build_tools(args))
        else:
            contracts.validate_id(args.operation)
            if args.action == "recover":
                contracts.validate_digest(args.request_digest)
            config = load_android_signing_configuration(args.config)
            operations = config.open_existing()
        if args.action == "status":
            report.update(status="observed", operation=operations.status(args.operation))
        elif args.action == "recover":
            row = operations.run_store.status(args.operation)
            require(row["requestDigest"] == args.request_digest,
                              "Signing request binding mismatch")
            observed = operations.status(args.operation)
            require(observed.get("definitionDigest") == operations.definition_digest
                and observed.get("requestDigest") == args.request_digest,
                "Original signing intent required")
            if row["state"] in TERMINAL:
                report.update(status="already-terminal", operationId=args.operation,
                              state=row["state"], reservedBytes=row["reservedBytes"])
            else:
                with operations.recovery(args.operation, args.request_digest) as capability:
                    row = operations.run_store.finish_signing_recovery(capability, authority=operations)
                    report.update(status="recovered", operationId=args.operation,
                        state=row["state"], reservedBytes=row["reservedBytes"],
                        evidenceDigest=capability.evidence_digest)
        result = 0
    except AndroidSigningToolsError as error:
        report.update(status="rejected", error={"code": error.code,
                      "message": str(error)}, cleanupConfirmed=error.cleanup_confirmed)
    except (contracts.ContractError, ArtifactError, ProtocolError,
            AndroidSigningError, SigningRecoveryError, RunDenied, OSError, TypeError, ValueError):
        report.update(status="rejected", error={"code": "signing_operation_unavailable",
            "message": "Check the existing owner configuration, operation and original request"})
    except KeyboardInterrupt:
        report.update(status="interrupted")
        result = 130
    finally:
        if operations is not None:
            operations.close()
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
