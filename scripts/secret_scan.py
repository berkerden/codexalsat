#!/usr/bin/env python3
"""Small, dependency-free guard against committing credentials and private keys."""

from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".parquet", ".zip"}
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")
ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|api[_-]?secret|secret|password|token|access[_-]?key)\b\s*[:=]\s*[\"']?([^\s\"']{16,})"
)
BINANCE_KEY = re.compile(r"\b[A-Za-z0-9]{64}\b")


def entropy(value: str) -> float:
    frequencies = {char: value.count(char) / len(value) for char in set(value)}
    return -sum(probability * math.log2(probability) for probability in frequencies.values())


def candidate_paths(staged: bool) -> list[Path]:
    if staged:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return [ROOT / line for line in result.stdout.splitlines()]
    return [path for path in ROOT.rglob("*") if path.is_file()]


def should_scan(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    if any(part in SKIP_DIRS for part in relative.parts) or path.suffix.lower() in SKIP_SUFFIXES:
        return False
    if path.stat().st_size > 1_000_000:
        return False
    return True


def scan_file(path: Path) -> list[str]:
    relative = path.relative_to(ROOT)
    issues: list[str] = []
    if relative.name.startswith(".env") and relative.name != ".env.example":
        return [f"{relative}: environment files must not be committed"]
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return issues
    if PRIVATE_KEY.search(content):
        issues.append(f"{relative}: private-key material detected")
    for number, line in enumerate(content.splitlines(), start=1):
        match = ASSIGNMENT.search(line)
        if match:
            value = match.group(1)
            if value.lower() not in {"changeme", "change-this-local-password", "example", "placeholder"}:
                issues.append(f"{relative}:{number}: credential-like assignment detected")
        for token in BINANCE_KEY.findall(line):
            if entropy(token) >= 4.5:
                issues.append(f"{relative}:{number}: high-entropy 64-character token detected")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged", action="store_true", help="Scan Git staged files when available")
    parser.add_argument("--all", action="store_true", help="Scan all project files")
    args = parser.parse_args()
    paths = candidate_paths(args.staged and not args.all)
    issues = [issue for path in paths if path.exists() and should_scan(path) for issue in scan_file(path)]
    if issues:
        print("Secret scan failed:", *issues, sep="\n  ", file=sys.stderr)
        return 1
    print("Secret scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
