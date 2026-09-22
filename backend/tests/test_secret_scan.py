from __future__ import annotations

import scripts.secret_scan as scanner


def write_file(tmp_path, name: str, content: str):  # type: ignore[no-untyped-def]
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


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
