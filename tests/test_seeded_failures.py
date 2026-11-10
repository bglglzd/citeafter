"""Deliberately broken alternatives verify that domain assertions catch real errors."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from citeafter.demo import Demo
from citeafter.policy import CiteafterError
from citeafter.store import pack, unpack


class SeedDetection(unittest.TestCase):
    def test_newest_observed_selection_is_detected_by_a_friday_historical_query(self):
        with tempfile.TemporaryDirectory(prefix="citeafter-seed-time-") as owned:
            demo = Demo(Path(owned) / "demo.sqlite")
            demo.advance("v2")
            demo.advance("v3")

            def assert_friday_source():
                result = demo.engine.as_of(
                    "D02", valid_at="2030-01-04T12:00:00Z", known_at="2030-01-08T09:00:00Z"
                )
                self.assertEqual(result["selected_source_version"], "v1")

            assert_friday_source()

            def newest_observed(records, valid_at, known_at):
                return {
                    "status": "selected",
                    "reason": "seeded_latest_only_bug",
                    "source": max(records, key=lambda row: row["observed_at"]),
                }

            with patch("citeafter.engine.select_source", newest_observed):
                with self.assertRaises(AssertionError):
                    assert_friday_source()
            assert_friday_source()

    def test_missing_final_support_validation_is_detected_by_an_actual_stale_outbox_write(self):
        with tempfile.TemporaryDirectory(prefix="citeafter-seed-preview-") as owned:
            demo = Demo(Path(owned) / "demo.sqlite")
            preview = demo.engine.preview("D02")
            demo.advance("v2")

            def assert_no_stale_write():
                try:
                    demo.engine.release(preview["preview_id"])
                except CiteafterError as error:
                    self.assertEqual(error.code, "stale_preview")
                with demo.engine.store.transaction() as connection:
                    self.assertEqual(
                        connection.execute("SELECT count(*) FROM outbox WHERE action_id='D02'").fetchone()[0],
                        0,
                    )

            assert_no_stale_write()

            def unchecked_release(preview_id):
                # Test-only regression: dispatch the old preview without current support/freshness checks.
                with demo.engine.store.transaction(True) as connection:
                    row = connection.execute("SELECT * FROM previews WHERE id=?", (preview_id,)).fetchone()
                    saved = unpack(row)
                    receipt = {
                        "id": "outbox-seeded-bug",
                        "action_id": saved["action_id"],
                        "preview_id": preview_id,
                        "payload": saved["payload"],
                        "scope": "test_only_broken_local_outbox",
                    }
                    connection.execute(
                        "INSERT INTO outbox VALUES (?,?,?,?,?)",
                        (receipt["id"], receipt["action_id"], preview_id, *pack(receipt)),
                    )
                    return receipt

            with patch.object(demo.engine, "release", unchecked_release):
                with self.assertRaises(AssertionError):
                    assert_no_stale_write()


if __name__ == "__main__":
    unittest.main()
