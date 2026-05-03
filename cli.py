"""CLI entry: pipeline | single | help."""
import argparse
import logging
import os
import sys

HELP = """EDOM AI Email Ops (Python, minimal scope: M1 only)

Usage:
  python cli.py pipeline [--dry-run]              Run pipeline across all 5 sources
  python cli.py single <CONV_ID> [--dry-run]      Process one conversation by ID
  python cli.py help                              Show this help

Cost-control: each conversation gets exactly ONE AI review, gated by the
edom-ai/processed tag. To re-review, manually remove the tag in Front.
"""


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("help", "--help", "-h"):
        print(HELP)
        return 0 if len(sys.argv) >= 2 else 1

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("command", choices=["pipeline", "single"])
    parser.add_argument("conv_id", nargs="?", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    # Lazy-import so `help` works without env vars
    from pipeline import run_pipeline

    try:
        if args.command == "pipeline":
            run_pipeline(dry_run=args.dry_run)
        elif args.command == "single":
            if not args.conv_id:
                print("Missing CONV_ID for 'single' command", file=sys.stderr)
                return 1
            run_pipeline(conversation_id=args.conv_id, dry_run=args.dry_run)
    except Exception as exc:
        logging.exception("fatal")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
