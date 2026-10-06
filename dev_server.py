"""Local dev server: serves web/ and proxies /api/agent to the model provider.

Vercel runs web/api/agent.js in production. Locally there is no Vercel, so this
does the same job with the same contract, delegating provider choice, model
tiers and response parsing to exception_desk/providers.py. It exists so
`make serve` gives you the real thing rather than a version of the app with the
interesting half switched off.

    OPENAI_API_KEY=sk-... make serve      # or put it in .env
"""

from __future__ import annotations

import json
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
sys.path.insert(0, str(ROOT))

from exception_desk import providers  # noqa: E402

#: Keys and model overrides are read from .env so `make serve` needs no exports.
ENV_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MODEL_PROVIDER",
            "MODEL_FAST", "MODEL_REASON")


def _load_env() -> None:
    """Merge .env into os.environ without clobbering real exports."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name in ENV_KEYS and not os.environ.get(name):
            os.environ[name] = value.strip().strip('"').strip("'")


def _have_key() -> bool:
    conf = providers.DEFAULTS[providers.active_provider()]
    return bool(os.environ.get(conf["key_env"]))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB), **kw)

    def log_message(self, fmt, *args):
        # Quieten static-file noise, but coerce first: log_error passes an
        # HTTPStatus enum here, not a string, and `in` on an enum raises —
        # which turned every 404 into a traceback instead of a log line.
        if args and "/api/" in str(args[0]):
            super().log_message(fmt, *args)

    def do_GET(self):
        # Browsers ask for these unprompted. Answer them rather than logging a
        # miss for each one on every page load.
        if self.path in ("/favicon.ico", "/apple-touch-icon.png",
                         "/apple-touch-icon-precomposed.png"):
            self.send_response(204)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        super().do_GET()

    def do_POST(self):
        if self.path.rstrip("/") != "/api/agent":
            self.send_error(404)
            return
        if not _have_key():
            conf = providers.DEFAULTS[providers.active_provider()]
            self._json({"error": "no_key",
                        "message": f"{conf['key_env']} not set. Put it in .env or export it."}, 503)
            return
        try:
            n = int(self.headers.get("content-length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            tier = "reason" if body.get("tier") == "reason" else "fast"
            out = providers.complete(body["system"], body["user"], tier)
            self._json(out)
        except Exception as exc:  # noqa: BLE001 - dev server: show the error
            self._json({"error": "upstream", "message": f"{type(exc).__name__}: {exc}"}, 502)

    def _json(self, obj, status=200):
        raw = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    _load_env()
    port = int(os.environ.get("PORT", "8000"))
    prov = providers.active_provider()
    if _have_key():
        have = (f"live agents ON · {prov} · fast={providers.model_for('fast')} "
                f"reason={providers.model_for('reason')}")
    else:
        have = f"live agents OFF (no {providers.DEFAULTS[prov]['key_env']})"
    print(f"  http://localhost:{port}   [{have}]")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
