#!/usr/bin/env python3
"""Seal explicitly supplied preinstalled VM resources; never boot or download."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reproof.execution.artifacts import ArtifactError, read_regular
from reproof.execution.resources import RESOURCE_FILES, ResourceError, provision
from reproof.execution.wire import ProtocolError, decode_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--output-new", required=True, type=Path)
    for key in RESOURCE_FILES:
        parser.add_argument("--" + key, required=True, type=Path)
    args = parser.parse_args()
    try:
        path = args.metadata.absolute()
        metadata = decode_json(read_regular(Path(path.anchor), path.as_posix().lstrip("/"), maximum=256 * 1024))
        bundle = provision(args.output_new, metadata=metadata, resources={key: getattr(args, key) for key in RESOURCE_FILES})
        print(json.dumps({"status": "resources-sealed", "environmentDigest": bundle.environment_digest,
                          "actualVM": False, "qualified": False}))
        return 0
    except (ArtifactError, ResourceError, ProtocolError, OSError):
        print(json.dumps({"status": "provisioning-rejected", "actualVM": False, "qualified": False}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
