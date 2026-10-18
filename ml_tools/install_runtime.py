"""Download pinned public files, or verify an existing transfer without network access."""
import argparse
import concurrent.futures
import hashlib
import json
import re
import shutil
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCK = PROJECT_ROOT / "ml_tools/model35-lock.json"


def _filename(value):
    return (isinstance(value, str) and value not in {"", ".", ".."}
            and not any(c in value for c in '/\\:<>"|?*')
            and not value.endswith((".", " ")))


def _manifest(lock):
    manifest = json.loads(lock.read_text(encoding="utf-8-sig"))
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("Lock file must contain a nonempty files list")
    names = set()
    for item in files:
        if not _filename(item.get("name")):
            raise ValueError("Lock file contains an invalid filename")
        if item.get("kind") not in {"model", "runtime"}:
            raise ValueError("Lock file contains an invalid file kind")
        if not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")):
            raise ValueError("Lock file must contain a SHA-256 for every file")
        url = urllib.parse.urlsplit(item.get("url", ""))
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("Lock downloads require public HTTPS URLs without credentials")
        name = (item["kind"], item["name"].casefold())
        if name in names:
            raise ValueError("Lock file contains a duplicate filename")
        names.add(name)
    return manifest


def _sha256(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _target(destination, item):
    return destination / ("downloads" if item["kind"] == "runtime" else "models") / item["name"]


def _verify(path, expected):
    if _sha256(path) != expected:
        raise ValueError("Checksum mismatch: " + path.name)


def _runtime_members(files):
    """Inspect every archive before writing flattened Windows runtime files."""
    members = {}
    for path, item in files:
        if item["kind"] != "runtime":
            continue
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                name = PurePosixPath(member.filename.replace("\\", "/"))
                if (name.is_absolute() or ".." in name.parts or not _filename(name.name)
                        or (member.external_attr >> 16) & 0o170000 == 0o120000):
                    raise ValueError("Invalid runtime archive filename: " + member.filename)
                with archive.open(member) as source:
                    digest = hashlib.file_digest(source, "sha256").hexdigest()
                key = name.name.casefold()
                previous = members.get(key)
                if previous and previous[3] != digest:
                    raise ValueError("Duplicate runtime filename: " + name.name)
                members[key] = (path, member.filename, name.name, digest)
    return list(members.values())


def verify_installed(lock: Path, destination: Path):
    """No downloads, directory creation, extraction, or marker changes."""
    manifest = _manifest(lock)
    files = [(_target(destination, item), item) for item in manifest["files"]]
    for path, item in files:
        _verify(path, item["sha256"])
    for _, _, name, digest in _runtime_members(files):
        if _sha256(destination / "llama" / name) != digest:
            raise ValueError("Runtime checksum mismatch: " + name)
    return manifest


def install(lock: Path, destination: Path):
    manifest = _manifest(lock)
    destination.mkdir(parents=True, exist_ok=True)

    def download(item):
        target = _target(destination, item)
        target.parent.mkdir(exist_ok=True)
        if not target.exists():
            partial = target.with_name(target.name + "." + uuid.uuid4().hex + ".part")
            try:
                with urllib.request.urlopen(item["url"], timeout=90) as response, partial.open("xb") as output:
                    shutil.copyfileobj(response, output, 1024 * 1024)
                if _sha256(partial) != item["sha256"]:
                    raise ValueError("Downloaded checksum mismatch: " + item["name"])
                partial.replace(target)
            finally:
                partial.unlink(missing_ok=True)
        _verify(target, item["sha256"])
        print("Verified: " + item["name"], flush=True)
        return target, item

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        files = list(pool.map(download, manifest["files"]))
    members = _runtime_members(files)
    folder = destination / "llama"
    folder.mkdir(exist_ok=True)
    for path, archive_name, name, digest in members:
        target = folder / name
        if target.exists() and _sha256(target) == digest:
            continue
        partial = target.with_name(target.name + "." + uuid.uuid4().hex + ".part")
        try:
            with zipfile.ZipFile(path) as archive, archive.open(archive_name) as source, partial.open("xb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            _verify(partial, digest)
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
    (destination / ("installed-" + lock.name)).write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--destination", type=Path, default=PROJECT_ROOT / ".local_ml")
    parser.add_argument("--verify-only", action="store_true", help="Verify transferred files offline without changing them")
    args = parser.parse_args()
    if args.verify_only:
        verify_installed(args.lock, args.destination)
        print("Offline verification passed: public weights, archives and extracted runtime match the lock.")
    else:
        install(args.lock, args.destination)
