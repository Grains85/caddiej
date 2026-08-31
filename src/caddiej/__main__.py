"""Command-line entry point for CaddieJ."""

import asyncio
import logging
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from caddiej.app import run
from caddiej.cli import (
    build_parser,
    describe,
    explain,
    load_settings,
    resolve_log_level,
)
from caddiej.runtime.logging import JsonFormatter as _JsonFormatter
from caddiej.runtime.logging import LoggingRuntime, configure_logging, shutdown_logging

# Existing tests import the formatter from this module.
__all__ = ["_JsonFormatter", "main"]


def _configure_logging(level: str = "INFO") -> LoggingRuntime:
    return configure_logging(level)


def main(argv: Sequence[str] | None = None) -> None:
    """Run CaddieJ from either the module or console entry point.

    Configuration is read here rather than inside the application, so that a
    mistyped setting is a sentence on stderr and an exit code, instead of a
    traceback wrapped in a log line that nobody can read at the moment they most
    need to.
    """

    options = build_parser().parse_args(argv)
    runtime = _configure_logging(resolve_log_level(options.log_level))
    logger = logging.getLogger(__name__)

    try:
        try:
            settings = load_settings()
        except ValidationError as error:
            print(explain(error), file=sys.stderr)
            raise SystemExit(2) from None

        if options.check_config:
            print(describe(settings))
            return

        try:
            asyncio.run(run(settings))
        except KeyboardInterrupt:
            logger.info("shutdown_requested")
            raise SystemExit(130) from None
        except Exception:
            logger.exception("application_failed")
            raise SystemExit(1) from None
    finally:
        shutdown_logging(runtime)


if __name__ == "__main__":
    main()
