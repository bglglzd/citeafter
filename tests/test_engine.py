import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from citeafter.engine import Engine
from citeafter.policy import CiteafterError
from citeafter.store import SCHEMA

EXAMPLES = Path(__file__).resolve().parents[1] / "citeafter/examples/policies"
ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def database(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class Clock:
    def __init__(self, now="2030-01-03T09:00:00Z"):
        self.now = now

    def __call__(self):
        return self.now


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.owned = tempfile.TemporaryDirectory(prefix="citeafter-engine-")
        self.path = Path(self.owned.name) / "ledger.db"
        self.clock = Clock()
        self.engine = Engine(self.path, clock=self.clock)
        self.engine.initialize(mode="synthetic-local")
        self.engine.ingest(self.raw("v1"))

    def tearDown(self):
        self.owned.cleanup()

    def raw(self, version):
        return (EXAMPLES / (version + ".json")).read_bytes()

    def action(self, action_id="draft", **changes):
        self.clock.now = "2030-01-04T12:00:00Z"
        settings = dict(
            label="Tuesday discount",
            business_at="2030-01-08T12:00:00Z",
            recipe="discount_quote",
            clause="standard",
            inputs={"amount": "2000.00", "currency": "USD"},
        )
        settings.update(changes)
        self.engine.ingest(self.raw("v1"))
        return self.engine.create_action(action_id, **settings)

    def advance(self, version="v2"):
        self.clock.now = {"v2": "2030-01-04T15:00:00Z", "v3": "2030-01-08T09:00:00Z"}[version]
        return self.engine.ingest(self.raw(version))

    def test_changed_support_stops_release_then_recheck_appends_and_releases_exact_payload(self):
        original = self.action()
        preview = self.engine.preview("draft")
        update = self.advance()
        self.assertEqual(update["recipe_executions"], 0)
        self.assertEqual(update["decisions_inspected"], 1)
        with self.assertRaises(CiteafterError) as refused:
            self.engine.release(preview["preview_id"])
        self.assertEqual(refused.exception.code, "stale_preview")
        with self.assertRaises(CiteafterError):
            self.engine.preview("draft")
        new = self.engine.recheck("draft")
        self.assertEqual(new["revision"], 2)
        self.assertEqual(new["claim"]["result"]["output"]["net_amount"], "1840.00")
        self.assertEqual(original["claim"]["result"]["output"]["net_amount"], "1800.00")
        accepted = self.engine.preview("draft")
        receipt = self.engine.release(accepted["preview_id"])
        self.assertEqual(receipt["payload"], accepted["payload"])
        self.assertEqual(len(self.engine.history("draft")["claims"]), 2)
        self.assertEqual(receipt["scope"], "local_sqlite_outbox_only")

    def test_cosmetic_update_preserves_stale_old_claim_and_equivalence_keeps_original_citations(self):
        self.action()
        self.advance()
        self.advance("v3")
        self.assertEqual(self.engine.state()["actions"][0]["assessment"]["gate"], "stale")
        self.engine.recheck("draft")
        self.assertEqual(self.engine.state()["actions"][0]["claim"]["result"]["source_version"], "v3")

    def test_unrelated_shipping_needs_new_preview_without_recipe_execution(self):
        self.action(recipe="shipping_days", clause="shipping", inputs={"destination": "domestic"})
        old = self.engine.preview("draft")
        with patch(
            "citeafter.engine.evaluate_recipe", side_effect=AssertionError("Unexpected recipe execution")
        ):
            self.advance()
            state = self.engine.state()["actions"][0]
            self.assertEqual(state["assessment"]["gate"], "allowed")
            self.assertEqual(state["claim"]["result"]["citations"][0]["source_version"], "v1")
            with self.assertRaises(CiteafterError):
                self.engine.release(old["preview_id"])
            new = self.engine.preview("draft")
            self.assertEqual(new["payload"]["citation_source_version"], "v1")
            self.assertEqual(new["source_version"], "v2")
            self.engine.release(new["preview_id"])

    def test_manual_and_normative_text_remain_review_not_successful_rechecks(self):
        self.action(
            recipe="manual_claim",
            clause="manual_note",
            inputs={"text": "Review"},
            declared_paths=["/clauses/shipping/days"],
        )
        self.advance()
        action = self.engine.state()["actions"][0]
        self.assertEqual(action["assessment"]["impact_class"], "needs_review")
        self.assertEqual(action["assessment"]["gate"], "needs_review")
        with self.assertRaises(CiteafterError):
            self.engine.recheck("draft")
        with self.assertRaises(CiteafterError):
            self.engine.preview("draft")

    def test_reopen_failure_expiry_and_original_receipt_replay_do_not_rewrite_history(self):
        self.action(business_at="2030-01-04T12:00:00Z")
        preview = self.engine.preview("draft")
        first = self.engine.release(preview["preview_id"])
        failed = self.engine.ingest(b'{"invalid":true}')
        self.assertFalse(failed["accepted"])
        reopened = Engine(self.path, clock=self.clock)
        self.assertEqual(reopened.state()["health"]["status"], "error")
        self.assertEqual(reopened.release(preview["preview_id"]), first)
        self.clock.now = "2030-01-05T12:00:00Z"
        self.assertEqual(reopened.release(preview["preview_id"]), first)
        self.assertEqual(len(reopened.history("draft")["claims"]), 1)

    def test_expired_source_and_preview_block_pending_release_and_recover(self):
        self.action()
        preview = self.engine.preview("draft")
        self.clock.now = "2030-01-04T12:01:01Z"
        self.assertEqual(self.engine.state()["health"]["status"], "expired")
        with self.assertRaises(CiteafterError):
            self.engine.release(preview["preview_id"])
        with self.assertRaises(CiteafterError):
            self.engine.preview("draft")
        self.engine.ingest(self.raw("v1"))
        self.assertEqual(self.engine.state()["sources"][0]["observed_at"], "2030-01-03T09:00:00.000000Z")
        self.engine.release(self.engine.preview("draft")["preview_id"])

    def test_history_query_never_changes_action_or_operational_authority(self):
        self.action()
        self.advance()
        before = self.path.read_bytes()
        historical = self.engine.as_of(
            "draft", valid_at="2030-01-08T12:00:00Z", known_at="2030-01-04T14:59:59Z"
        )
        self.assertEqual(historical["selected_source_version"], "v1")
        self.assertEqual(historical["output"]["net_amount"], "1800.00")
        self.assertTrue(historical["read_only"])
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(self.engine.state()["actions"][0]["assessment"]["gate"], "stale")

    def test_conflicting_version_and_fork_fail_closed_not_last_arrival_wins(self):
        self.action()
        conflict = json.loads(self.raw("v1"))
        conflict["clauses"]["standard"]["rate_percent"] = "99.00"
        self.assertTrue(self.engine.ingest(json.dumps(conflict).encode())["accepted"])
        state = self.engine.state()
        self.assertEqual(len(state["sources"]), 2)
        self.assertEqual(state["actions"][0]["assessment"]["impact_class"], "needs_review")
        with self.assertRaises(CiteafterError):
            self.engine.preview("draft")

    def test_unrelated_database_is_preserved_and_corruption_fails_closed(self):
        unrelated = self.path.parent / "other.db"
        with database(unrelated) as connection:
            connection.execute("CREATE TABLE useful(x)")
        before = unrelated.read_bytes()
        with self.assertRaises(CiteafterError):
            Engine(unrelated).state()
        self.assertEqual(unrelated.read_bytes(), before)
        self.action()
        with database(self.path) as connection:
            connection.execute("DROP TRIGGER immutable_claims_update")
            connection.execute("UPDATE claims SET data='{}'")
            connection.execute(SCHEMA["immutable_claims_update"])
        before = self.path.read_bytes()
        with self.assertRaises(CiteafterError):
            self.engine.preview("draft")
        self.assertEqual(self.path.read_bytes(), before)

    def test_freshness_setting_corruption_cannot_turn_an_expired_source_into_permission(self):
        self.action()
        self.clock.now = "2030-01-04T12:30:00Z"
        self.assertEqual(self.engine.state()["health"]["status"], "expired")
        with database(self.path) as connection:
            connection.execute("UPDATE meta SET value='86400' WHERE key='ttl_seconds'")
        before = self.path.read_bytes()
        with self.assertRaises(CiteafterError) as failed:
            self.engine.preview("draft")
        self.assertEqual(failed.exception.code, "integrity")
        self.assertEqual(self.path.read_bytes(), before)
        with database(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)

    def test_observation_metadata_corruption_cannot_silently_change_history(self):
        self.action()
        with database(self.path) as connection:
            connection.execute("DROP TRIGGER immutable_sources_update")
            connection.execute("UPDATE sources SET observed_at='2029-01-01T00:00:00.000000Z'")
            connection.execute(SCHEMA["immutable_sources_update"])
        with self.assertRaises(CiteafterError) as failed:
            self.engine.as_of("draft", valid_at="2030-01-08T12:00:00Z", known_at="2030-01-02T12:00:00Z")
        self.assertEqual(failed.exception.code, "integrity")

    def test_check_time_metadata_corruption_cannot_freshen_an_old_source(self):
        self.action()
        with database(self.path) as connection:
            connection.execute("DROP TRIGGER immutable_checks_update")
            connection.execute("UPDATE checks SET checked_at='2030-01-04T12:30:00.000000Z'")
            connection.execute(SCHEMA["immutable_checks_update"])
        self.clock.now = "2030-01-04T12:30:00Z"
        with self.assertRaises(CiteafterError) as failed:
            self.engine.preview("draft")
        self.assertEqual(failed.exception.code, "integrity")

    def test_ledger_mutation_is_refused_and_clock_rollback_cannot_authorize(self):
        self.action()
        with database(self.path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM sources")
        self.clock.now = "2029-01-01T00:00:00Z"
        with self.assertRaises(CiteafterError):
            self.engine.preview("draft")
        self.assertIsNotNone(
            self.engine.as_of("draft", valid_at="2030-01-08T12:00:00Z", known_at="2030-01-04T12:00:00Z")
        )

    def test_claim_cannot_be_reassigned_to_another_action_by_metadata_corruption(self):
        self.action("first")
        self.action("second")
        self.advance()
        self.engine.recheck("first")
        with database(self.path) as connection:
            connection.execute("DROP TRIGGER immutable_claims_update")
            connection.execute("UPDATE claims SET action_id='second' WHERE action_id='first' AND revision=2")
            connection.execute(SCHEMA["immutable_claims_update"])
        before = self.path.read_bytes()
        with self.assertRaises(CiteafterError) as failed:
            self.engine.preview("second")
        self.assertEqual(failed.exception.code, "integrity")
        self.assertEqual(self.path.read_bytes(), before)
        with database(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)

    def test_corrupt_revision_cannot_hide_a_newer_claim_from_selection_or_history(self):
        self.action()
        self.advance()
        self.engine.recheck("draft")
        with database(self.path) as connection:
            connection.execute("DROP TRIGGER immutable_claims_update")
            connection.execute("UPDATE claims SET revision=0 WHERE revision=2")
            connection.execute(SCHEMA["immutable_claims_update"])
        before = self.path.read_bytes()
        for operation in (
            self.engine.state,
            lambda: self.engine.history("draft"),
            lambda: self.engine.preview("draft"),
        ):
            with self.assertRaises(CiteafterError) as failed:
                operation()
            self.assertEqual(failed.exception.code, "integrity")
        self.assertEqual(self.path.read_bytes(), before)

    def test_oversize_import_records_failure_and_blocks_until_successful_check(self):
        self.action()
        failed = self.engine.ingest(b" " * (256 * 1024 + 1))
        self.assertFalse(failed["accepted"])
        self.assertEqual(failed["error"]["code"], "too_large")
        self.assertEqual(self.engine.state()["health"]["status"], "error")
        with self.assertRaises(CiteafterError):
            self.engine.preview("draft")
        self.engine.ingest(self.raw("v1"))
        self.assertIsNotNone(self.engine.preview("draft"))

    def test_checked_old_representation_cannot_freshen_a_selected_newer_version(self):
        self.action()
        self.action("friday", business_at="2030-01-04T12:00:00Z")
        self.advance()
        self.engine.recheck("draft")
        self.engine.ingest(self.raw("v1"))
        actions = {row["id"]: row for row in self.engine.state()["actions"]}
        self.assertEqual(actions["draft"]["assessment"]["impact_class"], "unaffected")
        self.assertEqual(actions["draft"]["assessment"]["gate"], "source_unavailable")
        self.assertEqual(actions["friday"]["assessment"]["gate"], "allowed")
        with self.assertRaises(CiteafterError):
            self.engine.preview("draft")
        self.engine.ingest(self.raw("v2"))
        self.assertEqual(self.engine.preview("draft")["source_version"], "v2")

    def test_two_actual_processes_racing_same_release_produce_one_outbox(self):
        self.action()
        preview = self.engine.preview("draft")
        code = "import json,sys;from citeafter.engine import Engine;e=Engine(sys.argv[1],clock=lambda:'2030-01-04T12:00:00Z');print(json.dumps(e.release(sys.argv[2])))"
        children = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(self.path), preview["preview_id"]],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            for _ in range(2)
        ]
        try:
            outputs = [child.communicate(timeout=20) for child in children]
            self.assertEqual([child.returncode for child in children], [0, 0], outputs)
            receipts = [json.loads(out) for out, _ in outputs]
            self.assertEqual(receipts[0], receipts[1])
            with database(self.path) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM outbox").fetchone()[0], 1)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate()


if __name__ == "__main__":
    unittest.main()
