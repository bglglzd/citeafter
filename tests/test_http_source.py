"""Actual loopback transport fixtures. No policy, temporal engine or gold data."""

from contextlib import contextmanager
from dataclasses import FrozenInstanceError
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from citeafter import http_source as source

FIRST = ' {"synthetic_transport":"первый"}\n'.encode()
SECOND = b'{"synthetic_transport":"second"}\r\n'


class Publisher(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.calls = []
        self.body = FIRST
        self.etag = '"synthetic-v1"'
        self.stop_event = threading.Event()
        self.lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def reply(
        self,
        status=200,
        body=FIRST,
        *,
        headers=(),
        mime="application/json",
        length=True,
    ):
        self.send_response(status)
        if mime is not None:
            self.send_header("Content-Type", mime)
        if length:
            self.send_header("Content-Length", str(len(body)))
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        if status != 304:
            self.wfile.write(body)

    def do_GET(self):
        self.close_connection = True
        with self.server.lock:
            self.server.calls.append(
                {"path": self.path, "headers": dict(self.headers.items())}
            )
        try:
            self.route()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # The client intentionally closes refused/deadline fixtures.

    def route(self):
        path = self.path
        if path == "/publisher":
            with self.server.lock:
                body, etag = self.server.body, self.server.etag
            if self.headers.get("If-None-Match") == etag:
                return self.reply(304, body, headers=[("ETag", etag)], mime=None)
            return self.reply(body=body, headers=[("ETag", etag)])
        if path == "/bare304":
            return self.reply(304, mime=None, length=False)
        if path in ("/weak304", "/different304"):
            tag = 'W/"synthetic-v1"' if path == "/weak304" else '"different"'
            return self.reply(304, headers=[("ETag", tag)], mime=None, length=False)
        if path == "/bad-etag":
            return self.reply(headers=[("ETag", "invalid-unquoted")])
        if path == "/utf8":
            return self.reply(body=b'{"synthetic":"\xff"}')
        if path == "/json":
            return self.reply(body=b'{"synthetic":')
        if path == "/nonfinite":
            return self.reply(body=b'{"synthetic":NaN}')
        if path == "/mime":
            return self.reply(mime="text/plain")
        if path == "/charset":
            return self.reply(mime="application/json; charset=iso-8859-1")
        if path == "/coding":
            return self.reply(headers=[("Content-Encoding", "gzip")])
        if path == "/large":
            return self.reply(body=b'{"synthetic":"' + b"x" * 1024 + b'"}')
        if path == "/redirect":
            return self.reply(302, headers=[("Location", "/target")])
        if path == "/not-found":
            return self.reply(
                404, body=b'{"detail":"synthetic-private-publisher-detail"}'
            )
        if path == "/truncated":
            return self.reply(
                length=False, headers=[("Content-Length", str(len(FIRST) + 10))]
            )
        if path == "/duplicate-length":
            return self.reply(headers=[("Content-Length", str(len(FIRST)))])
        if path == "/close-delimited":
            return self.reply(length=False)
        if path in ("/bad-chunk-delimiter", "/missing-chunk-end"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            malformed = (
                b"2\r\n{}XX0\r\n\r\n"
                if path == "/bad-chunk-delimiter"
                else b"2\r\n{}\r\n0\r\n"
            )
            self.wfile.write(malformed)
            return
        if path in ("/chunked", "/incomplete-chunk", "/mixed-framing", "/large-chunk"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            if path == "/mixed-framing":
                self.send_header("Content-Length", str(len(FIRST)))
            self.end_headers()
            if path == "/incomplete-chunk":
                self.wfile.write(b'100\r\n{"synthetic":1}\r\n')
                return
            body = (
                FIRST
                if path != "/large-chunk"
                else b'{"synthetic":"' + b"x" * 1024 + b'"}'
            )
            for offset in range(0, len(body), 7):
                chunk = body[offset : offset + 7]
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        if path == "/slow-headers":
            for byte in b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}":
                if self.server.stop_event.is_set():
                    return
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
                time.sleep(0.025)
            return
        if path == "/slow-body":
            body = b'{"synthetic":"' + b"x" * 100 + b'"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            for byte in body:
                if self.server.stop_event.is_set():
                    return
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
                time.sleep(0.025)
            return
        if path == "/timeout":
            self.server.stop_event.wait(2)
            return
        return self.reply()


@contextmanager
def publisher(context=None):
    server = Publisher()
    if context:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}
    )
    thread.start()
    try:
        yield (
            server,
            ("https" if context else "http")
            + "://127.0.0.1:"
            + str(server.server_port),
        )
    finally:
        server.stop_event.set()
        server.shutdown()
        server.server_close()
        thread.join(2)
        if thread.is_alive():
            raise AssertionError("Owned publisher did not stop")


class HTTPSourceTests(unittest.TestCase):
    def test_malformed_chunk_delimiters_are_refused(self):
        with publisher() as (_server, origin):
            for route in ("/bad-chunk-delimiter", "/missing-chunk-end"):
                with self.subTest(route=route):
                    with self.assertRaises(source.TypedFetchError) as failure:
                        source.fetch_policy(origin + route)
                    self.assertEqual(failure.exception.code, "framing")

    def test_actual_conditional_cycle_preserves_received_bytes_and_frozen_result(self):
        with publisher() as (server, origin):
            first = source.fetch_policy(origin + "/publisher")
            self.assertEqual(
                (first.status, first.body, first.etag), (200, FIRST, '"synthetic-v1"')
            )
            self.assertEqual(first.response_sha256, hashlib.sha256(FIRST).hexdigest())
            self.assertGreater(first.elapsed_ms, 0)
            self.assertEqual(
                set(vars(first)),
                {"status", "body", "etag", "response_sha256", "elapsed_ms"},
            )
            with self.assertRaises(FrozenInstanceError):
                first.status = 304
            cached = source.fetch_policy(origin + "/publisher", etag=first.etag)
            self.assertEqual(
                (cached.status, cached.body, cached.etag, cached.response_sha256),
                (304, None, first.etag, None),
            )
            server.body = SECOND
            server.etag = '"synthetic-v2"'
            changed = source.fetch_policy(origin + "/publisher", etag=first.etag)
            self.assertEqual(changed.body, SECOND)
            self.assertEqual(
                changed.response_sha256, hashlib.sha256(SECOND).hexdigest()
            )
            self.assertEqual(len(server.calls), 3)
            self.assertEqual(server.calls[1]["headers"]["If-None-Match"], first.etag)

    def test_304_conditional_requirement_weak_comparison_and_missing_response_tag(self):
        with publisher() as (_server, origin):
            for route in ("/bare304", "/weak304"):
                with self.subTest(route=route):
                    result = source.fetch_policy(origin + route, etag='"synthetic-v1"')
                    self.assertEqual(
                        (result.status, result.body, result.response_sha256),
                        (304, None, None),
                    )
            for route, tag in [("/bare304", None), ("/different304", '"synthetic-v1"')]:
                with self.assertRaises(source.TypedFetchError) as failure:
                    source.fetch_policy(origin + route, etag=tag)
                self.assertEqual(failure.exception.code, "etag")
                self.assertEqual(failure.exception.status, 304)

    def test_actual_bad_responses_fail_without_returning_body_or_retrying(self):
        cases = {
            "/utf8": "utf8",
            "/json": "json",
            "/nonfinite": "json",
            "/mime": "content_type",
            "/charset": "content_type",
            "/coding": "content_encoding",
            "/truncated": "framing",
            "/duplicate-length": "framing",
            "/close-delimited": "framing",
            "/mixed-framing": "framing",
            "/incomplete-chunk": "framing",
            "/bad-etag": "etag",
            "/not-found": "http_status",
        }
        with publisher() as (server, origin):
            for route, code in cases.items():
                with self.subTest(route=route):
                    before = len(server.calls)
                    with self.assertRaises(source.TypedFetchError) as failure:
                        source.fetch_policy(origin + route)
                    self.assertEqual(failure.exception.code, code)
                    self.assertEqual(len(server.calls), before + 1)
                    self.assertNotIn(
                        "synthetic-private-publisher-detail", str(failure.exception)
                    )
                    self.assertNotIn(origin, str(failure.exception))
                    self.assertFalse(hasattr(failure.exception, "body"))

    def test_transfer_coding_is_refused_and_content_length_size_bound(self):
        with publisher() as (_server, origin):
            for route in ("/chunked", "/large-chunk"):
                with self.assertRaises(source.TypedFetchError) as failure:
                    source.fetch_policy(origin + route, max_bytes=32)
                self.assertEqual(failure.exception.code, "framing")
            with self.assertRaises(source.TypedFetchError) as failure:
                source.fetch_policy(origin + "/large", max_bytes=32)
            self.assertEqual(failure.exception.code, "too_large")

    def test_parent_refuses_worker_reply_without_matching_content_length(self):
        # Pure IPC boundary fault injection; actual network tests remain above.
        class FinishedWorker:
            returncode = 0

            def communicate(self, *_args, **_kwargs):
                return json.dumps(self.reply).encode("ascii"), b""

            def poll(self):
                return 0

        for declared in (None, 3, True):
            worker = FinishedWorker()
            worker.reply = {
                "ok": True,
                "status": 200,
                "body": base64.b64encode(b"{}").decode(),
                "etag": None,
                "response_sha256": hashlib.sha256(b"{}").hexdigest(),
                "content_length": declared,
            }
            with (
                patch.object(source.subprocess, "Popen", return_value=worker),
                self.assertRaises(source.TypedFetchError) as failure,
            ):
                source.fetch_policy("http://127.0.0.1/synthetic.json")
            self.assertEqual(failure.exception.code, "worker")

    def test_redirect_never_follows_location(self):
        with publisher() as (server, origin):
            with self.assertRaises(source.TypedFetchError) as failure:
                source.fetch_policy(origin + "/redirect")
            self.assertEqual(
                (failure.exception.code, failure.exception.status), ("redirect", 302)
            )
            self.assertEqual([c["path"] for c in server.calls], ["/redirect"])

    def test_proxy_credentials_and_python_environment_not_inherited(self):
        real_popen = subprocess.Popen
        children = []

        def capture(*args, **kwargs):
            self.assertEqual(set(kwargs["env"]) - {"SystemRoot"}, set())
            self.assertTrue(Path(args[0][-2]).is_absolute())
            self.assertEqual(args[0][1:4], ["-I", "-S", "-B"])
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        with (
            publisher() as (trap, proxy),
            publisher() as (server, origin),
            patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": proxy,
                    "HTTPS_PROXY": proxy,
                    "ALL_PROXY": proxy,
                    "NO_PROXY": "",
                    "SYNTHETIC_CREDENTIAL_MARKER": "synthetic-not-a-real-credential",
                    "PYTHONPATH": "/synthetic-untrusted",
                },
            ),
            patch.object(source.subprocess, "Popen", side_effect=capture),
        ):
            self.assertEqual(source.fetch_policy(origin + "/publisher").status, 200)
            self.assertEqual(trap.calls, [])
            self.assertEqual(len(server.calls), 1)
            headers = {k.lower() for k in server.calls[0]["headers"]}
            self.assertFalse(
                headers & {"authorization", "proxy-authorization", "cookie"}
            )
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())

    def test_whole_deadline_covers_timeout_and_trickle_and_reaps_only_own_worker(self):
        real_popen = subprocess.Popen
        with publisher() as (_server, origin):
            for route in ("/timeout", "/slow-headers", "/slow-body"):
                children = []

                def capture(*args, **kwargs):
                    child = real_popen(*args, **kwargs)
                    children.append(child)
                    return child

                started = time.monotonic()
                with (
                    patch.object(source.subprocess, "Popen", side_effect=capture),
                    self.assertRaises(source.TypedFetchError) as failure,
                ):
                    source.fetch_policy(origin + route, timeout=0.4)
                elapsed = time.monotonic() - started
                self.assertEqual(failure.exception.code, "timeout", route)
                self.assertLess(elapsed, 1.15, route)
                self.assertGreaterEqual(failure.exception.elapsed_ms, 350, route)
                self.assertLess(failure.exception.elapsed_ms, 1150, route)
                self.assertEqual(len(children), 1)
                self.assertIsNotNone(children[0].poll())
                self.assertIsNotNone(children[0].wait(timeout=0))

    def test_url_and_limit_failures_do_not_spawn_a_worker(self):
        accepted = [
            "http://127.0.0.1:1/synthetic.json",
            "http://127.9.8.7/a%20b.json",
            "http://[::1]/synthetic.json",
            "https://example.com/policy.json",
        ]
        for url in accepted:
            self.assertEqual(source.validate_source_url(url), url)
        bad = [
            "http://localhost/x",
            "http://127.1/x",
            "http://192.0.2.1/x",
            "https://user:synthetic@example.com/x",
            "http://127.0.0.1/x?",
            "http://127.0.0.1/x#",
            "http://127.0.0.1:0/x",
            "http://127.0.0.1:/x",
            "http://127.0.0.1/\r\nX",
            "http://127.0.0.1/привет",
            "http://127.0.0.1/%ZZ",
            "file:///tmp/x",
            "https://[::1",
            "http://127.0.0.1\\@example.com/",
        ]
        with patch.object(
            source.subprocess,
            "Popen",
            side_effect=AssertionError("Invalid input spawned a worker"),
        ):
            for url in bad:
                with self.subTest(url=url), self.assertRaises(source.TypedFetchError):
                    source.fetch_policy(url)
            for config in [
                {"timeout": float("nan")},
                {"timeout": True},
                {"timeout": 10**1000},
                {"timeout": 0},
                {"max_bytes": True},
                {"max_bytes": 262145},
                {"etag": '"x"\r\nInjected: 1'},
            ]:
                with (
                    self.subTest(config=config),
                    self.assertRaises(source.TypedFetchError),
                ):
                    source.fetch_policy("http://127.0.0.1/", **config)

    def test_self_signed_https_is_refused_without_verification_bypass(self):
        openssl = shutil.which("openssl")
        if not openssl and os.name == "nt":
            candidate = (
                Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                / "Git/usr/bin/openssl.exe"
            )
            openssl = str(candidate) if candidate.is_file() else None
        if not openssl:
            self.skipTest(
                "OpenSSL executable unavailable for the synthetic self-signed TLS fixture"
            )
        with tempfile.TemporaryDirectory(prefix="citeafter-http-tls-") as owned:
            directory = Path(owned).resolve()
            self.assertEqual(directory.parent, Path(tempfile.gettempdir()).resolve())
            cert, key = directory / "cert.pem", directory / "key.pem"
            environment = (
                {"SystemRoot": os.environ["SystemRoot"]}
                if os.name == "nt" and "SystemRoot" in os.environ
                else {}
            )
            subprocess.run(
                [
                    openssl,
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "1",
                    "-subj",
                    "/CN=localhost",
                    "-addext",
                    "subjectAltName=DNS:localhost,IP:127.0.0.1",
                    "-keyout",
                    str(key),
                    "-out",
                    str(cert),
                ],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            with (
                publisher(context) as (_server, origin),
                self.assertRaises(source.TypedFetchError) as failure,
            ):
                source.fetch_policy(origin + "/publisher")
            self.assertEqual(failure.exception.code, "tls")


if __name__ == "__main__":
    unittest.main(verbosity=2)
