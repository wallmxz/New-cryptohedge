from __future__ import annotations
import logging
import os
from pythonjsonlogger import jsonlogger


def setup_logging() -> None:
    """Configure root logger based on LOG_FORMAT env var.

    LOG_FORMAT=json -> JSON output (one object per line, suitable for Fly.io / log aggregators)
    LOG_FORMAT=plain (default) -> human-readable single-line text
    """
    fmt = os.environ.get("LOG_FORMAT", "plain").lower()
    handler = logging.StreamHandler()

    if fmt == "json":
        formatter = jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={
                "levelname": "level",
                "name": "logger",
                "asctime": "timestamp",
            },
        )
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        )

    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
