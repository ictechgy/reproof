#!/usr/bin/env python3
"""Run the repository-owned, local-only causal checkout repair demonstration."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).absolute().parents[1]))

from reproof.offline_repair_demo import main


if __name__ == "__main__":
    raise SystemExit(main())
