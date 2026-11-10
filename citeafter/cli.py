"""CLI uses the same engine as the local application; history has no write authority."""
import argparse
from pathlib import Path
import sys

from .demo import Demo, export_recording
from .engine import Engine
from .policy import CiteafterError, canonical, fields, strict_json
from .server import serve


def parser():
    command = argparse.ArgumentParser(prog="citeafter", description="Versioned evidence impact analysis")
    command.add_argument("--db", type=Path, default=Path(".citeafter/ledger.sqlite"), help="Local ledger path")
    sub = command.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create a new ledger")
    init.add_argument("--source-url"); init.add_argument("--ttl-seconds", type=int, default=60)
    ingest = sub.add_parser("import", help="Import original policy JSON bytes")
    ingest.add_argument("path", type=Path)
    sub.add_parser("refresh", help="Fetch the operator-pinned source once")
    create = sub.add_parser("create", help="Save a decision and pending action from JSON")
    create.add_argument("path", type=Path)
    sub.add_parser("state", help="Inspect current decisions and release gates")
    for name in ("recheck", "preview", "history"):
        child = sub.add_parser(name); child.add_argument("action_id")
    release = sub.add_parser("release", help="Release the exact preview to the local outbox")
    release.add_argument("preview_id")
    historical = sub.add_parser("as-of", help="Read-only recipe query with separate business and knowledge times")
    historical.add_argument("action_id"); historical.add_argument("--valid-at", required=True); historical.add_argument("--known-at", required=True)
    local = sub.add_parser("serve", help="Open this ledger in a loopback-only UI")
    local.add_argument("--port", type=int, default=0)
    demo = sub.add_parser("demo", help="Run the synthetic fixture-clock example")
    demo.add_argument("--serve", action="store_true"); demo.add_argument("--port", type=int, default=0)
    demo.add_argument("--export", type=Path, help="Write read-only engine recordings into a new export destination")
    return command


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        engine = Engine(args.db)
        if args.command == "init":
            args.db.resolve().parent.mkdir(parents=True, exist_ok=True)
            engine.initialize(source_url=args.source_url, ttl_seconds=args.ttl_seconds)
            result = {"initialized": True}
        elif args.command == "import":
            with args.path.open("rb") as stream: raw = stream.read(256 * 1024 + 1)
            result = engine.ingest(raw)
        elif args.command == "refresh": result = engine.refresh()
        elif args.command == "state": result = engine.state()
        elif args.command == "create":
            with args.path.open("rb") as stream: data = strict_json(stream.read(65537), limit=65536)
            fields(data, ["id", "label", "business_at", "recipe", "clause", "inputs", "declared_paths"], "action")
            result = engine.create_action(data["id"], **{key: value for key, value in data.items() if key != "id"})
        elif args.command in ("preview", "recheck", "history"):
            result = getattr(engine, args.command)(args.action_id)
        elif args.command == "release": result = engine.release(args.preview_id)
        elif args.command == "as-of": result = engine.as_of(args.action_id, valid_at=args.valid_at, known_at=args.known_at)
        elif args.command == "serve":
            serve(engine, port=args.port); return 0
        elif args.command == "demo":
            if args.export:
                result = export_recording(args.export)
            else:
                # The default operational database is never implicitly reused by the fixture clock.
                path = Path(".citeafter/demo.sqlite") if args.db == Path(".citeafter/ledger.sqlite") else args.db
                demo = Demo(path)
                if args.serve:
                    serve(demo.engine, port=args.port, demo=demo); return 0
                result = demo.engine.state()
        print(canonical(result))
        return 1 if result.get("accepted") is False else 0
    except CiteafterError as error:
        print(canonical({"error": {"code": error.code, "message": str(error)}}), file=sys.stderr)
        return 1
    except OSError:
        print(canonical({"error": {"code": "io", "message": "File or local listener unavailable; check the path and permissions"}}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
