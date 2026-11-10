"""Engine transport integration against a separately launched HTTP publisher."""

from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from citeafter.engine import Engine
from citeafter.policy import CiteafterError, digest

ROOT = Path(__file__).resolve().parents[1]
POLICIES = ROOT / "citeafter/examples/policies"
FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# The publisher owns its socket and serves the exact supplied bytes. Its control
# channel is distinct from /policy; no transport or engine function is replaced.
PUBLISHER = r"""
from base64 import b64decode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json, threading
lock = threading.Lock()
release = threading.Event()
rules, requests = [], []
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def reply(self, status, body=b'', etag=None):
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            if etag is not None: self.send_header('ETag', etag)
            self.end_headers()
            if status != 304: self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # The timeout test intentionally cancels its HTTP worker.
    def do_POST(self):
        global rules
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/configure':
            with lock:
                rules = data['rules']
                release.clear()
            self.reply(200, b'{}')
        elif self.path == '/release':
            release.set(); self.reply(200, b'{}')
        elif self.path == '/stop':
            release.set(); self.reply(200, b'{}')
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else: self.reply(404, b'{}')
    def do_GET(self):
        if self.path == '/requests':
            with lock: body = json.dumps(requests).encode()
            self.reply(200, body); return
        if self.path != '/policy': self.reply(404, b'{}'); return
        with lock:
            rule = dict(rules.pop(0) if len(rules) > 1 else rules[0])
            requests.append({'if_none_match': self.headers.get('If-None-Match'),
                             'held': bool(rule.get('hold')), 'status': rule['status']})
        if rule.get('hold') and not release.wait(12): return
        self.reply(rule['status'], b64decode(rule.get('body', '')), rule.get('etag'))
server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
server.daemon_threads = True
print(json.dumps({'port': server.server_port}), flush=True)
server.serve_forever(poll_interval=0.05)
server.server_close()
"""


class Clock:
    def __init__(self, now="2030-01-03T09:00:00Z"):
        self.now = now

    def __call__(self):
        return self.now


@contextmanager
def database(path):
    connection = sqlite3.connect(path)
    try:
        yield connection
    finally:
        connection.close()


class Publisher:
    def __enter__(self):
        self.process = subprocess.Popen(
            [sys.executable, "-B", "-u", "-c", PUBLISHER],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=FLAGS,
        )
        started = queue.Queue()
        threading.Thread(target=lambda: started.put(self.process.stdout.readline()), daemon=True).start()
        try:
            self.port = json.loads(started.get(timeout=8))["port"]
        except BaseException:
            self.process.kill()
            self.process.communicate()
            raise
        self.url = f"http://127.0.0.1:{self.port}/policy"
        return self

    def control(self, path, data=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            body = json.dumps(data or {}).encode() if data is not None else None
            connection.request(
                "POST" if data is not None else "GET",
                path,
                body,
                {"Content-Type": "application/json"} if body else {},
            )
            response = connection.getresponse()
            result = response.read()
            if response.status != 200:
                raise AssertionError((response.status, result))
            return json.loads(result)
        finally:
            connection.close()

    def configure(self, *rules):
        self.control("/configure", {"rules": rules})

    def wait_for_held_request(self, after):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            requests = self.control("/requests")
            if len(requests) > after and requests[after]["held"]:
                return requests[after]
            time.sleep(0.01)
        raise AssertionError("Publisher did not receive the held request")

    def __exit__(self, *unused):
        try:
            self.control("/stop", {})
            output, errors = self.process.communicate(timeout=5)
            if self.process.returncode or errors:
                raise AssertionError((self.process.returncode, output, errors))
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.communicate()


def rule(status=200, *, version="v1", body=None, etag='"v1"', hold=False):
    raw = (POLICIES / (version + ".json")).read_bytes() if body is None else body
    return {"status": status, "body": b64encode(raw).decode("ascii"), "etag": etag, "hold": hold}


class EngineHTTPTests(unittest.TestCase):
    def setUp(self):
        self.owned = tempfile.TemporaryDirectory(prefix="citeafter-http-")
        self.addCleanup(self.owned.cleanup)
        self.publisher = self.enterContext(Publisher())
        self.clock = Clock()
        self.path = Path(self.owned.name) / "ledger.db"
        self.engine = Engine(self.path, clock=self.clock)
        self.engine.initialize(source_url=self.publisher.url, ttl_seconds=60, mode="synthetic-local")
        self.publisher.configure(rule())

    def create(self, action_id="draft", shipping=False):
        return self.engine.create_action(
            action_id,
            label="Synthetic draft",
            business_at="2030-01-08T12:00:00Z",
            recipe="shipping_days" if shipping else "discount_quote",
            clause="shipping" if shipping else "standard",
            inputs={"destination": "domestic"} if shipping else {"amount": "2000.00", "currency": "USD"},
        )

    def test_200_304_changed_200_preserve_first_observation_and_exact_release_support(self):
        first = self.engine.refresh()
        self.assertTrue(first["accepted"])
        self.clock.now = "2030-01-04T12:00:00Z"
        self.publisher.configure(rule(304))
        self.assertTrue(self.engine.refresh()["not_modified"])
        original = self.create()
        self.create("shipping", shipping=True)
        old = self.engine.preview("shipping")
        self.clock.now = "2030-01-04T15:00:00Z"
        self.publisher.configure(rule(version="v2", etag='"v2"'))
        changed = self.engine.refresh()
        self.assertTrue(changed["accepted"])
        self.assertEqual(changed["recipe_executions"], 0)
        with self.assertRaises(CiteafterError) as refused:
            self.engine.release(old["preview_id"])
        self.assertEqual(refused.exception.code, "stale_preview")
        state = self.engine.state()
        self.assertEqual([item["version_id"] for item in state["sources"]], ["v1", "v2"])
        self.assertEqual(state["sources"][0]["observed_at"], "2030-01-03T09:00:00.000000Z")
        self.assertEqual(state["health"]["generation"], 3)
        self.assertEqual(original["claim"]["result"]["output"]["net_amount"], "1800.00")
        shipping = self.engine.preview("shipping")
        self.assertEqual(shipping["payload"]["citation_source_version"], "v1")
        self.assertEqual(shipping["source_version"], "v2")
        receipt = self.engine.release(shipping["preview_id"])
        updated = self.engine.recheck("draft")
        self.assertEqual(updated["claim"]["result"]["output"]["net_amount"], "1840.00")
        reopened = Engine(self.path, clock=self.clock)
        self.assertEqual(reopened.release(shipping["preview_id"]), receipt)
        self.clock.now = "2030-01-04T15:00:01Z"
        duplicate = reopened.refresh()
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["observed_at"], "2030-01-04T15:00:00.000000Z")
        self.assertEqual(len(reopened.state()["sources"]), 2)
        requests = self.publisher.control("/requests")
        self.assertEqual([r["if_none_match"] for r in requests], [None, '"v1"', '"v1"', '"v2"'])

    def test_http_failure_and_reopen_never_approve_cached_source_then_fresh_check_recovers(self):
        self.engine.refresh()
        self.clock.now = "2030-01-04T12:00:00Z"
        self.engine.refresh()
        self.create()
        for response, expected in [
            (rule(404), "http_status"),
            (rule(body=b'{"broken":'), "json"),
            (rule(hold=True), "timeout"),
        ]:
            with self.subTest(error=expected):
                preview = self.engine.preview("draft")
                self.publisher.configure(response)
                failed = self.engine.refresh(timeout=0.5 if expected == "timeout" else 2)
                self.assertFalse(failed["accepted"])
                self.assertEqual(failed["error"]["code"], expected)
                self.publisher.control("/release", {})
                reopened = Engine(self.path, clock=self.clock)
                state = reopened.state()
                self.assertEqual(state["health"]["status"], "error")
                self.assertEqual(state["health"]["reason"], expected)
                self.assertEqual(len(state["sources"]), 1)
                with self.assertRaises(CiteafterError) as refused:
                    reopened.release(preview["preview_id"])
                self.assertEqual(refused.exception.code, "stale_preview")
                with self.assertRaises(CiteafterError) as refused:
                    reopened.preview("draft")
                self.assertEqual(refused.exception.code, "source_unavailable")
                self.publisher.configure(rule(304))
                self.assertTrue(reopened.refresh()["not_modified"])
                self.assertEqual(reopened.state()["health"]["status"], "fresh")
        receipt = self.engine.release(self.engine.preview("draft")["preview_id"])
        self.publisher.configure(rule(404))
        self.assertFalse(self.engine.refresh()["accepted"])
        self.assertEqual(Engine(self.path, clock=self.clock).release(receipt["preview_id"]), receipt)
        with database(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM outbox").fetchone()[0], 1)

    def test_schema_failure_retains_received_bytes_without_replacing_cache(self):
        self.engine.refresh()
        invalid = b'{"schema_version":1,"observed_at":"1900-01-01T00:00:00Z"}'
        self.publisher.configure(rule(body=invalid, etag='"invalid"'))
        failed = self.engine.refresh()
        self.assertFalse(failed["accepted"])
        with database(self.path) as connection:
            received, received_sha = connection.execute(
                "SELECT received,received_sha FROM checks ORDER BY id DESC LIMIT 1"
            ).fetchone()
            cached_sha, etag = connection.execute("SELECT source_sha,etag FROM cache").fetchone()
        self.assertEqual((received, received_sha), (invalid, digest(invalid)))
        self.assertEqual(cached_sha, digest((POLICIES / "v1.json").read_bytes()))
        self.assertEqual(etag, '"v1"')
        self.assertEqual(self.engine.state()["health"]["status"], "error")

    def test_304_without_a_cached_conditional_representation_is_not_fresh(self):
        self.publisher.configure(rule(304))
        failed = self.engine.refresh()
        self.assertEqual(failed["error"]["code"], "etag")
        state = self.engine.state()
        self.assertEqual(state["sources"], [])
        self.assertEqual(state["health"]["status"], "error")

    def test_held_304_cannot_validate_a_cache_replaced_during_the_http_request(self):
        self.engine.refresh()
        self.clock.now = "2030-01-04T15:00:00Z"
        before = len(self.publisher.control("/requests"))
        self.publisher.configure(rule(304, hold=True), rule(version="v2", etag='"v2"'))
        with ThreadPoolExecutor(max_workers=1) as executor:
            late = executor.submit(self.engine.refresh, timeout=5)
            held = self.publisher.wait_for_held_request(before)
            self.assertEqual(held["if_none_match"], '"v1"')
            self.assertTrue(self.engine.refresh()["accepted"])
            self.publisher.control("/release", {})
            refused = late.result(timeout=6)
        self.assertEqual(refused["error"]["code"], "conditional_cache_changed")
        state = Engine(self.path, clock=self.clock).state()
        self.assertEqual(state["health"]["status"], "error")
        self.assertEqual([item["version_id"] for item in state["sources"]], ["v1", "v2"])
        with database(self.path) as connection:
            self.assertEqual(connection.execute("SELECT etag FROM cache").fetchone()[0], '"v2"')
        self.publisher.configure(rule(304, etag='"v2"'))
        self.assertTrue(self.engine.refresh()["not_modified"])
        self.assertEqual(self.engine.state()["health"]["status"], "fresh")


if __name__ == "__main__":
    unittest.main()
