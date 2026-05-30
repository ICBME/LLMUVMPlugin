from __future__ import annotations

from pathlib import Path
import sys


THIS_DIR = Path(__file__).resolve().parent
PY_DIR = THIS_DIR / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from fuzz_feedback.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
