"""Build one clean-source candidate; retain and review these exact artifact bytes."""

import argparse
import hashlib
from importlib.metadata import version as installed_version
from importlib.metadata import PackageNotFoundError
import io
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ("index.html", "app.js", "app.css", "mark.svg", "icon.svg")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(data):
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def git(*arguments):
    return subprocess.check_output(["git", *arguments], cwd=ROOT)


def snapshot(destination):
    source_sha = git("rev-parse", "HEAD").decode("ascii").strip()
    if not re.fullmatch(r"[a-f0-9]{40}", source_sha) or git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Build requires a clean checkout at a full commit SHA")
    rows = []
    for entry in git("ls-tree", "-r", "-z", source_sha).split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, blob = metadata.split(b" ")
        path = raw_path.decode("utf-8")
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise ValueError("Release snapshot does not support symlinks or submodules")
        local = ROOT / path
        content = git("cat-file", "blob", blob.decode("ascii"))
        if local.is_symlink() or not local.is_file() or local.read_bytes() != content:
            raise ValueError("Working bytes differ from the committed blob: " + path)
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        rows.append({"path": path, "bytes": len(content), "sha256": digest(content)})
    if git("rev-parse", "HEAD").decode("ascii").strip() != source_sha or git(
        "status", "--porcelain", "--untracked-files=all"
    ):
        raise ValueError("Checkout changed while reading the source snapshot")
    return source_sha, digest(encoded(rows))


def build(output):
    if output.exists() or output.is_symlink() or not output.parent.is_dir() or output.parent.is_symlink():
        raise ValueError("Choose a fresh output directory under an existing non-symlink parent")
    with tempfile.TemporaryDirectory(prefix="citeafter-build-") as temporary:
        workspace = Path(temporary)
        source = workspace / "source"
        source.mkdir()
        source_sha, source_tree_sha256 = snapshot(source)
        project = tomllib.loads((source / "pyproject.toml").read_text("utf-8"))["project"]
        release_version = project["version"]
        if project["name"] != "citeafter" or release_version != "0.1.0":
            raise ValueError("Unexpected product or release version")
        for name in ASSETS:
            if not (source / "citeafter/assets" / name).is_file():
                raise ValueError("Required static asset is missing: " + name)
        toolchain = {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            **{name: installed_version(name) for name in ("build", "setuptools", "wheel")},
        }
        configuration = {
            name: digest((source / name).read_bytes())
            for name in (
                "pyproject.toml",
                "requirements-dev.txt",
                ".gitattributes",
                "scripts/build-release.py",
                "citeafter/demo.py",
            )
        }
        output.mkdir()
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-m",
                "build",
                "--no-isolation",
                "--outdir",
                str(output),
                str(source),
            ],
            cwd=source,
            check=True,
        )
        expected_packages = {
            f"citeafter-{release_version}-py3-none-any.whl",
            f"citeafter-{release_version}.tar.gz",
        }
        if {path.name for path in output.iterdir()} != expected_packages:
            raise ValueError("Unexpected built package inventory")
        recording = workspace / "recording"
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys;sys.path.insert(0,sys.argv[1]);from citeafter.demo import export_recording;export_recording(sys.argv[2])",
                str(source),
                str(recording),
            ],
            cwd=source,
            check=True,
        )
        files = {name: (source / "citeafter/assets" / name).read_bytes() for name in ASSETS}
        files["recorded.json"] = (recording / "recorded.json").read_bytes()
        static_files = [
            {"path": name, "bytes": len(data), "sha256": digest(data)} for name, data in sorted(files.items())
        ]
        files["LICENSE"] = (source / "LICENSE").read_bytes()
        ownership = {
            "format": "citeafter-demo-output-v1",
            "product": "citeafter",
            "version": release_version,
            "source_sha": source_sha,
            "files": static_files,
            "license_sha256": digest(files["LICENSE"]),
        }
        files["citeafter-demo-output.json"] = encoded(ownership)
        archive_name = f"citeafter-demo-{release_version}.zip"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in sorted(files.items()):
                info = zipfile.ZipInfo("citeafter-demo/" + name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data)
        (output / archive_name).write_bytes(buffer.getvalue())
        artifacts = [
            {"name": path.name, "bytes": path.stat().st_size, "sha256": digest(path.read_bytes())}
            for path in sorted(output.iterdir())
        ]
        manifest = {
            "schema_version": 1,
            "product": "citeafter",
            "version": release_version,
            "source_sha": source_sha,
            "source_tree_sha256": source_tree_sha256,
            "toolchain": toolchain,
            "configuration_sha256": configuration,
            "build_configuration": {
                "isolation": False,
                "source": "exact tracked Git blob bytes",
                "recording": "actual synthetic fixture-clock engine outputs",
                "rebuild_limitation": "Recording identifiers and package bytes can change; acceptance binds these exact artifact digests.",
            },
            "artifacts": artifacts,
            "static_files": static_files,
            "license_sha256": digest(files["LICENSE"]),
            "ownership_sha256": digest(files["citeafter-demo-output.json"]),
        }
        (output / "release-manifest.json").write_bytes(encoded(manifest))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = build(args.out.absolute())
        print(
            json.dumps({"source_sha": manifest["source_sha"], "artifacts": manifest["artifacts"]}, indent=2)
        )
    except (ValueError, OSError, subprocess.SubprocessError, PackageNotFoundError) as error:
        parser.exit(1, "Release build refused: " + str(error) + "\n")
