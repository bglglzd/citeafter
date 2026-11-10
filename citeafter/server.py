"""Loopback-only application surface with same-origin, token-bound mutations."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
from pathlib import Path
import secrets
import threading
from urllib.parse import parse_qs, urlsplit

from .policy import CiteafterError, canonical, fields, require, strict_json

ASSETS = Path(__file__).resolve().parent / "assets"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/app.css": ("app.css", "text/css; charset=utf-8"),
          "/mark.svg": ("mark.svg", "image/svg+xml"), "/icon.svg": ("icon.svg", "image/svg+xml")}
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"


def make_server(engine, *, port=0, demo=None):
    require(type(port) is int and 0 <= port <= 65535, "Port must be zero to65535")
    token, lock = secrets.token_urlsafe(32), threading.RLock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "Citeafter/0.1"
        sys_version = ""
        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def log_message(self, *_):
            pass

        def send_bytes(self, status, body, content_type="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def reply(self, status, data):
            self.send_bytes(status, canonical(data).encode("utf-8"))

        def boundary(self):
            expected_host = "127.0.0.1:" + str(self.server.server_port)
            require(self.headers.get_all("Host") == [expected_host], "Use the printed loopback URL", "forbidden")
            parsed = urlsplit(self.path)
            require(not parsed.scheme and not parsed.netloc and not parsed.fragment, "Use an origin-relative request")
            return parsed

        def failure(self, error):
            status = 403 if error.code == "forbidden" else (404 if error.code == "not_found" else 409 if error.code in
                     ("stale_preview", "stale", "needs_review", "source_unavailable", "not_recheck_eligible", "already_observed", "fixture_order") else 400)
            self.reply(status, {"error": {"code": error.code, "message": str(error)}})

        def do_GET(self):
            try:
                parsed = self.boundary()
                if parsed.path == "/api/state":
                    require(not parsed.query, "State does not accept a history date")
                    with lock:
                        result = engine.state()
                    self.reply(200, {**result, "csrf_token": token})
                elif parsed.path == "/api/as-of":
                    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=3)
                    require(set(query) == {"action_id", "valid_at", "known_at"} and all(len(value) == 1 for value in query.values()), "Supply exactly one action, business time and knowledge time")
                    with lock:
                        result = engine.as_of(query["action_id"][0], valid_at=query["valid_at"][0], known_at=query["known_at"][0])
                    self.reply(200, result)
                elif parsed.path in STATIC and not parsed.query:
                    name, mime = STATIC[parsed.path]
                    require((ASSETS / name).is_file(), "Application asset is unavailable", "not_found")
                    self.send_bytes(200, (ASSETS / name).read_bytes(), mime)
                else:
                    raise CiteafterError("not_found", "Page does not exist")
            except CiteafterError as error:
                self.failure(error)
            except (ValueError, UnicodeError):
                self.failure(CiteafterError("invalid_input", "Malformed request"))

        def do_POST(self):
            try:
                parsed = self.boundary()
                require(not parsed.query, "Mutations do not accept query parameters")
                origin = "http://127.0.0.1:" + str(self.server.server_port)
                require(self.headers.get_all("Origin") in (None, [origin]), "Origin is not this local application", "forbidden")
                values = self.headers.get_all("X-Citeafter-Token")
                require(values is not None and len(values) == 1 and hmac.compare_digest(values[0], token), "Reload the local application before changing state", "forbidden")
                require(self.headers.get_all("Content-Type") == ["application/json"], "Use application/json")
                lengths = self.headers.get_all("Content-Length")
                require(self.headers.get_all("Transfer-Encoding") is None and lengths is not None and len(lengths) == 1
                        and lengths[0].isascii() and lengths[0].isdecimal() and 0 < int(lengths[0]) <= 512 * 1024,
                        "Supply one bounded Content-Length")
                size = int(lengths[0]); body = self.rfile.read(size)
                require(len(body) == size, "Incomplete request")
                request = strict_json(body, limit=512 * 1024)
                with lock:
                    if parsed.path == "/api/recheck":
                        fields(request, ["action_id"], "recheck"); result = engine.recheck(request["action_id"])
                    elif parsed.path == "/api/preview":
                        fields(request, ["action_id"], "preview"); result = engine.preview(request["action_id"])
                    elif parsed.path == "/api/release":
                        fields(request, ["preview_id"], "release"); result = engine.release(request["preview_id"])
                    elif parsed.path == "/api/refresh":
                        fields(request, [], "refresh"); result = demo.refresh() if demo else engine.refresh()
                    elif parsed.path == "/api/import":
                        fields(request, ["raw_text"], "import"); require(isinstance(request["raw_text"], str), "Supply UTF-8 policy text")
                        result = engine.ingest(request["raw_text"].encode("utf-8", "strict"))
                    elif parsed.path == "/api/demo/advance":
                        require(demo is not None, "Fixture clock is unavailable in operational mode", "forbidden")
                        fields(request, ["version"], "fixture"); result = demo.advance(request["version"])
                    else:
                        raise CiteafterError("not_found", "Action does not exist")
                self.reply(200, result)
            except CiteafterError as error:
                self.failure(error)
            except (ValueError, UnicodeError):
                self.failure(CiteafterError("invalid_input", "Malformed request"))

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def serve(engine, *, port=0, demo=None):
    server = make_server(engine, port=port, demo=demo)
    print("Citeafter: http://127.0.0.1:" + str(server.server_port) + "/", flush=True)
    print("Synthetic fixture clock; local SQLite outbox only." if demo else "Local SQLite outbox only.", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
