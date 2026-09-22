"""Package and restore a read-only Docs snapshot without third-party dependencies."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def child_path(root, relative):
    if not relative or "\\" in relative or ":" in relative:
        raise ValueError(f"Invalid archive path: {relative}")
    path = (root / relative).resolve()
    if path == root.resolve() or not path.is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes archive root: {relative}")
    return path


class SplitWriter(io.RawIOBase):
    def __init__(self, directory, limit):
        self.directory = directory
        self.limit = limit
        self.parts = []
        self.current = None
        self.current_size = 0

    def writable(self):
        return True

    def write(self, data):
        remaining = memoryview(data)
        while remaining:
            if self.current is None or self.current_size == self.limit:
                if self.current is not None:
                    self.current.close()
                path = self.directory / f"docs.tar.gz.part{len(self.parts):04d}"
                self.current = path.open("xb")
                self.parts.append(path)
                self.current_size = 0
            count = min(len(remaining), self.limit - self.current_size)
            self.current.write(remaining[:count])
            self.current_size += count
            remaining = remaining[count:]
        return len(data)

    def close(self):
        if self.current is not None:
            self.current.close()
        super().close()


def pack(source, output, part_mib):
    source = source.resolve()
    output = output.resolve()
    if not source.is_dir() or output.is_relative_to(source):
        raise ValueError("Source must exist and output must be outside the source tree")
    if output.exists():
        raise FileExistsError(f"Archive destination already exists: {output}")
    paths = sorted(source.rglob("*"))
    for path in paths:
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError(f"Refusing linked source path: {path}")
    files = [path for path in paths if path.is_file()]
    output.mkdir(parents=True)
    inventory = []
    with SplitWriter(output, part_mib * 1024 * 1024) as writer:
        with tarfile.open(fileobj=writer, mode="w|gz", format=tarfile.PAX_FORMAT) as archive:
            for number, path in enumerate(files, 1):
                relative = path.relative_to(source).as_posix()
                child_path(source, relative)
                stat = path.stat()
                checksum = file_hash(path)
                archive.add(path, arcname=relative, recursive=False)
                after = path.stat()
                if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise RuntimeError(f"Source changed while packaging: {relative}")
                inventory.append({"path": relative, "bytes": stat.st_size, "sha256": checksum})
                if number % 1000 == 0:
                    print(f"Packaged {number}/{len(files)} files", flush=True)
        parts = writer.parts
    manifest = {
        "format": "rdna-docs-tar-gzip-parts-v1",
        "parts": [{"path": path.name, "bytes": path.stat().st_size, "sha256": file_hash(path)} for path in parts],
        "files": inventory,
    }
    with (output / "manifest.json").open("x", encoding="utf-8") as target:
        json.dump(manifest, target, indent=2)
        target.write("\n")
    print(f"Packed {len(files)} files into {len(parts)} parts; original documents unchanged.")


def load_manifest(archive_root):
    manifest = json.loads((archive_root / "manifest.json").read_text(encoding="utf-8"))
    if manifest["format"] != "rdna-docs-tar-gzip-parts-v1":
        raise ValueError("Unsupported Docs archive format")
    for part in manifest["parts"]:
        path = child_path(archive_root, part["path"])
        if path.stat().st_size != part["bytes"] or file_hash(path) != part["sha256"]:
            raise ValueError(f"Archive part failed verification: {part['path']}")
    return manifest


def verify_tree(destination, files):
    expected = {entry["path"] for entry in files}
    actual = {path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()}
    if actual != expected:
        raise ValueError("Destination has missing or extra files; no files will be overwritten")
    for entry in files:
        path = child_path(destination, entry["path"])
        if path.stat().st_size != entry["bytes"] or file_hash(path) != entry["sha256"]:
            raise ValueError(f"Destination differs: {entry['path']}; no files will be overwritten")
    print(f"Verified {len(files)} restored documentation files.")


def restore(archive_root, destination, verify_only=False):
    archive_root = archive_root.resolve()
    destination = destination.resolve()
    manifest = load_manifest(archive_root)
    if verify_only or destination.exists():
        if not destination.is_dir():
            raise FileNotFoundError(f"Docs destination does not exist: {destination}")
        verify_tree(destination, manifest["files"])
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected = {entry["path"]: entry for entry in manifest["files"]}
    if len(expected) != len(manifest["files"]):
        raise ValueError("Duplicate inventory paths")
    for relative in expected:
        child_path(destination, relative)
    with tempfile.TemporaryDirectory(prefix=".docs-restore-", dir=destination.parent) as temporary:
        temporary = Path(temporary)
        combined = temporary / "docs.tar.gz"
        with combined.open("xb") as target:
            for part in manifest["parts"]:
                with child_path(archive_root, part["path"]).open("rb") as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        target.write(block)
        extracted = temporary / "Docs"
        extracted.mkdir()
        seen = set()
        with tarfile.open(combined, mode="r|gz") as archive:
            for member in archive:
                if not member.isfile() or member.name not in expected or member.name in seen:
                    raise ValueError(f"Unexpected archive member: {member.name}")
                entry = expected[member.name]
                if member.size != entry["bytes"]:
                    raise ValueError(f"Incorrect member size: {member.name}")
                target = child_path(extracted, member.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with archive.extractfile(member) as source, target.open("xb") as output:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(block)
                        output.write(block)
                if digest.hexdigest() != entry["sha256"]:
                    raise ValueError(f"Incorrect member hash: {member.name}")
                os.utime(target, (member.mtime, member.mtime))
                seen.add(member.name)
        if seen != set(expected):
            raise ValueError("Archive is missing files")
        if destination.exists():
            raise FileExistsError(f"Destination appeared during extraction: {destination}")
        extracted.rename(destination)
    print(f"Restored and verified {len(seen)} documentation files to {destination}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    package = commands.add_parser("pack")
    package.add_argument("--source", required=True, type=Path)
    package.add_argument("--output", required=True, type=Path)
    package.add_argument("--part-mib", type=int, default=90, choices=range(1, 96))
    for command in ("restore", "verify"):
        action = commands.add_parser(command)
        action.add_argument("--archive", required=True, type=Path)
        action.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "pack":
        pack(args.source, args.output, args.part_mib)
    else:
        restore(args.archive, args.destination, args.command == "verify")


if __name__ == "__main__":
    main()