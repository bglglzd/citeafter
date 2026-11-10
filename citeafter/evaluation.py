"""Replay annotated synthetic decisions through SQLite; gold remains the authority.

The primary denominator is one post-v2 impact per decision. The two v3 tracks
are separate counterfactual branches, never additional decisions. Timings include
call profiling and independent per-case databases, not one shared production DB.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

from .engine import Engine
from .policy import CiteafterError
from . import recipes

EXAMPLES = Path(__file__).resolve().parent / "examples"
CLASSES = ("affected", "unaffected", "needs_review")
GATES = {
    "allowed_with_fresh_preview": "allowed",
    "blocked_stale_support": "stale",
    "blocked_needs_review": "needs_review",
    "return_original_release_receipt": "released",
}
COMPONENTS = (
    "policy.py",
    "recipes.py",
    "store.py",
    "engine.py",
    "http_source.py",
    "evaluation.py",
)


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _tokens(pointer):
    return tuple(token.replace("~1", "/").replace("~0", "~") for token in pointer.split("/")[1:])


def _intersects(left, right):
    a, b = _tokens(left), _tokens(right)
    return a[: len(b)] == b or b[: len(a)] == a


def _clone(source, destination):
    first, second = sqlite3.connect(source), sqlite3.connect(destination)
    try:
        first.backup(second)
    finally:
        second.close()
        first.close()


def _outbox_count(path):
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT count(*) FROM outbox").fetchone()[0]
    finally:
        connection.close()


class _Run:
    def __init__(self, case, directory, policies, observations):
        self.case, self.directory, self.policies, self.observations = (
            case,
            directory,
            policies,
            observations,
        )
        self.now = observations[0]["observed_at"]
        self.engine = Engine(directory / "handled.sqlite", clock=lambda: self.now)
        self.engine.initialize(mode="synthetic-local")
        self.mismatches, self.diagnostics, self.operations, self.gates = [], [], [], []
        self.preview_number = 0
        self.releases = {}
        self.counts = Counter()

    def same(self, phase, field, actual, expected, category="business"):
        if actual != expected:
            self.mismatches.append(
                {
                    "phase": phase,
                    "field": field,
                    "actual": actual,
                    "expected": expected,
                    "category": category,
                }
            )

    def call(self, phase, function, *args, **kwargs):
        calls, supported = 0, 0
        code = recipes.evaluate_recipe.__code__
        previous = sys.getprofile()
        if previous is not None:
            raise RuntimeError("Evaluation requires a thread without an existing profiler")

        def profile(frame, event, result):
            nonlocal calls, supported
            if frame.f_code is code:
                if event == "call":
                    calls += 1
                elif (
                    event == "return"
                    and isinstance(result, dict)
                    and result.get("result_status") == "supported"
                ):
                    supported += 1

        started = time.perf_counter_ns()
        try:
            sys.setprofile(profile)
            return function(*args, **kwargs)
        finally:
            sys.setprofile(previous)
            self.operations.append(
                {
                    "phase": phase,
                    "elapsed_ms": round((time.perf_counter_ns() - started) / 1e6, 6),
                    "recipe_invocations": calls,
                    "supported_recipe_results": supported,
                }
            )

    def state(self, engine=None):
        return self.call("inspection", (engine or self.engine).state)["actions"][0]

    def import_version(self, version, *, phase="fixture_refresh", engine=None):
        return self.call(phase, (engine or self.engine).ingest, self.policies[version])

    def observe_until(self, at):
        existing = {source["version_id"] for source in self.engine.state()["sources"]}
        for row in self.observations:
            if _instant(row["observed_at"]) <= _instant(at) and row["version_id"] not in existing:
                self.now = row["observed_at"]
                self.import_version(row["version_id"], phase="first_observation")
        self.now = at

    def citations(self, phase, claim, expected_version, *, engine=None):
        result = claim["result"]
        self.same(
            phase,
            "citation_source_version",
            result["source_version"],
            expected_version,
            "citation_dependency",
        )
        raw = self.policies.get(result["source_version"])
        if raw is None:
            self.same(
                phase,
                "known_citation_source",
                result["source_version"],
                sorted(self.policies),
                "citation_dependency",
            )
            return
        state = (engine or self.engine).state()
        source = next(
            (row for row in state["sources"] if row["sha256"] == result["source_sha256"]),
            None,
        )
        self.same(
            phase,
            "claim_source_sha256",
            result["source_sha256"],
            _hash(raw),
            "citation_dependency",
        )
        if source is None:
            self.same(phase, "citation_source_present", False, True, "citation_dependency")
            return
        rendered = source["rendered_text"].encode("utf-8")
        self.same(
            phase,
            "render_sha256",
            source["render_sha256"],
            _hash(rendered),
            "citation_dependency",
        )
        self.same(
            phase,
            "citation_paths",
            sorted(row["pointer"] for row in result["citations"]),
            sorted(result["read_values"]),
            "citation_dependency",
        )
        for row in result["citations"]:
            exact = rendered[row["start"] : row["end"]].decode("utf-8", "strict")
            for key, expected in (
                ("quote", exact),
                ("source_sha256", _hash(raw)),
                ("source_version", expected_version),
                ("render_sha256", _hash(rendered)),
                ("unit", "utf8-bytes"),
                ("renderer", "citeafter-render-v1"),
            ):
                self.same(phase, "citation." + key, row[key], expected, "citation_dependency")

    def compare_gate(self, phase, action, expected, *, engine=None):
        engine = engine or self.engine
        wanted = GATES[expected["support_gate"]]
        self.same(phase, "gate", action["assessment"]["gate"], wanted, "gate")
        try:
            preview = self.call("gate_preview", engine.preview, self.case["id"])
            actual = "released" if preview.get("already_released") else "allowed"
        except CiteafterError as error:
            preview, actual = None, error.code
        self.gates.append({"phase": phase, "expected": wanted, "actual": actual})
        self.same(phase, "actual_preview_gate", actual, wanted, "gate")
        if (
            wanted in ("stale", "needs_review", "source_unavailable")
            and actual in ("allowed", "released")
            and not action["receipt"]
        ):
            self.counts["unsafe_previews"] += 1
        if wanted == "allowed" and actual != "allowed":
            self.counts["false_blocks"] += 1
        return preview

    def reasons(self, phase, actual, expected):
        a, b = sorted(actual), sorted(expected)
        if a == b:
            return
        compatible = all(any(_intersects(path, other) for other in a) for path in b) and all(
            any(_intersects(path, other) for other in b) for path in a
        )
        if compatible:
            self.diagnostics.append(
                {
                    "phase": phase,
                    "field": "reason_paths",
                    "actual": a,
                    "expected": b,
                    "meaning": "Decoded ancestor/child coverage matches; diagnostic specificity differs",
                }
            )
        else:
            self.same(phase, "reason_paths", a, b, "citation_dependency")

    def compare_stage(self, phase, action, expected):
        assessment = action["assessment"]
        for key in ("impact_class", "selected_source_version"):
            self.same(
                phase,
                key,
                assessment[key],
                expected[key],
                "temporal" if key == "selected_source_version" else "business",
            )
        self.reasons(phase, assessment["reason_paths"], expected["reason_paths"])
        if expected.get("changed_required_paths") is not None:
            self.same(
                phase,
                "changed_required_paths",
                sorted(assessment["changed_required_paths"]),
                sorted(expected["changed_required_paths"]),
                "citation_dependency",
            )
        self.same(
            phase,
            "pinned_business_at",
            _instant(action["business_at"]).isoformat(),
            _instant(self.case["business_at"]).isoformat(),
            "gate",
        )

    def query_result(self, phase, engine, expected, *, valid_at=None, known_at=None):
        result = self.call(
            phase,
            engine.as_of,
            self.case["id"],
            valid_at=valid_at or self.case["business_at"],
            known_at=known_at or expected["known_at"],
        )
        for key in ("selected_source_version", "result_status"):
            self.same(
                phase,
                key,
                result[key],
                expected[key],
                "temporal" if key == "selected_source_version" else "business",
            )
        output = expected.get("independently_reevaluated_output", expected.get("output"))
        self.same(phase, "output", result["output"], output)
        self.same(phase, "read_only", result["read_only"], True, "gate")
        if "decision_exists_at_known_at" in expected:
            self.same(
                phase,
                "decision_exists_at_known_at",
                result["decision_exists_at_known_at"],
                expected["decision_exists_at_known_at"],
                "temporal",
            )
        return result

    def release_at(self, phase, expected, version):
        self.now = expected["known_at"]
        self.import_version(version)
        preview = self.call("gate_preview", self.engine.preview, self.case["id"])
        receipt = self.call("explicit_release", self.engine.release, preview["preview_id"])
        self.same(
            phase,
            "receipt_source",
            receipt["source_version"],
            expected["support_source_version"],
            "gate",
        )
        self.same(
            phase,
            "receipt_output",
            receipt["payload"]["output"],
            expected["output"],
            "gate",
        )
        self.same(
            phase,
            "outbox_count",
            _outbox_count(self.engine.store.path),
            expected["outbox_count_after_release"],
            "gate",
        )
        self.releases[phase] = {"preview_id": preview["preview_id"], "receipt": receipt}

    def old_preview_probe(self, preview, expected_receipt):
        if not preview or "preview_id" not in preview:
            return
        path = self.directory / "old-preview.sqlite"
        _clone(self.engine.store.path, path)
        probe = Engine(path, clock=lambda: self.now)
        before = _outbox_count(path)
        try:
            receipt = self.call("old_preview_release_probe", probe.release, preview["preview_id"])
            outcome = (
                "original_receipt"
                if expected_receipt and receipt == expected_receipt
                else "unexpected_release"
            )
        except CiteafterError as error:
            outcome = error.code
        expected = "original_receipt" if expected_receipt else "stale_preview"
        self.same("after_v2", "old_generation_preview", outcome, expected, "gate")
        after = _outbox_count(path)
        self.same("after_v2", "old_preview_new_outbox", after - before, 0, "gate")
        self.counts["old_preview_release_attempts"] += 1
        self.counts["unsafe_releases"] += int(after > before)

    def finish(self):
        case = self.case
        self.observe_until(case["created_known_at"])
        latest = [row for row in self.observations if _instant(row["observed_at"]) <= _instant(self.now)][-1]
        self.import_version(latest["version_id"])
        created = self.call(
            "initial_creation",
            self.engine.create_action,
            case["id"],
            label=case["title"],
            business_at=case["business_at"],
            recipe=case["recipe"],
            clause=case["clause"],
            inputs=case["inputs"],
            declared_paths=case["dependency_coverage"]["declared_paths"]
            if case["recipe"] == "manual_claim"
            else None,
        )
        original = created["claim"]
        initial = case["initial_expected"]
        for key in ("result_status", "output", "read_values"):
            self.same(
                "initial",
                key,
                original["result"][key],
                initial[key],
                "citation_dependency" if key == "read_values" else "business",
            )
        self.same(
            "initial",
            "source_version",
            original["result"]["source_version"],
            initial["selected_source_version"],
            "temporal",
        )
        self.same(
            "initial",
            "coverage_proven",
            original["result"]["coverage_proven"],
            case["dependency_coverage"]["proven"],
            "citation_dependency",
        )
        self.same(
            "initial",
            "required_paths",
            original["result"]["required_paths"],
            case["dependency_coverage"]["required_paths"],
            "citation_dependency",
        )
        self.citations("initial", original, initial["citation_source_version"])
        old_preview = self.compare_gate("initial", self.state(), initial["gate"])
        if case["action_state"]["initial_release"]:
            expected = case["action_state"]["initial_release"]
            self.release_at("initial_release", expected, expected["support_source_version"])
        self.observe_until(case["after_v2"]["known_at"])
        update = self.import_version("v2", phase="v2_update")
        after = self.state()
        expected = case["after_v2"]
        self.compare_stage("after_v2", after, expected)
        self.citations(
            "after_v2_retained",
            after["claim"],
            expected["saved_support_version_before_recheck"],
        )
        self.compare_gate("after_v2", after, expected["gate_before_recheck"])
        self.old_preview_probe(old_preview, after["receipt"])
        baseline = self.query_result("v2_full_recompute", self.engine, expected)
        unhandled_path = self.directory / "without-v2-recheck.sqlite"
        _clone(self.engine.store.path, unhandled_path)
        unhandled = Engine(unhandled_path, clock=lambda: self.now)
        rechecked = False
        try:
            self.call("v2_recheck", self.engine.recheck, case["id"])
            rechecked = True
        except CiteafterError as error:
            self.same(
                "after_v2",
                "recheck_refusal",
                error.code,
                "not_recheck_eligible",
                "gate",
            )
        self.same(
            "after_v2",
            "recheck_eligible",
            rechecked,
            expected["recheck"]["eligible"],
            "gate",
        )
        handled = self.state()
        self.same(
            "after_v2_handling",
            "revision",
            handled["revision"],
            2 if expected["recheck"]["new_claim_and_action_revision"] else 1,
            "gate",
        )
        self.same(
            "after_v2_handling",
            "support_source_version",
            handled["claim"]["result"]["source_version"],
            expected["recheck"]["support_source_version_after_handling"],
            "citation_dependency",
        )
        self.citations(
            "after_v2_handling",
            handled["claim"],
            expected["recheck"]["citation_source_version_after_handling"],
        )
        if rechecked:
            self.same(
                "after_v2_handling",
                "output",
                handled["claim"]["result"]["output"],
                expected["independently_reevaluated_output"],
            )
        self.compare_gate("after_v2_handling", handled, expected["recheck"]["gate_after_handling"])
        if case["action_state"]["v2_release"]:
            release = case["action_state"]["v2_release"]
            self.release_at("v2_release", release, release["support_source_version"])
        branches = {}
        for name, engine in (
            ("without_v2_recheck", unhandled),
            ("after_v2_handling", self.engine),
        ):
            gold = case["after_v3"][name]
            self.now = gold["known_at"]
            self.import_version("v3", phase="v3_update_" + name, engine=engine)
            action = self.state(engine)
            self.compare_stage("v3_" + name, action, gold)
            self.citations(
                "v3_" + name,
                action["claim"],
                gold["citation_source_version_without_recheck"],
                engine=engine,
            )
            self.same(
                "v3_" + name,
                "citation_source_version",
                action["claim"]["result"]["source_version"],
                gold["citation_source_version_without_recheck"],
                "citation_dependency",
            )
            self.same(
                "v3_" + name,
                "outbox_count",
                _outbox_count(engine.store.path),
                gold["outbox_count_before_optional_release"],
                "gate",
            )
            self.compare_gate("v3_" + name, action, gold["gate_before_recheck"], engine=engine)
            # Full recomputation is secondary evidence, independently checked against gold.
            self.query_result("v3_full_recompute_" + name, engine, gold)
            branches[name] = {
                "impact_class": action["assessment"]["impact_class"],
                "expected_class": gold["impact_class"],
                "gate": action["assessment"]["gate"],
                "source_version": action["assessment"]["selected_source_version"],
                "citation_source_version": action["claim"]["result"]["source_version"],
            }
            did_recheck = False
            try:
                self.call("v3_recheck_" + name, engine.recheck, case["id"])
                did_recheck = True
            except CiteafterError as error:
                self.same(
                    "v3_" + name,
                    "recheck_refusal",
                    error.code,
                    "not_recheck_eligible",
                    "gate",
                )
            self.same(
                "v3_" + name,
                "recheck_eligible",
                did_recheck,
                gold["recheck_eligible"],
                "gate",
            )
            final = self.state(engine)
            self.same(
                "v3_" + name,
                "support_after_handling",
                final["claim"]["result"]["source_version"],
                gold["support_source_version_after_handling"],
                "citation_dependency",
            )
            self.citations(
                "v3_" + name + "_handled",
                final["claim"],
                gold["citation_source_version_after_handling"],
                engine=engine,
            )
            self.compare_gate(
                "v3_" + name + "_handled",
                final,
                gold["gate_after_handling"],
                engine=engine,
            )
        before = _hash(self.engine.store.path.read_bytes())
        histories = []
        for query in case["historical_queries"]:
            result = self.query_result(
                "historical_query",
                self.engine,
                query,
                valid_at=query["valid_at"],
                known_at=query["known_at"],
            )
            histories.append(
                {
                    "id": query["id"],
                    "selected_source_version": result["selected_source_version"],
                    "result_status": result["result_status"],
                    "output": result["output"],
                }
            )
        self.same(
            "history",
            "database_unchanged",
            _hash(self.engine.store.path.read_bytes()),
            before,
            "gate",
        )
        for name, release in self.releases.items():
            first = release["receipt"]
            self.same(
                "replay_" + name,
                "receipt",
                self.call("receipt_replay", self.engine.release, release["preview_id"]),
                first,
                "gate",
            )
        self.same(
            "replay",
            "outbox_count",
            _outbox_count(self.engine.store.path),
            case["action_state"]["after_v3_replay"]["outbox_count_in_handled_branch"],
            "gate",
        )
        for source in self.engine.state()["sources"]:
            observed = next(row for row in self.observations if row["version_id"] == source["version_id"])
            self.same(
                "source_history",
                "first_observed_at",
                _instant(source["observed_at"]).isoformat(),
                _instant(observed["observed_at"]).isoformat(),
                "temporal",
            )
        return {
            "id": case["id"],
            "expected_class": expected["impact_class"],
            "impact_class": after["assessment"]["impact_class"],
            "binary_affected_metric_eligible": expected["binary_affected_metric_eligible"],
            "v2_rechecked": rechecked,
            "v2_update": update,
            "v2_full_recompute": {
                key: baseline[key] for key in ("selected_source_version", "result_status", "output")
            },
            "v3_branches": branches,
            "historical_queries": histories,
            "gates": self.gates,
            "counts": dict(self.counts),
            "mismatches": self.mismatches,
            "diagnostics": self.diagnostics,
            "operations": self.operations,
            "passed": not self.mismatches,
        }


def _matrix(rows, actual="impact_class", expected="expected_class"):
    result = {name: {other: 0 for other in CLASSES} for name in CLASSES}
    for row in rows:
        if row.get(expected) in result and row.get(actual) in CLASSES:
            result[row[expected]][row[actual]] += 1
    return result


def _summary(cases):
    complete = [row for row in cases if "impact_class" in row]
    subset = [row for row in complete if row["binary_affected_metric_eligible"]]
    tp = sum(row["expected_class"] == row["impact_class"] == "affected" for row in subset)
    fp = sum(row["expected_class"] != "affected" and row["impact_class"] == "affected" for row in subset)
    fn = sum(row["expected_class"] == "affected" and row["impact_class"] != "affected" for row in subset)
    mismatches = [issue for row in cases for issue in row["mismatches"]]
    counts = Counter()
    for row in cases:
        counts.update(row.get("counts", {}))
    operations = [op for row in cases for op in row.get("operations", [])]
    phases = sorted({row["phase"] for row in operations})
    measured = {}
    for phase in phases:
        chosen = [row for row in operations if row["phase"] == phase]
        elapsed = sorted(row["elapsed_ms"] for row in chosen)
        measured[phase] = {
            "operations": len(chosen),
            "recipe_invocations": sum(row["recipe_invocations"] for row in chosen),
            "supported_recipe_results": sum(row["supported_recipe_results"] for row in chosen),
            "elapsed_ms_total": round(sum(elapsed), 6),
            "elapsed_ms_median": statistics.median(elapsed),
            "elapsed_ms_p90": elapsed[max(0, (9 * len(elapsed) + 9) // 10 - 1)],
        }
    return {
        "decisions": len(cases),
        "completed_decisions": len(complete),
        "passed_decisions": sum(row["passed"] for row in cases),
        "primary_stage": "after_v2_before_recheck",
        "matrix_rows": "gold",
        "matrix_columns": "implementation",
        "confusion_matrix": _matrix(complete),
        "v3_branch_confusion_matrices": {
            name: _matrix([row["v3_branches"][name] for row in complete])
            for name in ("without_v2_recheck", "after_v2_handling")
        },
        "binary_affected": {
            "eligible_decisions": len(subset),
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
        },
        "v2_rechecked_decisions": sum(row.get("v2_rechecked", False) for row in complete),
        "v2_decisions_inspected": sum(row["v2_update"]["decisions_inspected"] for row in complete),
        "v2_reverse_edges_visited": sum(row["v2_update"]["reverse_edges_visited"] for row in complete),
        "full_recompute_decisions": measured.get("v2_full_recompute", {}).get("recipe_invocations", 0),
        "historical_queries": sum(len(row.get("historical_queries", [])) for row in complete),
        "unsafe_releases": counts["unsafe_releases"],
        "unsafe_previews": counts["unsafe_previews"],
        "false_blocks": counts["false_blocks"],
        "gate_preview_attempts": measured.get("gate_preview", {}).get("operations", 0),
        "old_preview_release_attempts": counts["old_preview_release_attempts"],
        "temporal_mismatches": sum(row["category"] == "temporal" for row in mismatches),
        "citation_dependency_mismatches": sum(row["category"] == "citation_dependency" for row in mismatches),
        "mismatch_count": len(mismatches),
        "diagnostic_specificity_differences": sum(len(row.get("diagnostics", [])) for row in cases),
        "operation_measurements": measured,
    }


def _source_identity(base):
    commit, dirty = None, None
    options = {
        "cwd": base,
        "capture_output": True,
        "text": True,
        "timeout": 5,
        "check": True,
        "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    }
    try:
        top = Path(
            subprocess.run(["git", "rev-parse", "--show-toplevel"], **options).stdout.strip()
        ).resolve()
        # An installed wheel inside a checkout's .venv is not that checkout's source.
        if (top / "citeafter").resolve() == base:
            commit = subprocess.run(["git", "rev-parse", "HEAD"], **options).stdout.strip()
            dirty = bool(subprocess.run(["git", "status", "--porcelain"], **options).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return commit, dirty


def evaluate_cases(case_dir, *, output_dir=None):
    """Run one immutable attempt. An existing output directory is never reused.

    The caller supplies annotated decisions in the public fixture format. Policies
    and observations come from this installed package's three declared versions.
    No network, model, customer dispatch, hidden label generation or gold edits.
    """
    case_dir = Path(case_dir).resolve()
    files = sorted(case_dir.glob("*.json"))
    if not files:
        raise ValueError("No annotated decision JSON files")
    if len(files) > 1000:
        raise ValueError("At most1000 annotated decisions per run")
    raw_cases = {path.name: path.read_bytes() for path in files}
    cases = [json.loads(raw_cases[path.name]) for path in files]
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Decision IDs must be unique")
    policies = {path.stem: path.read_bytes() for path in sorted((EXAMPLES / "policies").glob("*.json"))}
    observations_raw = (EXAMPLES / "observations.json").read_bytes()
    observations = sorted(
        json.loads(observations_raw)["observations"],
        key=lambda row: _instant(row["observed_at"]),
    )
    if set(policies) != {row["version_id"] for row in observations}:
        raise ValueError("Policy/observation identities differ")
    for row in observations:
        if _hash(policies[row["version_id"]]) != row["sha256"]:
            raise ValueError("Policy differs from its observation manifest")
    target = Path(output_dir).resolve() if output_dir is not None else None
    if target:
        target.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    timer = time.perf_counter_ns()
    base = Path(__file__).resolve().parent
    commit, source_tree_dirty = _source_identity(base)
    components = {name: _hash((base / name).read_bytes()) for name in COMPONENTS}
    input_manifest = {
        "decisions": [{"file": path.name, "sha256": _hash(raw_cases[path.name])} for path in files],
        "policies": {version: _hash(raw) for version, raw in policies.items()},
        "observations_sha256": _hash(observations_raw),
    }
    if target:
        with (target / "attempt.json").open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(
                _json(
                    {
                        "schema_version": 1,
                        "started_at": started,
                        "source_commit_sha": commit,
                        "source_tree_dirty": source_tree_dirty,
                        "components": components,
                        "inputs": input_manifest,
                    }
                )
                + "\n"
            )
    outcomes = []
    with tempfile.TemporaryDirectory(prefix="citeafter-evaluation-") as owned:
        for index, case in enumerate(cases):
            directory = Path(owned) / str(index)
            directory.mkdir()
            run = _Run(case, directory, policies, observations)
            try:
                result = run.finish()
            except Exception as error:
                result = {
                    "id": case["id"],
                    "passed": False,
                    "mismatches": run.mismatches
                    + [
                        {
                            "phase": "execution",
                            "field": "completed",
                            "actual": {
                                "exception": type(error).__name__,
                                "code": getattr(error, "code", None),
                                "message": str(error),
                            },
                            "expected": True,
                            "category": "execution",
                        }
                    ],
                    "diagnostics": run.diagnostics,
                    "operations": run.operations,
                    "counts": dict(run.counts),
                }
            outcomes.append(result)
            if target:
                with (target / f"case-{index + 1:03d}.json").open(
                    "x", encoding="utf-8", newline="\n"
                ) as stream:
                    stream.write(_json(result) + "\n")
    unchanged_inputs = sorted(case_dir.glob("*.json")) == files and all(
        path.read_bytes() == raw_cases[path.name] for path in files
    )
    unchanged_inputs = unchanged_inputs and (EXAMPLES / "observations.json").read_bytes() == observations_raw
    unchanged_inputs = unchanged_inputs and all(
        (EXAMPLES / "policies" / (version + ".json")).read_bytes() == raw for version, raw in policies.items()
    )
    unchanged_components = components == {name: _hash((base / name).read_bytes()) for name in COMPONENTS}
    summary = _summary(outcomes)
    report = {
        "schema_version": 1,
        "scope": "Agent-authored synthetic fixed-set SQLite replay; no customer/model accuracy inference",
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": round((time.perf_counter_ns() - timer) / 1e6, 6),
        "source_commit_sha": commit,
        "source_tree_dirty": source_tree_dirty,
        "input_bytes_unchanged": unchanged_inputs,
        "component_bytes_unchanged": unchanged_components,
        "toolchain": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "os": platform.system(),
            "os_release": platform.release(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "logical_cpus": os.cpu_count(),
        },
        "components": components,
        "inputs": input_manifest,
        "method": {
            "primary_denominator": "One post-v2, pre-recheck classification per unique annotated decision",
            "v3_branches": "Mutually exclusive handled/unhandled copies; not extra decisions",
            "correctness_authority": "Original predeclared gold; fresh full recomputation is secondary and also checked against gold",
            "reason_codes": "Narrative reason strings are diagnostic; business class, source, output, required paths and gates are scored",
            "reason_paths": "Exact pointers preferred; decoded ancestor/child coverage allowed and disclosed separately",
            "execution_count": "Actual evaluate_recipe calls observed by Python profiler; supported results counted separately",
            "timing": "Profiler overhead included; independent per-case SQLite databases, not one shared batch workload",
            "release_scope": "Actual local SQLite outbox; blocked old-preview probes use independent copied ledgers",
        },
        "summary": summary,
        "cases": outcomes,
        "passed": summary["passed_decisions"] == len(cases) and unchanged_inputs and unchanged_components,
    }
    if target:
        with (target / "report.json").open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(_json(report) + "\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Evaluate annotated synthetic decisions through the SQLite engine"
    )
    parser.add_argument("--cases", type=Path, default=EXAMPLES / "development")
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New immutable-attempt output directory",
    )
    args = parser.parse_args(argv)
    try:
        report = evaluate_cases(args.cases, output_dir=args.output)
    except (OSError, ValueError, KeyError) as error:
        print(
            _json({"error": type(error).__name__, "message": str(error)}),
            file=sys.stderr,
        )
        return 2
    print(_json({"passed": report["passed"], "summary": report["summary"]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
