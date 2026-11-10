"""Independent-process importer/release orderings over the actual SQLite ledger."""

from contextlib import contextmanager
import json
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from citeafter.engine import Engine
from citeafter.policy import CiteafterError

ROOT = Path(__file__).resolve().parents[1]
POLICIES = ROOT / "citeafter/examples/policies"
FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
NOW = "2030-01-04T15:00:00Z"

WORKER = r"""
import json,os,sys
from pathlib import Path
from citeafter.engine import Engine
from citeafter.policy import CiteafterError
engine = Engine(sys.argv[1], clock=lambda: '2030-01-04T15:00:00Z')
print(json.dumps({'ready': True, 'pid': os.getpid()}), flush=True)
for line in sys.stdin:
    command = json.loads(line)
    operation = command['operation']
    print(json.dumps({'started': operation}), flush=True)
    try:
        if operation == 'ingest': result = engine.ingest(Path(command['path']).read_bytes())
        elif operation == 'release': result = engine.release(command['preview_id'])
        elif operation == 'paused_release':
            original = engine._payload
            def paused(*args):
                print(json.dumps({'write_transaction_open': True}), flush=True)
                if sys.stdin.readline().strip() != 'continue': raise RuntimeError('Missing barrier release')
                return original(*args)
            engine._payload = paused
            result = engine.release(command['preview_id'])
        else: raise RuntimeError('Unknown test operation')
        print(json.dumps({'ok': True, 'result': result}), flush=True)
    except CiteafterError as error:
        print(json.dumps({'ok': False, 'error': error.code}), flush=True)
"""


class Worker:
    def __init__(self, path):
        self.process = subprocess.Popen(
            [sys.executable, "-B", "-u", "-c", WORKER, str(path)],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=FLAGS,
        )
        self.messages = queue.Queue()

        def read():
            for line in self.process.stdout:
                self.messages.put(json.loads(line))

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        ready = self.receive()
        if not ready.get("ready"):
            raise AssertionError(ready)
        self.pid = ready["pid"]

    def receive(self):
        return self.messages.get(timeout=12)

    def start(self, operation, **arguments):
        self.send(json.dumps({"operation": operation, **arguments}))
        started = self.receive()
        if started != {"started": operation}:
            raise AssertionError(started)

    def send(self, value):
        self.process.stdin.write((value + "\n").encode())
        self.process.stdin.flush()

    def close(self):
        try:
            if self.process.poll() is None:
                self.process.stdin.close()
                self.process.wait(timeout=8)
            self.reader.join(timeout=2)
            errors = self.process.stderr.read()
            if self.process.returncode or errors:
                raise AssertionError((self.process.returncode, errors))
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            self.process.stdout.close()
            self.process.stderr.close()


@contextmanager
def database(path, **options):
    connection = sqlite3.connect(path, **options)
    try:
        yield connection
    finally:
        connection.close()


class EngineRaceTests(unittest.TestCase):
    def setUp(self):
        self.owned = tempfile.TemporaryDirectory(prefix="citeafter-process-race-")
        self.addCleanup(self.owned.cleanup)
        self.path = Path(self.owned.name) / "ledger.db"
        self.engine = Engine(self.path, clock=lambda: NOW)
        self.engine.initialize(mode="synthetic-local")
        self.engine.ingest((POLICIES / "v1.json").read_bytes())

    def worker(self):
        worker = Worker(self.path)
        self.addCleanup(worker.close)
        return worker

    def create(self, action_id, shipping=False):
        return self.engine.create_action(
            action_id,
            label="Synthetic process race",
            business_at="2030-01-08T12:00:00Z",
            recipe="shipping_days" if shipping else "discount_quote",
            clause="shipping" if shipping else "standard",
            inputs={"destination": "domestic"} if shipping else {"amount": "2000.00", "currency": "USD"},
        )

    def test_importer_commits_before_release_old_token_refuses_and_fresh_unaffected_token_works(self):
        self.create("discount")
        self.create("shipping", shipping=True)
        discount = self.engine.preview("discount")
        shipping = self.engine.preview("shipping")
        importer, releaser = self.worker(), self.worker()
        self.assertNotEqual(importer.pid, releaser.pid)
        importer.start("ingest", path=str(POLICIES / "v2.json"))
        self.assertTrue(importer.receive()["result"]["accepted"])
        for preview in (discount, shipping):
            releaser.start("release", preview_id=preview["preview_id"])
            self.assertEqual(releaser.receive(), {"ok": False, "error": "stale_preview"})
        with database(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)
        with patch(
            "citeafter.engine.evaluate_recipe", side_effect=AssertionError("Unexpected recipe execution")
        ):
            fresh = self.engine.preview("shipping")
        self.assertEqual(fresh["payload"]["citation_source_version"], "v1")
        self.assertEqual(fresh["source_version"], "v2")
        releaser.start("release", preview_id=fresh["preview_id"])
        receipt = releaser.receive()["result"]
        self.assertEqual(receipt["payload"], fresh["payload"])
        with self.assertRaises(CiteafterError) as refused:
            self.engine.preview("discount")
        self.assertEqual(refused.exception.code, "stale")
        self.assertEqual(Engine(self.path, clock=lambda: NOW).state()["actions"][1]["receipt"], receipt)

    def test_release_holds_write_lock_importer_waits_then_original_receipt_remains_immutable(self):
        self.create("draft")
        preview = self.engine.preview("draft")
        releaser, importer = self.worker(), self.worker()
        releaser.start("paused_release", preview_id=preview["preview_id"])
        self.assertEqual(releaser.receive(), {"write_transaction_open": True})
        try:
            # This is an actual second OS process; only the release's payload
            # comparison is paused, after it has acquired BEGIN IMMEDIATE.
            importer.start("ingest", path=str(POLICIES / "v2.json"))
            with database(self.path, timeout=0) as connection:
                with self.assertRaises(sqlite3.OperationalError) as blocked:
                    connection.execute("BEGIN IMMEDIATE")
                self.assertIn("locked", str(blocked.exception))
            self.assertTrue(importer.messages.empty())
        finally:
            releaser.send("continue")
        receipt = releaser.receive()["result"]
        self.assertTrue(importer.receive()["result"]["accepted"])
        self.assertEqual(receipt["source_version"], "v1")
        self.assertEqual(receipt["payload"]["output"]["net_amount"], "1800.00")
        reopened = Engine(self.path, clock=lambda: NOW)
        self.assertEqual(reopened.release(preview["preview_id"]), receipt)
        self.assertEqual(reopened.state()["actions"][0]["assessment"]["gate"], "released")
        self.assertEqual(reopened.history("draft")["receipt"], receipt)
        with database(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM outbox").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM sources").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
