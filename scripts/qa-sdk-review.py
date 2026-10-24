#!/usr/bin/env python3
"""Read repository synthetic fixtures and print a bounded inert review demo."""
from pathlib import Path
import json
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reproof.sdk_review_demo import run_demo

if __name__ == '__main__':
    print(json.dumps(run_demo(ROOT), indent=2, sort_keys=True))
