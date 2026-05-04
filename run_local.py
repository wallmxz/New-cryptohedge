"""Local dev launcher (Windows): stubs dydx-v4-client, then starts uvicorn.

The dydx-v4-client SDK has native deps that don't compile on Windows Python 3.13.
Production runs on Linux/Fly.io where the SDK is available. For local UI testing
on Windows, this wrapper installs the same stub used by the test suite (via
`tests/conftest.py`) before importing `app`, so uvicorn boots cleanly.

Run: python run_local.py
"""
from __future__ import annotations
import sys
import os

# Ensure tests/ is importable so we can pull in the conftest stub
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "tests"))
sys.path.insert(0, os.path.dirname(__file__))

# Triggers _install_dydx_stubs() at import time
import conftest  # noqa: F401

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        log_level="info",
    )
