import logging
import os
from web.logging_config import setup_logging


def _reset_root():
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


def test_setup_logging_plain_default(monkeypatch):
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    _reset_root()
    setup_logging()
    root = logging.getLogger()
    assert len(root.handlers) == 1
    formatter = root.handlers[0].formatter
    assert isinstance(formatter, logging.Formatter)
    assert "JsonFormatter" not in type(formatter).__name__


def test_setup_logging_json_when_env_set(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "json")
    _reset_root()
    setup_logging()
    root = logging.getLogger()
    assert len(root.handlers) == 1
    formatter = root.handlers[0].formatter
    assert "JsonFormatter" in type(formatter).__name__


def test_setup_logging_replaces_existing_handlers(monkeypatch):
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    _reset_root()
    root = logging.getLogger()
    root.addHandler(logging.StreamHandler())
    root.addHandler(logging.StreamHandler())
    assert len(root.handlers) == 2
    setup_logging()
    assert len(root.handlers) == 1
