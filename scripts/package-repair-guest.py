#!/usr/bin/env python3
"""Package fixed agent code for installation inside an explicitly owned guest."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reproloop.execution.artifacts import ArtifactError, read_regular
from reproloop.execution.guest_installation import InstallationError, package_agent
from reproloop.execution.wire import ProtocolError, decode_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--native-build", required=True, type=Path)
    parser.add_argument("--uid", required=True, type=int)
    parser.add_argument("--gid", required=True, type=int)
    parser.add_argument("--output-new", required=True, type=Path)
    args = parser.parse_args()
    try:
        path = args.catalog.absolute()
        catalog = decode_json(read_regular(Path(path.anchor), path.as_posix().lstrip("/"), maximum=256 * 1024))
        policy = package_agent(args.output_new, source_root=ROOT, native_root=args.native_build,
                               catalog=catalog, uid=args.uid, gid=args.gid)
        print(json.dumps({"status": "agent-packaged", "agentDigest": policy["agentDigest"],
                          "installed": False, "actualVM": False, "qualified": False}))
        return 0
    except (ArtifactError, InstallationError, ProtocolError, OSError):
        print(json.dumps({"status": "guest-package-rejected", "actualVM": False, "qualified": False}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
