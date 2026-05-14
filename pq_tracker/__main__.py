from __future__ import annotations

import argparse
import sys
from datetime import date

from . import ingest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pq-tracker",
                                description="Track Irish parliamentary questions by keyword.")
    p.add_argument("--lookback-days", type=int, default=None,
                   help="Override lookback_days from topics.yaml.")
    p.add_argument("--start-date", type=date.fromisoformat, default=None,
                   metavar="YYYY-MM-DD",
                   help="Backfill from this date to today (overrides --lookback-days).")
    p.add_argument("--no-log-file", action="store_true",
                   help="Don't write a run log file (still logs to stdout).")
    args = p.parse_args(argv)
    result = ingest.run(lookback_days=args.lookback_days,
                        start_date=args.start_date,
                        log_to_file=not args.no_log_file)
    print(f"new={result['new_questions']} newly_answered={result['newly_answered']} errors={result['errors']}")
    if result.get("log_path"):
        print(f"log: {result['log_path']}")
    return 0 if result["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
