#!/usr/bin/env python3
"""Run one batch-oriented message classification pass from the command line."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.classifier import classify_recent


def parse_args():
    parser = argparse.ArgumentParser(description="Classify unprocessed community messages")
    parser.add_argument("--days", type=int, default=None, help="Only consider messages from the last N days")
    parser.add_argument("--limit", type=int, default=5000, help="Maximum number of messages to process")
    parser.add_argument("--retry-failed", action="store_true", help="Retry messages previously marked as failed")
    parser.add_argument("--no-risk-push", action="store_true", help="Disable risk webhook delivery for this run")
    return parser.parse_args()


def main():
    args = parse_args()
    result = classify_recent(
        days=args.days,
        limit=args.limit,
        retry_failed=args.retry_failed,
        push_risks=not args.no_risk_push,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
