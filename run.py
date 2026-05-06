"""Local dev entrypoint for Windows where the dydx SDK can't install.

Installs the dydx stub before importing the app, then runs uvicorn.
Production (Linux/Fly.io) skips this — uses `python -m uvicorn app:app` directly.

Usage: python run.py
"""
import os
import sys

# Python embeddable distros don't add the script's directory to sys.path
# automatically the way regular Python does. Insert it here so `_dydx_stub`
# (and the `app` package) resolve.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from _dydx_stub import install_dydx_stubs  # noqa: E402

install_dydx_stubs()

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=8000,
        log_level="info",
    )
