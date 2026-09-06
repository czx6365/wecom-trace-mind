#!/usr/bin/env python3
"""CLI wrapper for generating a daily or weekly operations report."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.reports import generate_report


def parse_args():
    parser = argparse.ArgumentParser(description="Generate a Muse AI operations report")
    parser.add_argument("--type", choices=("daily", "weekly"), default="daily")
    parser.add_argument("--date", help="Anchor date in YYYY-MM-DD format")
    parser.add_argument("--force", action="store_true", help="Replace an existing report for the same period")
    parser.add_argument("--push", action="store_true", help="Push the generated report to the configured WeCom webhook")
    parser.add_argument("--mark-done", action="store_true", help="Persist scheduler completion state")
    return parser.parse_args()


def main():
    args = parse_args()
    result = generate_report(
        report_type=args.type,
        date_str=args.date,
        force=args.force,
        push=args.push,
        mark_done=args.mark_done,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
