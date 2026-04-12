from __future__ import annotations
import base64
import secrets
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class BasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, username: str, password: str, exclude: list[str] | None = None):
        super().__init__(app)
        self._username = username
        self._password = password
        self._exclude = exclude or []

    async def dispatch(self, request: Request, call_next):
        for path in self._exclude:
            if request.url.path.startswith(path):
                return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return Response("Unauthorized", status_code=401,
                          headers={"WWW-Authenticate": 'Basic realm="AutoMoney"'})

        try:
            decoded = base64.b64decode(auth[6:]).decode()
            user, passwd = decoded.split(":", 1)
        except Exception:
            return Response("Invalid credentials", status_code=401,
                          headers={"WWW-Authenticate": 'Basic realm="AutoMoney"'})

        if not (secrets.compare_digest(user, self._username)
                and secrets.compare_digest(passwd, self._password)):
            return Response("Invalid credentials", status_code=401,
                          headers={"WWW-Authenticate": 'Basic realm="AutoMoney"'})

        return await call_next(request)
