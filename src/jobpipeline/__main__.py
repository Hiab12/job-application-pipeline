"""Command-line entry point.

Usage:
    python -m jobpipeline                 # full run
    python -m jobpipeline --dry-run       # fetch and log only, no paid calls, no writes
    python -m jobpipeline --limit 3       # analyze at most 3 offers (caps API costs)
    python -m jobpipeline --check-cv      # validate data/master_cv.yaml and show a summary
    python -m jobpipeline --setup-notion <page URL>   # create the Notion tracker database

Exit codes: 0 = run completed (per-offer errors are logged in the summary),
1 = fatal error during the run, 2 = invalid configuration.
"""

from __future__ import annotations

import argparse
import logging
import sys

from jobpipeline import __version__
from jobpipeline.config import load_settings
from jobpipeline.cv import load_master_cv
from jobpipeline.errors import ConfigError, NotionError, PipelineError
from jobpipeline.http import build_http_client
from jobpipeline.logging_setup import setup_logging
from jobpipeline.notion import NotionClient, create_tracker_database
from jobpipeline.pipeline import Pipeline

logger = logging.getLogger("jobpipeline")

EXIT_OK, EXIT_FATAL, EXIT_CONFIG = 0, 1, 2
# Keys a full run can't do without. Sources only need to be configured for the ones you
# want: a source without keys is skipped with a warning. Notion joins this list at step 7.
REQUIRED_FOR_FULL_RUN = ("anthropic",)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobpipeline",
        description="Fetch job offers, score them with Claude, generate tailored documents.",
    )
    parser.add_argument("--config", help="Path to config.yaml (default: ./config.yaml)")
    parser.add_argument("--env-file", default=".env", help="Path to the .env file")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and log offers only: no Claude calls, no files, no Notion writes",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Maximum number of offers to analyze"
    )
    parser.add_argument(
        "--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Override log level"
    )
    parser.add_argument(
        "--check-cv", action="store_true", help="Validate the master CV, print a summary, and exit"
    )
    parser.add_argument(
        "--setup-notion",
        metavar="PAGE_URL",
        help="Create the tracker database inside this Notion page, print its ID, and exit",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.limit is not None and args.limit < 1:
        print("--limit must be a positive integer", file=sys.stderr)
        return EXIT_CONFIG

    try:
        settings = load_settings(args.config, args.env_file)
    except ConfigError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return EXIT_CONFIG

    if args.check_cv:
        return check_cv(settings.config.paths.master_cv)
    if args.setup_notion:
        return setup_notion(settings, args.setup_notion)

    setup_logging(args.log_level or settings.config.logging.level, settings.config.paths.log_dir)

    if not args.dry_run:
        missing = settings.secrets.missing_for(*REQUIRED_FOR_FULL_RUN)
        if missing:
            logger.error("Missing keys in .env or environment: %s", ", ".join(missing))
            return EXIT_CONFIG
        try:
            load_master_cv(settings.config.paths.master_cv)
        except ConfigError as exc:
            logger.error("%s", exc)
            return EXIT_CONFIG

    try:
        Pipeline(settings).run(dry_run=args.dry_run, limit=args.limit)
    except PipelineError as exc:
        logger.error("Run stopped: %s", exc)
        return EXIT_FATAL
    except Exception:
        logger.exception("Unexpected error (this is a bug)")
        return EXIT_FATAL
    return EXIT_OK


def check_cv(path) -> int:
    """Validate the master CV and print what it contains."""
    try:
        cv = load_master_cv(path)
    except ConfigError as exc:
        print(f"Master CV is invalid:\n{exc}", file=sys.stderr)
        return EXIT_CONFIG

    bullets = sum(len(e.bullets) for e in [*cv.experience, *cv.projects])
    print(f"Master CV OK: {path}")
    print(f"  {cv.personal.name}: {cv.personal.title.en}")
    print(f"  {len(cv.experience)} experiences, {len(cv.projects)} projects, {bullets} bullets")
    print(f"  {sum(len(g.items) for g in cv.skills)} skills, {len(cv.education)} degrees")
    return EXIT_OK


def setup_notion(settings, page_url: str) -> int:
    """Create the tracker database with every column the pipeline needs."""
    token = settings.secrets.notion_token
    if token is None:
        print("NOTION_TOKEN is missing in .env", file=sys.stderr)
        return EXIT_CONFIG
    try:
        with build_http_client() as http:
            database = create_tracker_database(
                NotionClient(token.get_secret_value(), http), page_url
            )
    except NotionError as exc:
        print(f"Could not create the database:\n{exc}", file=sys.stderr)
        return EXIT_FATAL

    print("Notion database created.")
    if database["url"]:
        print(f"  Open it: {database['url']}")
    print("Add this line to your .env file:")
    print(f"  NOTION_DATABASE_ID={database['id']}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
