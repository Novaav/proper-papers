import sys
from pathlib import Path

# The synthetic corpus generator (standard library only) lives at the repository root.
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "corpus"))
