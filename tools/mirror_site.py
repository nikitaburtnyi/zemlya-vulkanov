#!/usr/bin/env python3
"""Build a validated public Pages artifact from one canonical snapshot.

Default: read the public canonical repository at one immutable commit SHA.
--local: read the public files from the current working directory instead.
--sync-root .: also retain these exact public files in a legacy repository.

No credentials, third-party packages, or external writes are used by this script.
"""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request


CANONICAL_REPO = "klop-games/zemlya-vulkanov-privacy"
KNOWN_NAMES = frozenset({
    "activity", "destinations", "guide", "marks", "safety", "passport",
    "hotsprings", "protected", "seasons", "routes", "places", "city",
    "packing", "volcanoes",
})
MAX_FILE_BYTES = 2_000_000
NETWORK_TIMEOUT = 20
SNAPSHOT_TIMEOUT = 240


def checked_bytes(data, name):
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError(f"{name}: empty or larger than {MAX_FILE_BYTES} bytes")
    return data


def parse_json(data, name):
    checked_bytes(data, name)
    def reject_nonfinite(value):
        raise ValueError(f"{name}: non-finite numbers are not valid JSON")
    try:
        value = json.loads(data.decode("utf-8"), parse_constant=reject_nonfinite)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name}: invalid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name}: expected a JSON object")
    return value


def manifest_versions(data):
    versions = parse_json(data, "updates.json").get("files")
    if not isinstance(versions, dict) or not versions:
        raise ValueError("updates.json: files must be a nonempty object")
    for name, version in versions.items():
        if name not in KNOWN_NAMES:
            raise ValueError(f"updates.json: unsupported file name {name!r}")
        if type(version) is not int or version < 1:
            raise ValueError(f"updates.json: {name} has an invalid version")
    return versions


def public_filenames(versions):
    return ["index.html", "notices.json", "updates.json"] + [
        name + ".json" for name in sorted(versions)
    ]


def validate_snapshot(snapshot):
    versions = manifest_versions(snapshot["updates.json"])
    filenames = public_filenames(versions)
    if set(snapshot) != set(filenames):
        raise ValueError("Snapshot must contain exactly the declared public files")
    html = checked_bytes(snapshot["index.html"], "index.html").decode("utf-8")
    if "<html" not in html.lower():
        raise ValueError("index.html: expected an HTML document")
    notices = parse_json(snapshot["notices.json"], "notices.json")
    if not isinstance(notices.get("items"), list):
        raise ValueError("notices.json: items must be an array")
    for name, version in versions.items():
        actual = parse_json(snapshot[name + ".json"], name + ".json").get("version")
        if type(actual) is not int or actual != version:
            raise ValueError(f"{name}.json: version does not match updates.json ({version})")
    return filenames


def fetch_public(url, deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError("Snapshot download exceeded its time limit")
    host = urllib.parse.urlsplit(url).netloc
    if not url.startswith("https://") or host not in {"api.github.com", "raw.githubusercontent.com"}:
        raise ValueError("Unexpected public download URL")
    request = urllib.request.Request(url, headers={
        "User-Agent": "zemlya-vulkanov-pages-mirror",
        "Accept": "application/vnd.github+json" if host == "api.github.com" else "*/*",
    })
    with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT) as response:
        final = urllib.parse.urlsplit(response.geturl())
        if response.status != 200 or final.scheme != "https" or final.netloc != host:
            raise ValueError("Unexpected public download response")
        length = response.headers.get("Content-Length")
        if length is not None and int(length) > MAX_FILE_BYTES:
            raise ValueError("Public download exceeds the file size limit")
        data = bytearray()
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Snapshot download exceeded its time limit")
            chunk = response.read(min(65_536, MAX_FILE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_FILE_BYTES:
                raise ValueError("Public download exceeds the file size limit")
    return checked_bytes(bytes(data), url.rsplit("/", 1)[-1])


def remote_snapshot():
    deadline = time.monotonic() + SNAPSHOT_TIMEOUT
    commit = parse_json(fetch_public(
        f"https://api.github.com/repos/{CANONICAL_REPO}/commits/main", deadline
    ), "canonical commit")
    sha = commit.get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Canonical commit has no valid SHA")
    base = f"https://raw.githubusercontent.com/{CANONICAL_REPO}/{sha}/"
    manifest = fetch_public(base + "updates.json", deadline)
    filenames = public_filenames(manifest_versions(manifest))
    snapshot = {"updates.json": manifest}
    for name in filenames:
        if name != "updates.json":
            snapshot[name] = fetch_public(base + name, deadline)
    return snapshot, sha


def local_file(root, name):
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{name}: expected a regular local file")
    with path.open("rb") as source:
        return checked_bytes(source.read(MAX_FILE_BYTES + 1), name)


def local_snapshot(root):
    manifest = local_file(root, "updates.json")
    filenames = public_filenames(manifest_versions(manifest))
    return {name: manifest if name == "updates.json" else local_file(root, name)
            for name in filenames}, "local"


def write_atomic(path, data):
    # Replace this file only; do not follow an existing destination symlink.
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(data)
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true", help="Read public files from cwd")
    parser.add_argument("--output", default="_site", help="Empty artifact directory")
    parser.add_argument("--sync-root", type=Path, help="Also copy public files to this existing directory")
    args = parser.parse_args(argv)
    output = Path(args.output)
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise ValueError("Artifact output must be absent or an empty directory")
    if args.sync_root is not None and not args.sync_root.is_dir():
        raise ValueError("--sync-root must be an existing directory")
    snapshot, sha = local_snapshot(Path.cwd()) if args.local else remote_snapshot()
    filenames = validate_snapshot(snapshot)
    # All downloads and validations finish before any public file is changed.
    output.mkdir(parents=True, exist_ok=True)
    for name in filenames:
        write_atomic(output / name, snapshot[name])
    if args.sync_root is not None:
        for name in filenames:
            write_atomic(args.sync_root / name, snapshot[name])
    # Metadata stays outside the artifact and is never in the safe commit list.
    Path("_mirror_files.txt").write_text("\n".join(filenames) + "\n", encoding="utf-8")
    Path("_mirror_sha.txt").write_text(sha + "\n", encoding="ascii")
    print(f"Validated {len(filenames)} public files; canonical snapshot: {sha}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Site artifact failed: {exc}", file=sys.stderr)
        sys.exit(1)
