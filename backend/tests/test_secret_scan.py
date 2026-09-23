from __future__ import annotations

import subprocess
import sys

import scripts.secret_scan as scanner


def write_file(tmp_path, name: str, content: str):  # type: ignore[no-untyped-def]
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def git(tmp_path, *args: str) -> subprocess.CompletedProcess[bytes]:  # type: ignore[no-untyped-def]
    return subprocess.run(
        ["git", *args],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )


def init_repo(tmp_path) -> None:  # type: ignore[no-untyped-def]
    git(tmp_path, "init", "-q")


def test_detects_credential_assignment(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(scanner, "ROOT", tmp_path)
    path = write_file(
        tmp_path,
        "settings.py",
        "api" + "_key=" + "non" + "_placeholder_credential_value\n",
    )

    assert scanner.scan_file(path) == ["settings.py:1: credential-like assignment detected"]


def test_allows_documented_local_placeholder(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(scanner, "ROOT", tmp_path)
    path = write_file(
        tmp_path,
        ".env.example",
        "pass" + "word=" + "change-this-local-password\n",
    )

    assert scanner.scan_file(path) == []


def test_rejects_non_template_environment_file(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(scanner, "ROOT", tmp_path)
    path = write_file(tmp_path, ".env.production", "value\n")

    assert scanner.scan_file(path) == [".env.production: environment files must not be committed"]


def test_detects_private_key_marker(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(scanner, "ROOT", tmp_path)
    path = write_file(
        tmp_path,
        "key.txt",
        "-----BEGIN " + "PRIVATE KEY-----\n",
    )

    assert scanner.scan_file(path) == ["key.txt: private-key material detected"]


def test_staged_scan_reads_index_blob_after_working_copy_is_clean(
    tmp_path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    init_repo(tmp_path)
    path = write_file(
        tmp_path,
        "app.py",
        "api_" + "key = \"EXAMPLE_REVIEW_DUMMY_VALUE_123456789\"\n",
    )
    git(tmp_path, "add", "app.py")
    path.write_text("# working tree no longer contains the staged value\n", encoding="utf-8")
    monkeypatch.setattr(scanner, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["secret_scan.py", "--staged"])

    assert scanner.main() == 1
    captured = capsys.readouterr()
    assert "app.py:1: credential-like assignment detected" in captured.err
    assert "EXAMPLE_REVIEW_DUMMY_VALUE" not in captured.err


def test_staged_env_is_rejected_after_working_copy_is_deleted(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    init_repo(tmp_path)
    path = write_file(tmp_path, ".env.production", "safe-looking-content\n")
    git(tmp_path, "add", ".env.production")
    path.unlink()
    monkeypatch.setattr(scanner, "ROOT", tmp_path)

    paths = scanner.candidate_paths(staged=True)

    assert scanner.scan_index(paths) == [
        ".env.production: environment files must not be committed"
    ]


def test_staged_scan_handles_filename_with_newline(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    init_repo(tmp_path)
    path = write_file(
        tmp_path,
        "line\nbreak.py",
        "api_" + "key = \"EXAMPLE_REVIEW_DUMMY_VALUE_123456789\"\n",
    )
    git(tmp_path, "add", path.name)
    path.write_text("# clean working tree\n", encoding="utf-8")
    monkeypatch.setattr(scanner, "ROOT", tmp_path)

    issues = scanner.scan_index(scanner.candidate_paths(staged=True))

    assert len(issues) == 1
    assert issues[0].endswith(":1: credential-like assignment detected")


def test_all_scans_tracked_index_only(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    init_repo(tmp_path)
    write_file(tmp_path, "tracked.py", "# safe\n")
    write_file(
        tmp_path,
        "untracked.py",
        "api_" + "key = \"EXAMPLE_REVIEW_DUMMY_VALUE_123456789\"\n",
    )
    git(tmp_path, "add", "tracked.py")
    monkeypatch.setattr(scanner, "ROOT", tmp_path)

    paths = scanner.candidate_paths(staged=False)

    assert [path.name for path in paths] == ["tracked.py"]
    assert scanner.scan_index(paths) == []


def test_binary_and_oversized_staged_blobs_fail_closed(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    init_repo(tmp_path)
    binary = tmp_path / "credentials.dat"
    binary.write_bytes(b"\x00\xff")
    oversized = tmp_path / "credentials.txt"
    oversized.write_bytes(b"x" * (scanner.MAX_FILE_BYTES + 1))
    git(tmp_path, "add", "credentials.dat", "credentials.txt")
    monkeypatch.setattr(scanner, "ROOT", tmp_path)

    issues = scanner.scan_index(scanner.candidate_paths(staged=True))

    assert "credentials.dat: binary content cannot be safely secret-scanned" in issues
    assert "credentials.txt: file exceeds the secret-scan size limit" in issues
