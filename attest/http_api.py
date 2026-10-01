"""A plain JSON API over the standard library. No framework, no SDK, no protocol
version to negotiate. Anything that can POST JSON can use it."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from .service import Service, VERSION

TOKEN_ENV = "ATTEST_TOKEN"


def token_file(home: Optional[str] = None) -> str:
    base = (home or os.environ.get("ATTEST_HOME")
            or os.path.join(os.path.expanduser("~"), ".attest"))
    return os.path.join(base, "token.txt")


def resolve_token(home: Optional[str] = None, create: bool = True) -> Optional[str]:
    """The bearer token, from the environment or a 0600 file beside the store.

    The service REFUSES TO START without one rather than starting open. An
    unauthenticated service that can execute code is not a service, it is a
    remote shell with a JSON front end.
    """
    env = os.environ.get(TOKEN_ENV)
    if env and env.strip():
        return env.strip()
    path = token_file(home)
    try:
        with open(path, encoding="utf-8") as fh:
            t = fh.read().strip()
        if t:
            return t
    except Exception:
        pass
    if not create:
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    t = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.write(fd, t.encode())
    os.close(fd)
    return t


class Handler(BaseHTTPRequestHandler):
    server_version = "attest/" + VERSION

    def _send(self, code: int, obj: Dict[str, Any]) -> None:
        body = json.dumps(obj, indent=2, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        offered = self.headers.get("Authorization", "")
        if offered.startswith("Bearer "):
            offered = offered[7:].strip()
        if not offered:
            offered = self.headers.get("X-Attest-Token", "").strip()
        # Constant-time compare. A token check that leaks length is a token check
        # that leaks.
        return bool(offered) and hmac.compare_digest(offered, self.server.token)

    def do_OPTIONS(self) -> None:
        self._send(204, {})

    def do_GET(self) -> None:
        p = self.path.split("?")[0].rstrip("/") or "/"
        if p in ("/attest/v1/health", "/health"):
            return self._send(200, {"ok": True, "service": "attest",
                                    "version": VERSION, "time": time.time()})
        if p in ("/attest/v1/manifest", "/attest/v1", "/attest"):
            return self._send(200, self.server.svc.manifest())
        if p == "/attest/v1/capabilities":
            if not self._authed():
                return self._send(401, {"ok": False,
                                        "error": "bearer token required"})
            return self._send(200, self.server.svc.capabilities())
        return self._send(404, {"ok": False, "error": "no such endpoint",
                                "see": "/attest/v1/manifest"})

    def do_POST(self) -> None:
        p = self.path.split("?")[0].rstrip("/")
        if not self._authed():
            return self._send(401, {"ok": False, "error": "bearer token required"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 4_000_000:
            return self._send(413, {"ok": False, "error": "body too large"})
        raw = self.rfile.read(n) if n else b"{}"
        try:
            args = json.loads(raw.decode() or "{}")
        except Exception as exc:
            return self._send(400, {"ok": False, "error": "invalid JSON: %s" % exc})
        name = args.pop("operation", None) or p.rsplit("/", 1)[-1]
        return self._send(200, self.server.svc.dispatch(name, args))

    def log_message(self, fmt: str, *a) -> None:
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % a))


def serve(host: str = "127.0.0.1", port: int = 8250,
          svc: Optional[Service] = None) -> None:
    svc = svc or Service()
    tok = resolve_token()
    if not tok:
        raise SystemExit("attest: refusing to start without a token")
    threading.Thread(target=svc.warm, daemon=True).start()
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.token = tok
    srv.svc = svc
    srv.daemon_threads = True
    print("attest %s listening on http://%s:%d" % (VERSION, host, port), flush=True)
    print("manifest : http://%s:%d/attest/v1/manifest" % (host, port), flush=True)
    print("auth     : bearer token from %s or $%s" % (token_file(), TOKEN_ENV),
          flush=True)
    srv.serve_forever()
