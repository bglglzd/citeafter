"""Small, explicit SQLite ledger. Checksums detect accidental persisted corruption."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3

from .policy import CiteafterError, canonical, digest, require, strict_json, value_hash

APPLICATION_ID = 0x43495445
SCHEMA_VERSION = 1
SCHEMA = {
    "meta": "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL, checksum TEXT NOT NULL)",
    "sources": "CREATE TABLE sources (sha TEXT PRIMARY KEY, raw BLOB NOT NULL, observed_at TEXT NOT NULL, rendered BLOB NOT NULL, render_sha TEXT NOT NULL, checksum TEXT NOT NULL)",
    "checks": "CREATE TABLE checks (id INTEGER PRIMARY KEY, checked_at TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, source_sha TEXT REFERENCES sources(sha), url TEXT, etag TEXT, received BLOB, received_sha TEXT, checksum TEXT NOT NULL)",
    "actions": "CREATE TABLE actions (id TEXT PRIMARY KEY, data TEXT NOT NULL, checksum TEXT NOT NULL)",
    "claims": "CREATE TABLE claims (id TEXT PRIMARY KEY, action_id TEXT NOT NULL REFERENCES actions(id), revision INTEGER NOT NULL, created_at TEXT NOT NULL, data TEXT NOT NULL, checksum TEXT NOT NULL, UNIQUE(action_id, revision))",
    "assessments": "CREATE TABLE assessments (id TEXT PRIMARY KEY, action_id TEXT NOT NULL REFERENCES actions(id), claim_id TEXT NOT NULL REFERENCES claims(id), known_at TEXT NOT NULL, generation INTEGER NOT NULL, data TEXT NOT NULL, checksum TEXT NOT NULL)",
    "previews": "CREATE TABLE previews (id TEXT PRIMARY KEY, action_id TEXT NOT NULL REFERENCES actions(id), claim_id TEXT NOT NULL REFERENCES claims(id), assessment_id TEXT NOT NULL REFERENCES assessments(id), generation INTEGER NOT NULL, created_at TEXT NOT NULL, data TEXT NOT NULL, checksum TEXT NOT NULL)",
    "outbox": "CREATE TABLE outbox (id TEXT PRIMARY KEY, action_id TEXT NOT NULL UNIQUE REFERENCES actions(id), preview_id TEXT NOT NULL REFERENCES previews(id), data TEXT NOT NULL, checksum TEXT NOT NULL)",
    "cache": "CREATE TABLE cache (url TEXT PRIMARY KEY, source_sha TEXT NOT NULL REFERENCES sources(sha), etag TEXT, check_id INTEGER NOT NULL REFERENCES checks(id), checksum TEXT NOT NULL)",
}
IMMUTABLE = ("sources", "checks", "actions", "claims", "assessments", "previews", "outbox")
for table in IMMUTABLE:
    for verb in ("UPDATE", "DELETE"):
        name = f"immutable_{table}_{verb.lower()}"
        SCHEMA[name] = (
            f"CREATE TRIGGER {name} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT, 'Immutable ledger record'); END"
        )


def pack(data):
    return canonical(data), value_hash(data)


def record_checksum(table, record):
    values = {
        key: {"bytes": len(value), "sha256": digest(value)} if type(value) is bytes else value
        for key, value in dict(record).items()
        if key != "checksum"
    }
    return value_hash({"table": table, "values": values})


def unpack(row):
    try:
        data = strict_json(row["data"].encode("utf-8"), limit=2 * 1024 * 1024)
        require(value_hash(data) == row["checksum"], "Ledger checksum mismatch", "integrity")
        return data
    except (TypeError, KeyError, UnicodeError, CiteafterError) as error:
        raise CiteafterError(
            "integrity", "Ledger record is invalid; retain the database for inspection"
        ) from error


def _verify(connection):
    require(
        connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        and connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION,
        "This is not a supported Citeafter database",
        "database_format",
    )
    actual = {
        row["name"]: row["sql"]
        for row in connection.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
    }
    require(actual == SCHEMA, "Database schema differs from this version", "integrity")
    require(
        connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        and connection.execute("PRAGMA foreign_key_check").fetchone() is None,
        "Database integrity check failed",
        "integrity",
    )
    for table in ("meta", "sources", "checks", "cache"):
        for row in connection.execute("SELECT * FROM " + table):
            require(
                record_checksum(table, row) == row["checksum"],
                "Ledger metadata checksum mismatch",
                "integrity",
            )


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()

    def initialize(self, settings):
        require(not self.path.exists(), "Database already exists; choose a new path", "database_exists")
        require(self.path.parent.is_dir(), "Database parent directory does not exist")
        # Exclusive creation cannot mistake another application's empty file for ours.
        with self.path.open("xb"):
            pass
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            for statement in SCHEMA.values():
                connection.execute(statement)
            for key, value in settings.items():
                record = {"key": key, "value": str(value)}
                connection.execute(
                    "INSERT INTO meta VALUES (?,?,?)", (key, str(value), record_checksum("meta", record))
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def transaction(self, write=False):
        require(self.path.is_file(), "Database does not exist; initialize it first", "database_missing")
        uri = self.path.as_uri() + ("?mode=rw" if write else "?mode=ro")
        connection = None
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            _verify(connection)
            yield connection
            connection.commit()
        except sqlite3.Error as error:
            if connection:
                connection.rollback()
            raise CiteafterError(
                "database",
                "Database operation failed; retain the database and retry after other writers finish",
            ) from error
        except BaseException:
            if connection:
                connection.rollback()
            raise
        finally:
            if connection:
                connection.close()


def setting(connection, key):
    row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    require(row is not None, "Missing database configuration", "integrity")
    return row[0]


def set_setting(connection, key, value):
    checksum = record_checksum("meta", {"key": key, "value": str(value)})
    require(
        connection.execute(
            "UPDATE meta SET value=?,checksum=? WHERE key=?", (str(value), checksum, key)
        ).rowcount
        == 1,
        "Missing database configuration",
        "integrity",
    )


def checked_blob(raw, expected):
    require(type(raw) is bytes and digest(raw) == expected, "Stored source hash mismatch", "integrity")
    return raw
