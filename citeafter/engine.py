"""Temporal dependency assessments and transaction-bound local outbox releases."""
from datetime import datetime
import uuid

from .http_source import TypedFetchError, fetch_policy, validate_source_url
from .policy import (CiteafterError, canonical, changed_fields, digest, identifier, instant,
                     intersects, require, select_source, source_record, text, utc_now, value_hash)
from .recipes import EVALUATOR, assess_support, check_inputs, evaluate_recipe, inspect_recipe
from .store import Store, checked_blob, pack, set_setting, setting, unpack


def new_id(prefix):
    return prefix + "-" + uuid.uuid4().hex


def seconds(after, before):
    return (datetime.fromisoformat(after.replace("Z", "+00:00"))
            - datetime.fromisoformat(before.replace("Z", "+00:00"))).total_seconds()


class Engine:
    def __init__(self, path, *, clock=utc_now):
        self.store, self.clock = Store(path), clock

    def initialize(self, *, source_url=None, ttl_seconds=60, mode="local"):
        require(type(ttl_seconds) is int and 1 <= ttl_seconds <= 86400, "Freshness TTL must be one to 86400 seconds")
        require(mode in ("local", "synthetic-local"), "Invalid execution mode")
        if source_url is not None:
            validate_source_url(source_url)
        self.store.initialize({"source_url": source_url or "", "ttl_seconds": ttl_seconds,
                               "mode": mode, "generation": 0, "last_event_at": "", "policy_id": ""})

    def _now(self, connection, write=False):
        now = instant(self.clock())
        previous = setting(connection, "last_event_at")
        require(not previous or now >= previous, "Operational clock precedes the latest event; history remains available", "clock_rollback")
        if write:
            set_setting(connection, "last_event_at", now)
        return now

    def _sources(self, connection):
        result = []
        for row in connection.execute("SELECT * FROM sources ORDER BY observed_at,sha"):
            raw = checked_blob(row["raw"], row["sha"])
            try:
                item = source_record(raw, row["observed_at"])
            except CiteafterError as error:
                raise CiteafterError("integrity", "Stored policy is invalid") from error
            require(item["rendered"]["body"] == row["rendered"] and item["rendered"]["sha256"] == row["render_sha"],
                    "Stored rendering does not match its source", "integrity")
            result.append(item)
        return result

    def _health(self, connection, now):
        generation = int(setting(connection, "generation"))
        row = connection.execute("SELECT * FROM checks ORDER BY id DESC LIMIT 1").fetchone()
        ttl = int(setting(connection, "ttl_seconds"))
        base = {"generation": generation, "ttl_seconds": ttl, "checked_at": None, "age_seconds": None,
                "status": "empty", "reason": "no_successful_source_check"}
        if row is None:
            return base
        if row["received"] is not None:
            checked_blob(row["received"], row["received_sha"])
        age = seconds(now, row["checked_at"])
        status = "error" if row["status"] != "fresh" else ("fresh" if 0 <= age <= ttl else "expired")
        return {**base, "status": status, "checked_at": row["checked_at"], "age_seconds": age,
                "reason": row["reason"] if status != "expired" else "source_check_expired"}

    def _check(self, connection, now, status, reason, *, source_sha=None, url=None, etag=None, raw=None):
        generation = int(setting(connection, "generation")) + 1
        set_setting(connection, "generation", generation)
        cursor = connection.execute("INSERT INTO checks (checked_at,status,reason,source_sha,url,etag,received,received_sha) VALUES (?,?,?,?,?,?,?,?)",
                                    (now, status, reason, source_sha, url, etag, raw, digest(raw) if raw is not None else None))
        return cursor.lastrowid

    def _ingest(self, connection, raw, now, *, url=None, etag=None):
        try:
            source = source_record(raw, now)
            policy_id = setting(connection, "policy_id")
            require(not policy_id or policy_id == source["policy_id"], "One database supports one policy family", "wrong_policy")
        except CiteafterError as error:
            self._check(connection, now, "error", error.code, url=url, raw=raw)
            self._record_all(connection, now)
            return {"accepted": False, "error": {"code": error.code, "message": str(error)}}
        if not policy_id:
            set_setting(connection, "policy_id", source["policy_id"])
        existing = connection.execute("SELECT observed_at FROM sources WHERE sha=?", (source["sha256"],)).fetchone()
        if existing is None:
            connection.execute("INSERT INTO sources VALUES (?,?,?,?,?)", (source["sha256"], raw, now,
                               source["rendered"]["body"], source["rendered"]["sha256"]))
        check_id = self._check(connection, now, "fresh", "http_200" if url else "explicit_import", source_sha=source["sha256"], url=url, etag=etag)
        if url:
            connection.execute("INSERT INTO cache VALUES (?,?,?,?) ON CONFLICT(url) DO UPDATE SET source_sha=excluded.source_sha,etag=excluded.etag,check_id=excluded.check_id",
                               (url, source["sha256"], etag, check_id))
        metrics = self._record_all(connection, now)
        return {"accepted": True, "duplicate": existing is not None, "source_sha256": source["sha256"],
                "version_id": source["version_id"], "observed_at": existing[0] if existing else now, **metrics}

    def ingest(self, raw):
        require(type(raw) is bytes and len(raw) <= 256 * 1024, "Import requires at most256KiB of original bytes")
        with self.store.transaction(True) as connection:
            now = self._now(connection, True)
            self._sources(connection)
            return self._ingest(connection, raw, now)

    def refresh(self, *, timeout=2.0):
        # Fetch outside the write transaction. A304 is committed only against its exact cache snapshot.
        with self.store.transaction() as connection:
            self._sources(connection)
            url = setting(connection, "source_url")
            require(bool(url), "No pinned source URL; import explicitly or initialize with a URL", "no_source_url")
            row = connection.execute("SELECT * FROM cache WHERE url=?", (url,)).fetchone()
            cached = dict(row) if row else None
        try:
            response = fetch_policy(url, etag=cached["etag"] if cached else None, timeout=timeout)
        except TypedFetchError as error:
            with self.store.transaction(True) as connection:
                now = self._now(connection, True)
                self._check(connection, now, "error", error.code, url=url)
                self._record_all(connection, now)
            return {"accepted": False, "error": {"code": error.code, "message": str(error)}, "elapsed_ms": error.elapsed_ms}
        with self.store.transaction(True) as connection:
            now = self._now(connection, True)
            self._sources(connection)
            if response.status == 200:
                return {**self._ingest(connection, response.body, now, url=url, etag=response.etag), "elapsed_ms": response.elapsed_ms}
            current = connection.execute("SELECT * FROM cache WHERE url=?", (url,)).fetchone()
            if cached is None or current is None or dict(current) != cached:
                self._check(connection, now, "error", "conditional_cache_changed", url=url)
                self._record_all(connection, now)
                return {"accepted": False, "error": {"code": "conditional_cache_changed", "message": "Refresh again; the conditional cache changed during this request"}}
            check_id = self._check(connection, now, "fresh", "http_304", source_sha=cached["source_sha"], url=url, etag=response.etag)
            connection.execute("UPDATE cache SET check_id=?,etag=? WHERE url=?", (check_id, response.etag, url))
            return {"accepted": True, "not_modified": True, "source_sha256": cached["source_sha"],
                    "elapsed_ms": response.elapsed_ms, **self._record_all(connection, now)}

    def _action(self, connection, action_id):
        row = connection.execute("SELECT * FROM actions WHERE id=?", (identifier(action_id),)).fetchone()
        require(row is not None, "Action does not exist", "not_found")
        action = unpack(row)
        require(action["id"] == row["id"], "Action identity mismatch", "integrity")
        return action

    def _claim(self, connection, action, sources, *, known_at=None):
        query = "SELECT * FROM claims WHERE action_id=?"
        arguments = [action["id"]]
        if known_at is not None:
            query += " AND created_at<=?"; arguments.append(known_at)
        row = connection.execute(query + " ORDER BY revision DESC LIMIT 1", arguments).fetchone()
        if row is None:
            return None
        claim = unpack(row)
        require(claim["id"] == row["id"] and claim["revision"] == row["revision"] and claim["created_at"] == row["created_at"],
                "Claim identity mismatch", "integrity")
        source = next((item for item in sources if item["sha256"] == claim["result"]["source_sha256"]), None)
        require(source is not None, "Claim source is missing", "integrity")
        inspection = inspect_recipe(source, action["recipe"], action["clause"], action["inputs"], action["declared_paths"])
        require({key: value for key, value in claim["result"].items() if key != "output"} == inspection,
                "Claim dependency or citation binding is invalid", "integrity")
        return claim

    def _receipt(self, connection, action_id):
        row = connection.execute("SELECT * FROM outbox WHERE action_id=?", (action_id,)).fetchone()
        if row is None:
            return None
        receipt = unpack(row)
        require(receipt["id"] == row["id"] and receipt["action_id"] == action_id, "Receipt identity mismatch", "integrity")
        return receipt

    def _assess(self, connection, action, claim, sources, now):
        selection = select_source(sources, action["business_at"], now)
        current = selection["source"]
        if current is None:
            assessment = {"impact_class": "needs_review", "reason_codes": [selection["reason"]], "reason_paths": [], "changed_required_paths": []}
        else:
            inspection = inspect_recipe(current, action["recipe"], action["clause"], action["inputs"], action["declared_paths"])
            assessment = assess_support(claim["result"], inspection)
        health = self._health(connection, now)
        gate = {"affected": "stale", "unaffected": "allowed", "needs_review": "needs_review"}[assessment["impact_class"]]
        reason = {"stale": "required_business_value_changed", "allowed": "supported_current_values", "needs_review": "support_requires_review"}[gate]
        if health["status"] != "fresh":
            gate, reason = "source_unavailable", health["reason"]
        if self._receipt(connection, action["id"]) is not None:
            gate, reason = "released", "original_outbox_receipt_retained"
        saved_source = next(item for item in sources if item["sha256"] == claim["result"]["source_sha256"])
        diffs = changed_fields(saved_source["policy"], current["policy"]) if current else []
        edges = [{"field": diff["path"], "dependency": path, "claim_id": claim["id"], "action_id": action["id"], "action_revision": claim["revision"]}
                 for diff in diffs for path in claim["result"]["declared_paths"] if intersects(diff["path"], path)]
        return {**assessment, "known_at": now, "business_at": action["business_at"],
                "selected_source_version": current["version_id"] if current else None,
                "selected_source_sha256": current["sha256"] if current else None,
                "claim_id": claim["id"], "action_revision": claim["revision"], "generation": health["generation"],
                "gate": gate, "gate_reason": reason, "reverse_paths": edges, "recipe_executed": False}

    def _save_assessment(self, connection, action, claim, now, assessment):
        assessment_id = new_id("assessment")
        connection.execute("INSERT INTO assessments VALUES (?,?,?,?,?,?,?)", (assessment_id, action["id"], claim["id"], now,
                           assessment["generation"], *pack(assessment)))
        return assessment_id

    def _record_all(self, connection, now):
        sources, edges, count = self._sources(connection), 0, 0
        for row in connection.execute("SELECT * FROM actions ORDER BY id").fetchall():
            action = unpack(row); claim = self._claim(connection, action, sources)
            assessment = self._assess(connection, action, claim, sources, now)
            self._save_assessment(connection, action, claim, now, assessment)
            count += 1; edges += len(assessment["reverse_paths"])
        return {"decisions_inspected": count, "reverse_edges_visited": edges, "recipe_executions": 0}

    def create_action(self, action_id, *, label, business_at, recipe, clause, inputs, declared_paths=None):
        identifier(action_id); text(label, 160); check_inputs(recipe, inputs)
        action = {"id": action_id, "label": label, "business_at": instant(business_at), "recipe": recipe,
                  "clause": clause, "inputs": inputs, "declared_paths": declared_paths}
        with self.store.transaction(True) as connection:
            now = self._now(connection, True); sources = self._sources(connection)
            require(connection.execute("SELECT 1 FROM actions WHERE id=?", (action_id,)).fetchone() is None, "Action already exists", "already_exists")
            selected = select_source(sources, action["business_at"], now)
            require(selected["source"] is not None, "A unique effective known source is required to create a claim", "needs_review")
            result = evaluate_recipe(selected["source"], recipe, clause, inputs, declared_paths)
            action["created_at"] = now
            connection.execute("INSERT INTO actions VALUES (?,?,?)", (action_id, *pack(action)))
            claim = self._save_claim(connection, action, result, 1, now)
            assessment = self._assess(connection, action, claim, sources, now)
            self._save_assessment(connection, action, claim, now, assessment)
            return {"id": action_id, "revision": 1, "claim": claim, "assessment": assessment, "recipe_executions": 1}

    def _save_claim(self, connection, action, result, revision, now):
        claim = {"id": new_id("claim"), "action_id": action["id"], "revision": revision, "created_at": now, "result": result}
        connection.execute("INSERT INTO claims VALUES (?,?,?,?,?,?)", (claim["id"], action["id"], revision, now, *pack(claim)))
        return claim

    def recheck(self, action_id):
        with self.store.transaction(True) as connection:
            now = self._now(connection, True); sources = self._sources(connection)
            action = self._action(connection, action_id); claim = self._claim(connection, action, sources)
            assessment = self._assess(connection, action, claim, sources, now)
            require(assessment["gate"] == "stale", "Only a fresh-source, supported, affected pending action can be rechecked", "not_recheck_eligible")
            source = next(item for item in sources if item["sha256"] == assessment["selected_source_sha256"])
            result = evaluate_recipe(source, action["recipe"], action["clause"], action["inputs"], action["declared_paths"])
            require(result["result_status"] == "supported", "The new result requires review", "needs_review")
            updated = self._save_claim(connection, action, result, claim["revision"] + 1, now)
            assessment = self._assess(connection, action, updated, sources, now)
            self._save_assessment(connection, action, updated, now, assessment)
            return {"id": action_id, "revision": updated["revision"], "claim": updated, "assessment": assessment, "recipe_executions": 1}

    def _payload(self, action, claim, assessment):
        return {"action_id": action["id"], "action_revision": claim["revision"], "business_at": action["business_at"],
                "claim_id": claim["id"], "recipe": action["recipe"], "inputs": action["inputs"], "output": claim["result"]["output"],
                "citation_source_sha256": claim["result"]["source_sha256"], "citation_source_version": claim["result"]["source_version"],
                "selected_source_sha256": assessment["selected_source_sha256"], "source_version": assessment["selected_source_version"], "evaluator": EVALUATOR}

    def preview(self, action_id):
        with self.store.transaction(True) as connection:
            now = self._now(connection, True); sources = self._sources(connection)
            action = self._action(connection, action_id); claim = self._claim(connection, action, sources)
            receipt = self._receipt(connection, action_id)
            if receipt:
                return {"already_released": True, "receipt": receipt}
            assessment = self._assess(connection, action, claim, sources, now)
            require(assessment["gate"] == "allowed", "Release refused: " + assessment["gate_reason"], assessment["gate"])
            assessment_id = self._save_assessment(connection, action, claim, now, assessment)
            preview = {"preview_id": new_id("preview"), "action_id": action_id, "action_revision": claim["revision"],
                       "created_at": now, "payload": self._payload(action, claim, assessment),
                       "source_version": assessment["selected_source_version"], "generation": assessment["generation"],
                       "expires_in_seconds": min(60, int(setting(connection, "ttl_seconds")))}
            connection.execute("INSERT INTO previews VALUES (?,?,?,?,?,?,?,?)", (preview["preview_id"], action_id, claim["id"], assessment_id,
                               assessment["generation"], now, *pack(preview)))
            return preview

    def release(self, preview_id):
        with self.store.transaction(True) as connection:
            row = connection.execute("SELECT * FROM previews WHERE id=?", (identifier(preview_id),)).fetchone()
            require(row is not None, "Preview does not exist", "not_found")
            preview = unpack(row); action = self._action(connection, row["action_id"])
            old = self._receipt(connection, action["id"])
            if old is not None:
                return old
            now = self._now(connection, True); sources = self._sources(connection)
            claim = self._claim(connection, action, sources)
            require(preview["preview_id"] == row["id"] and preview["generation"] == row["generation"]
                    and preview["created_at"] == row["created_at"], "Preview identity mismatch", "integrity")
            require(preview["generation"] == int(setting(connection, "generation")) and row["claim_id"] == claim["id"],
                    "Source or action changed; obtain a fresh preview", "stale_preview")
            require(0 <= seconds(now, preview["created_at"]) <= preview["expires_in_seconds"], "Preview expired; obtain a fresh preview", "stale_preview")
            assessment = self._assess(connection, action, claim, sources, now)
            require(assessment["gate"] == "allowed", "Release refused: " + assessment["gate_reason"], assessment["gate"])
            require(canonical(preview["payload"]) == canonical(self._payload(action, claim, assessment)), "Preview payload changed", "stale_preview")
            self._save_assessment(connection, action, claim, now, assessment)
            receipt = {"id": new_id("outbox"), "action_id": action["id"], "action_revision": claim["revision"],
                       "released_at": now, "preview_id": preview_id, "payload": preview["payload"],
                       "source_version": assessment["selected_source_version"], "scope": "local_sqlite_outbox_only"}
            connection.execute("INSERT INTO outbox VALUES (?,?,?,?,?)", (receipt["id"], action["id"], preview_id, *pack(receipt)))
            return receipt

    def state(self):
        with self.store.transaction() as connection:
            now = self._now(connection); sources = self._sources(connection); actions = []
            for row in connection.execute("SELECT * FROM actions ORDER BY id").fetchall():
                action = unpack(row); claim = self._claim(connection, action, sources)
                actions.append({**action, "revision": claim["revision"], "claim": claim,
                                "assessment": self._assess(connection, action, claim, sources, now),
                                "receipt": self._receipt(connection, action["id"])})
            return {"mode": setting(connection, "mode"), "now": now, "health": self._health(connection, now),
                    "sources": [{key: item[key] for key in ("sha256", "version_id", "parent", "effective_from", "observed_at")}
                                | {"render_sha256": item["rendered"]["sha256"], "rendered_text": item["rendered"]["body"].decode("utf-8")}
                                for item in sources], "actions": actions}

    def as_of(self, action_id, *, valid_at, known_at):
        valid_at, known_at = instant(valid_at), instant(known_at)
        with self.store.transaction() as connection:
            action = self._action(connection, action_id); sources = self._sources(connection)
            selection = select_source(sources, valid_at, known_at); source = selection["source"]
            result = evaluate_recipe(source, action["recipe"], action["clause"], action["inputs"], action["declared_paths"]) if source else None
            receipt = self._receipt(connection, action_id)
            if receipt and receipt["released_at"] > known_at:
                receipt = None
            return {"read_only": True, "valid_at": valid_at, "known_at": known_at,
                    "selected_source_version": source["version_id"] if source else None,
                    "result_status": result["result_status"] if result else "needs_review", "output": result["output"] if result else None,
                    "citations": result["citations"] if result else [], "reason_codes": result["reason_codes"] if result else [selection["reason"]],
                    "decision_exists_at_known_at": action["created_at"] <= known_at, "receipt": receipt}

    def history(self, action_id):
        with self.store.transaction() as connection:
            action = self._action(connection, action_id); sources = self._sources(connection)
            self._claim(connection, action, sources)
            return {"action": action, "claims": [unpack(row) for row in connection.execute("SELECT * FROM claims WHERE action_id=? ORDER BY revision", (action_id,))],
                    "assessments": [unpack(row) for row in connection.execute("SELECT * FROM assessments WHERE action_id=? ORDER BY known_at,id", (action_id,))],
                    "receipt": self._receipt(connection, action_id)}
