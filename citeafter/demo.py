"""Synthetic, persisted fixture-clock journey through the real SQLite engine."""

from pathlib import Path
import tempfile

from .engine import Engine
from .policy import canonical, require, strict_json
from .store import setting

EXAMPLES = Path(__file__).resolve().parent / "examples"
SHOWCASE = ("D01", "D02", "D03", "D12", "D14", "D16")
LABELS = {
    "D01": "Friday quote",
    "D02": "Tuesday quote · 2,000.00 USD",
    "D03": "Tuesday quote · 1,000.05 USD",
    "D12": "Tuesday domestic shipping",
    "D14": "Tuesday VIP quote",
    "D16": "Dispatch exception note",
}
STAMPS = {"v1": "2030-01-03T09:00:00Z", "v2": "2030-01-04T15:00:00Z", "v3": "2030-01-08T09:00:00Z"}


def policy_bytes(version):
    require(version in STAMPS, "Unknown fixture version")
    return (EXAMPLES / "policies" / (version + ".json")).read_bytes()


class Demo:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.now = STAMPS["v1"]
        self.engine = Engine(self.path, clock=lambda: self.now)
        if self.path.exists():
            with self.engine.store.transaction() as connection:
                require(
                    setting(connection, "mode") == "synthetic-local",
                    "Existing database is not a synthetic demo",
                    "wrong_mode",
                )
                self.now = setting(connection, "last_event_at") or self.now
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.engine.initialize(mode="synthetic-local")
            self.engine.ingest(policy_bytes("v1"))
            self.now = "2030-01-04T12:00:00Z"
            self.engine.ingest(policy_bytes("v1"))
            for name in SHOWCASE:
                case = strict_json((EXAMPLES / "development" / (name + ".json")).read_bytes())
                self.engine.create_action(
                    case["id"],
                    label=LABELS[name],
                    business_at=case["business_at"],
                    recipe=case["recipe"],
                    clause=case["clause"],
                    inputs=case["inputs"],
                    declared_paths=case["dependency_coverage"]["declared_paths"]
                    if case["recipe"] == "manual_claim"
                    else None,
                )
            self.now = "2030-01-04T12:05:00Z"
            self.engine.ingest(policy_bytes("v1"))
            self.engine.release(self.engine.preview("D01")["preview_id"])

    def advance(self, version):
        require(version in ("v2", "v3"), "Choose v2 or v3")
        state = self.engine.state()
        existing = {row["version_id"] for row in state["sources"]}
        require(version not in existing, "That fixture version is already observed", "already_observed")
        require(version != "v3" or "v2" in existing, "Observe v2 before v3", "fixture_order")
        self.now = STAMPS[version]
        return self.engine.ingest(policy_bytes(version))

    def refresh(self):
        versions = {row["version_id"] for row in self.engine.state()["sources"]}
        version = next(value for value in ("v3", "v2", "v1") if value in versions)
        return self.engine.ingest(policy_bytes(version))


def export_recording(directory):
    """Export observed engine states; static JavaScript only selects these records."""
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "recorded.json"
    require(
        not destination.exists(), "Recording already exists; choose a new export directory", "already_exists"
    )
    with tempfile.TemporaryDirectory(prefix="citeafter-recording-") as owned:
        demo = Demo(Path(owned) / "demo.sqlite")
        stages, queries = [], []

        def save(stage_id, label):
            state = demo.engine.state()
            state["mode"] = "recorded"
            stages.append({"id": stage_id, "label": label, "state": state})

        save("v1", "v1 · Friday")
        demo.advance("v2")
        save("v2", "v2 · Monday change")
        demo.engine.recheck("D02")
        save("rechecked", "D02 · Rechecked")
        demo.engine.release(demo.engine.preview("D02")["preview_id"])
        save("released", "D02 · Released locally")
        demo.advance("v3")
        save("v3", "v3 · Title correction")
        for action in demo.engine.state()["actions"]:
            for valid_at in ("2030-01-04T12:00:00Z", "2030-01-08T12:00:00Z"):
                for known_at in ("2030-01-04T12:00:00Z", STAMPS["v2"], STAMPS["v3"]):
                    queries.append(
                        {
                            "action_id": action["id"],
                            "valid_at": valid_at,
                            "known_at": known_at,
                            "result": demo.engine.as_of(action["id"], valid_at=valid_at, known_at=known_at),
                        }
                    )
        recording = {
            "schema_version": 1,
            "mode": "recorded",
            "scope": "Synthetic fixture-clock outputs from the SQLite engine; no external customer dispatch",
            "stages": stages,
            "as_of": queries,
        }
        destination.write_text(canonical(recording) + "\n", encoding="utf-8", newline="\n")
    return {"path": str(destination), "stages": len(stages), "read_only_queries": len(queries)}
