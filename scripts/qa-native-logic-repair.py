#!/usr/bin/env python3
"""Compile and test the bounded, repository-owned Swift logic repair exercise."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).absolute().parents[1]))

from reproof.native_logic_repair_demo import main


if __name__ == "__main__":
    raise SystemExit(main())
