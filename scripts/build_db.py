"""建库 CLI。

    python scripts/build_db.py
    python scripts/build_db.py --db data/rentwise.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rentwise.build import build, report  # noqa: E402
from rentwise.config import DB_PATH  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="建 RentWise 数据库")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    build(args.db, verbose=not args.quiet)
    report(args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
