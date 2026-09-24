#!/usr/bin/env python3
"""Compile only the repository's fixed VM bridge; use an explicit new directory."""
import argparse
import json
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reproof.repair import CommandError, run_command
from reproof.core import ContractError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-new", required=True, type=Path)
    args = parser.parse_args()
    try:
        if platform.system() != "Darwin":
            raise RuntimeError()
        output = args.output_new.absolute()
        output.mkdir(mode=0o700, parents=False, exist_ok=False)
        native = ROOT / "native/macos-execution"
        commands = [
            ["/usr/bin/xcrun", "swiftc", "-O", "-target", "arm64-apple-macosx13.0",
             str(native / "main.swift"), "-o", str(output / "vm-helper")],
            ["/usr/bin/xcrun", "clang", "-O2", "-Wall", "-Wextra", "-Werror",
             str(native / "guest-connect.c"), "-o", str(output / "guest-connect")],
            ["/usr/bin/xcrun", "clang", "-O2", "-Wall", "-Wextra", "-Werror",
             str(native / "guest-run.c"), "-o", str(output / "guest-run")],
            ["/usr/bin/codesign", "--sign", "-", "--timestamp=none", "--entitlements",
             str(native / "entitlements.plist"), str(output / "vm-helper")],
        ]
        for command in commands:
            run_command(command, cwd=ROOT, timeout=90, max_output=256 * 1024)
        print(json.dumps({"status": "compiled", "actualVM": False, "qualified": False}))
        return 0
    except (OSError, RuntimeError, CommandError, ContractError):
        print(json.dumps({"status": "native-build-failed", "actualVM": False, "qualified": False}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
