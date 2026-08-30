"""CLI wrapper for the versioned offline Memory Benchmark."""

from pathlib import Path
import sys

# Running a file under ``scripts/`` makes that directory the import root.
# Add the repository root explicitly so the documented command works without
# an editable install or shell-specific PYTHONPATH configuration.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.memory_benchmark import main


if __name__ == "__main__":
    raise SystemExit(main())
