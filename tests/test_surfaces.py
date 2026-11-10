import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from citeafter.demo import Demo, export_recording
from citeafter.policy import CiteafterError
from citeafter.server import make_server


class Surfaces(unittest.TestCase):
    def setUp(self):
        self.owned = tempfile.TemporaryDirectory(prefix="citeafter-surfaces-")
        self.root = Path(self.owned.name)

    def tearDown(self):
        self.owned.cleanup()

    def test_demo_real_engine_stale_manual_recheck_release_and_restart(self):
        demo = Demo(self.root / "demo.sqlite")
        self.assertEqual(len(demo.engine.state()["actions"]), 6)
        first = next(row for row in demo.engine.state()["actions"] if row["id"] == "D01")["receipt"]
        demo.advance("v2")
        actions = {row["id"]: row for row in demo.engine.state()["actions"]}
        self.assertEqual(
            [actions[name]["assessment"]["gate"] for name in ("D01", "D02", "D03", "D12", "D14", "D16")],
            ["released", "stale", "stale", "allowed", "needs_review", "needs_review"],
        )
        demo.engine.recheck("D02")
        receipt = demo.engine.release(demo.engine.preview("D02")["preview_id"])
        demo.advance("v3")
        reopened = Demo(self.root / "demo.sqlite")
        actions = {row["id"]: row for row in reopened.engine.state()["actions"]}
        self.assertEqual(actions["D03"]["assessment"]["gate"], "stale")
        self.assertEqual(actions["D01"]["receipt"], first)
        self.assertEqual(actions["D02"]["receipt"], receipt)
        self.assertEqual(actions["D12"]["claim"]["result"]["source_version"], "v1")

    def test_recording_is_engine_output_and_never_overwrites_existing_bytes(self):
        result = export_recording(self.root / "static")
        target = Path(result["path"])
        saved = target.read_bytes()
        recording = json.loads(saved)
        self.assertEqual(len(recording["stages"]), 5)
        self.assertEqual(len(recording["as_of"]), 36)
        self.assertTrue(all(row["result"]["read_only"] for row in recording["as_of"]))
        actions = {row["id"]: row for row in recording["stages"][-1]["state"]["actions"]}
        self.assertEqual(actions["D03"]["assessment"]["gate"], "stale")
        self.assertEqual(actions["D02"]["receipt"]["payload"]["output"]["net_amount"], "1840.00")
        with self.assertRaises(CiteafterError):
            export_recording(self.root / "static")
        self.assertEqual(target.read_bytes(), saved)

    def test_actual_local_api_origin_token_time_boundary_and_stale_preview(self):
        demo = Demo(self.root / "demo.sqlite")
        server = make_server(demo.engine, demo=demo)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        origin = "http://127.0.0.1:" + str(server.server_port)

        def request(method, path, data=None, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
            try:
                body = json.dumps(data).encode() if data is not None else None
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                raw = response.read()
                return response.status, json.loads(raw), dict(response.getheaders())
            finally:
                connection.close()

        try:
            status, state, headers = request("GET", "/api/state")
            self.assertEqual(status, 200)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertNotIn("Access-Control-Allow-Origin", headers)
            self.assertEqual(request("POST", "/api/preview", {"action_id": "D02"})[0], 403)
            secure = {
                "Content-Type": "application/json",
                "X-Citeafter-Token": state["csrf_token"],
                "Origin": origin,
            }
            hostile = {**secure, "Origin": "https://unrelated.invalid"}
            self.assertEqual(request("POST", "/api/preview", {"action_id": "D02"}, hostile)[0], 403)
            self.assertEqual(request("GET", "/api/state", headers={"Host": "hostile.invalid"})[0], 403)
            status, preview, _ = request("POST", "/api/preview", {"action_id": "D02"}, secure)
            self.assertEqual(status, 200)
            self.assertEqual(request("POST", "/api/demo/advance", {"version": "v2"}, secure)[0], 200)
            self.assertEqual(
                request("POST", "/api/release", {"preview_id": preview["preview_id"]}, secure)[0], 409
            )
            before = demo.path.read_bytes()
            history = request(
                "GET",
                "/api/as-of?action_id=D02&valid_at=2030-01-08T12%3A00%3A00Z&known_at=2030-01-04T12%3A00%3A00Z",
            )
            self.assertEqual(history[1]["selected_source_version"], "v1")
            self.assertEqual(demo.path.read_bytes(), before)
            self.assertEqual(
                request(
                    "POST", "/api/recheck", {"action_id": "D02", "known_at": "2030-01-04T12:00:00Z"}, secure
                )[0],
                400,
            )
            self.assertEqual(request("POST", "/api/recheck", {"action_id": "D02"}, secure)[0], 200)
            status, current, _ = request("POST", "/api/preview", {"action_id": "D02"}, secure)
            self.assertEqual(status, 200)
            self.assertEqual(
                request("POST", "/api/release", {"preview_id": current["preview_id"]}, secure)[0], 200
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
