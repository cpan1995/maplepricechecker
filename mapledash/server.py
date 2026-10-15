"""Tiny local web server: the dashboard page plus a JSON API over queries.Market.
Binds to 127.0.0.1 only, so nothing is reachable from other machines."""
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

log = logging.getLogger("mapledash")
STATIC = Path(__file__).resolve().parent / "static"
PAGE = STATIC / "index.html"
TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


def make_handler(market):
    routes = [
        (r"/api/overview", lambda m, q: market.overview()),
        (r"/api/deals", lambda m, q: market.deals()),
        (r"/api/items", lambda m, q: market.items(q.get("q", [""])[0])),
        (r"/api/item/(\d+)", lambda m, q: market.item(int(m.group(1)))),
        (r"/api/shops", lambda m, q: market.shops()),
        (r"/api/shop/(.+)", lambda m, q: market.shop(unquote(m.group(1)))),
        (r"/api/settings", lambda m, q: market.settings()),
    ]
    posts = {
        "/api/settings": lambda body: market.set_ttl(body["ttl_hours"]),
        "/api/clear": lambda body: market.clear() if body.get("confirm") is True else None,
    }

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body, ctype):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            try:
                if url.path in ("/", "/index.html"):
                    return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                static = re.fullmatch(r"/static/([\w-]+(\.[\w-]+)*)", url.path)  # bundled, works offline
                if static and (STATIC / static.group(1)).is_file():
                    ctype = TYPES.get(Path(static.group(1)).suffix, "application/octet-stream")
                    return self._send(200, (STATIC / static.group(1)).read_bytes(), ctype)
                icon = re.fullmatch(r"/icon/(\d+)\.png", url.path)
                if icon:
                    png = market.icon(int(icon.group(1)))
                    return self._send(200, png, "image/png") if png else self._send(404, b"", "text/plain")
                for pattern, fn in routes:
                    m = re.fullmatch(pattern, url.path)
                    if m:
                        data = fn(m, parse_qs(url.query))
                        if data is None:
                            return self._send(404, b"null", "application/json")
                        return self._send(200, json.dumps(data).encode(), "application/json")
                self._send(404, b"not found", "text/plain")
            except Exception:
                log.exception("request failed: %s", self.path)
                self._send(500, b'{"error": "server error"}', "application/json")

        def do_POST(self):
            url = urlparse(self.path)
            fn = posts.get(url.path)
            # JSON only: a web page can't send that cross-site without a CORS
            # preflight we never answer, so other sites can't wipe your data.
            if not fn or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._send(404, b"not found", "text/plain")
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                data = fn(body)
                if data is None:
                    return self._send(400, b'{"error": "bad request"}', "application/json")
                self._send(200, json.dumps(data).encode(), "application/json")
            except (ValueError, KeyError, TypeError) as e:
                self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            except Exception:
                log.exception("request failed: %s", self.path)
                self._send(500, b'{"error": "server error"}', "application/json")

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

    return Handler


def serve(market, port):
    for p in (port, 0):  # fall back to any free port
        try:
            return ThreadingHTTPServer(("127.0.0.1", p), make_handler(market))
        except OSError:
            continue
    raise RuntimeError("no free port")
