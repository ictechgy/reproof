#!/usr/bin/env python3
"""Build the fixed Android signing owner with explicit pinned local tools."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import sys
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reproloop.android_signing_tools import (  # noqa: E402
    AndroidSigningBuildTools, AndroidSigningToolsError,
    build_android_signing_owner,
)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-new", required=True, type=Path)
    parser.add_argument("--jdk-home", required=True, type=Path)
    for name in ("java", "javac", "jar", "clang", "apksigner-jar"):
        parser.add_argument("--" + name, required=True, type=Path)
        parser.add_argument("--" + name + "-sha256", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    return parser


def main(argv=None):
    arguments = _parser().parse_args(argv)
    cancellation = threading.Event()
    previous = {}
    try:
        if not 1 <= arguments.timeout_seconds <= 120:
            raise AndroidSigningToolsError(
                "android_signing_tools_configuration")
        for selected in (signal.SIGINT, signal.SIGTERM):
            previous[selected] = signal.signal(
                selected, lambda _number, _frame: cancellation.set())
        build_tools = AndroidSigningBuildTools(
            arguments.jdk_home,
            arguments.java, arguments.java_sha256,
            arguments.javac, arguments.javac_sha256,
            arguments.jar, arguments.jar_sha256,
            arguments.clang, arguments.clang_sha256,
            arguments.apksigner_jar, arguments.apksigner_jar_sha256)
        result = build_android_signing_owner(
            arguments.output_new, build_tools, cancellation=cancellation,
            deadline_monotonic=time.monotonic() + arguments.timeout_seconds)
        print(json.dumps({
            "schemaVersion": 1,
            "status": "built",
            "manifestDigest": result.manifest_digest,
            "outputDigest": result.output_digest,
            "ownerDefinitionDigest": result.tools.definition_digest,
        }, sort_keys=True))
        return 0
    except AndroidSigningToolsError as error:
        print(json.dumps({
            "schemaVersion": 1,
            "status": "failed",
            "code": error.code,
        }, sort_keys=True))
        return 2
    finally:
        for selected, handler in previous.items():
            signal.signal(selected, handler)


if __name__ == "__main__":
    sys.exit(main())
