import logging
import sys

import structlog


def configure_logging(env: str) -> None:
    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    if env in ("local", "test"):
        processors.append(structlog.dev.ConsoleRenderer())
    else:
        processors += [structlog.processors.format_exc_info, structlog.processors.JSONRenderer()]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )


log = structlog.get_logger()
