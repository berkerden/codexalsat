from pathlib import Path
from subprocess import CalledProcessError

import pytest

from spotlab import main


def test_version_prefers_checkout_revision(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    checkout = "a" * 40
    (tmp_path / "BUILD_REVISION").write_text("b" * 40, encoding="ascii")
    monkeypatch.setattr(main, "BUILD_REVISION_PATH", tmp_path / "BUILD_REVISION")
    monkeypatch.setattr(main.subprocess, "check_output", lambda *args, **kwargs: checkout)
    assert main.version() == checkout


@pytest.mark.parametrize("contents", ["not-a-revision", "c" * 39, "é" * 40])
def test_version_rejects_invalid_image_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contents: str
) -> None:
    revision = tmp_path / "BUILD_REVISION"
    revision.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(main, "BUILD_REVISION_PATH", revision)
    monkeypatch.setattr(
        main.subprocess,
        "check_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(CalledProcessError(1, "git")),
    )
    assert main.version() == "uncommitted"


def test_version_uses_immutable_image_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected = "d" * 40
    revision = tmp_path / "BUILD_REVISION"
    revision.write_text(expected, encoding="ascii")
    monkeypatch.setattr(main, "BUILD_REVISION_PATH", revision)
    monkeypatch.setattr(
        main.subprocess,
        "check_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("git unavailable")),
    )
    assert main.version() == expected
