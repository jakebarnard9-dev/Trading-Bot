"""Thin wrapper: python scripts/preview_week.py [--start YYYY-MM-DD] [--days 7] [--deep] [--write FILE]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from earnings_bot.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["preview", *sys.argv[1:]]))
