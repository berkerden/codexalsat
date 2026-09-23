#!/usr/bin/env python3
"""Small, dependency-free guard against committing credentials and private keys."""

from __future__ import annotations

import argparse
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 1_000_000
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "data",
    "logs",
    "dist",
    "build",
    "__pycache__",
}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".parquet", ".zip"}
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")
ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|api[_-]?secret|secret|password|token|access[_-]?key)\b\s*[:=]\s*[\"']?([^\s\"']{16,})"
)
BINANCE_KEY = re.compile(r"\b[A-Za-z0-9]{64}\b")


class IndexEntry(NamedTuple):
    mode: bytes
    object_id: bytes


def entropy(value: str) -> float:
    frequencies = {char: value.count(char) / len(value) for char in set(value)}
    return -sum(probability * math.log2(probability) for probability in frequencies.values())


def _git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )


def _decode_paths(output: bytes) -> list[Path]:
    return [ROOT / os.fsdecode(name) for name in output.split(b"\0") if name]


def candidate_paths(staged: bool) -> list[Path]:
    """Return index paths in Git repositories, or safe filesystem paths as a fallback."""
    args = (
        ("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
        if staged
        else ("ls-files", "-z")
    )
    result = _git(*args)
    if result.returncode == 0:
        return _decode_paths(result.stdout)
    return [
        path
        for path in ROOT.rglob("*")
        if path.is_file() and not any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts)
    ]


def should_scan(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    return not (
        any(part in SKIP_DIRS for part in relative.parts)
        or path.suffix.lower() in SKIP_SUFFIXES
    )


def _display_path(relative: Path) -> str:
    return relative.as_posix().encode("utf-8", "backslashreplace").decode("utf-8")


def scan_content(relative: Path, content: bytes) -> list[str]:
    """Scan file bytes without ever including a credential value in diagnostics."""
    display = _display_path(relative)
    if relative.name.startswith(".env") and relative.name != ".env.example":
        return [f"{display}: environment files must not be committed"]
    if len(content) > MAX_FILE_BYTES:
        return [f"{display}: file exceeds the secret-scan size limit"]
    if b"\0" in content:
        return [f"{display}: binary content cannot be safely secret-scanned"]
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return [f"{display}: non-UTF-8 content cannot be safely secret-scanned"]

    issues: list[str] = []
    if PRIVATE_KEY.search(text):
        issues.append(f"{display}: private-key material detected")
    for number, line in enumerate(text.splitlines(), start=1):
        match = ASSIGNMENT.search(line)
        if match:
            value = match.group(1)
            if value.lower() not in {
                "changeme",
                "change-this-local-password",
                "example",
                "placeholder",
            }:
                issues.append(f"{display}:{number}: credential-like assignment detected")
        for token in BINANCE_KEY.findall(line):
            if entropy(token) >= 4.5:
                issues.append(f"{display}:{number}: high-entropy 64-character token detected")
    return issues


def scan_file(path: Path) -> list[str]:
    relative = path.relative_to(ROOT)
    entry = _index_entries().get(_relative_bytes(path))
    if entry is not None:
        return _scan_index_entry(relative, entry)
    try:
        content = path.read_bytes()
    except OSError:
        return [f"{_display_path(relative)}: file could not be read for secret scanning"]
    return scan_content(relative, content)


def _index_entries() -> dict[bytes, IndexEntry]:
    result = _git("ls-files", "--stage", "-z")
    if result.returncode != 0:
        return {}

    entries: dict[bytes, IndexEntry] = {}
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        metadata, separator, name = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or fields[2] != b"0":
            continue
        entries[name] = IndexEntry(mode=fields[0], object_id=fields[1])
    return entries


def _relative_bytes(path: Path) -> bytes:
    return os.fsencode(path.relative_to(ROOT).as_posix())


def _scan_index_entry(relative: Path, entry: IndexEntry) -> list[str]:
    display = _display_path(relative)
    if relative.name.startswith(".env") and relative.name != ".env.example":
        return [f"{display}: environment files must not be committed"]
    if entry.mode == b"160000":  # A gitlink has no file blob to scan.
        return []

    object_id = os.fsdecode(entry.object_id)
    size_result = _git("cat-file", "-s", object_id)
    try:
        size = int(size_result.stdout.strip()) if size_result.returncode == 0 else -1
    except ValueError:
        size = -1
    if size < 0:
        return [f"{display}: staged content could not be read for secret scanning"]
    if size > MAX_FILE_BYTES:
        return [f"{display}: file exceeds the secret-scan size limit"]

    blob_result = _git("cat-file", "blob", object_id)
    if blob_result.returncode != 0:
        return [f"{display}: staged content could not be read for secret scanning"]
    return scan_content(relative, blob_result.stdout)


def scan_index(paths: list[Path]) -> list[str]:
    """Scan exact blobs recorded in the index, independent of working-tree contents."""
    entries = _index_entries()
    issues: list[str] = []
    for path in paths:
        if not should_scan(path):
            continue
        relative = path.relative_to(ROOT)
        entry = entries.get(_relative_bytes(path))
        if entry is None:
            display = _display_path(relative)
            issues.append(f"{display}: staged content could not be read for secret scanning")
            continue
        issues.extend(_scan_index_entry(relative, entry))
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged", action="store_true", help="Scan the exact Git staged blobs")
    parser.add_argument("--all", action="store_true", help="Scan all Git-tracked files")
    args = parser.parse_args()

    paths = candidate_paths(args.staged and not args.all)
    git_index_available = _git("ls-files", "--stage", "-z").returncode == 0
    if git_index_available:
        issues = scan_index(paths)
    else:
        issues = [issue for path in paths if should_scan(path) for issue in scan_file(path)]
    if issues:
        print("Secret scan failed:", *issues, sep="\n  ", file=sys.stderr)
        return 1
    print("Secret scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
