"""Synthetic-only, opt-in backup transfer drill across two CI jobs.

The source and restore phases intentionally share no database and accept no
operator-selected paths or URLs.  Only the four fixed files below may cross
the job boundary; the manifest digest is carried independently as a job
output.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import Mock

import pytest
from backend.tests.test_postgres_recovery import (
    _ADMIN_URL,
    _AT,
    _UID,
    TESTNET_ACCOUNT_VENUE,
    AccountBindingError,
    OrderState,
    OrderStore,
    Panel,
    _assert_identical,
    _create_database,
    _database_url,
    _drop_created,
    _raw_paper_state,
    _restore,
    _run_dump,
    _schema_and_rows,
    _seed_orders,
    _seed_paper,
    _verify_same_pg16,
)
from sqlalchemy import create_engine, text

from spotlab.engine import PaperEngine

_OPT_IN = "SPOTLAB_REQUIRE_BACKUP_TRANSFER"
_PHASE = "SPOTLAB_TRANSFER_PHASE"
_TRUSTED_MANIFEST = "SPOTLAB_TRANSFER_MANIFEST_SHA256"
_BUNDLE_DIRECTORY = "spotlab-synthetic-transfer"
_PG_ARCHIVE = "paper.pgcustom"
_SQLITE_ARCHIVE = "testnet.sqlite"
_EXPECTED = "expected.json"
_MANIFEST = "manifest.json"
_FILES = frozenset({_PG_ARCHIVE, _SQLITE_ARCHIVE, _EXPECTED, _MANIFEST})
_PAYLOAD_FILES = (_EXPECTED, _PG_ARCHIVE, _SQLITE_ARCHIVE)
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024
_MIN_FILE_BYTES = {_PG_ARCHIVE: 5, _SQLITE_ARCHIVE: 512, _EXPECTED: 2}
_SAFE_CONTAINER = re.compile(r"^[a-f0-9]{12,64}$")


class TransferRejected(ValueError):
    """The transferred artifact is not the exact synthetic bundle expected."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as payload:
        for block in iter(lambda: payload.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical(value: object) -> object:
    """Return a stable, lossless-enough JSON image for schema/row comparison."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return {"$float": repr(value)}
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return {"$type": type(value).__name__, "$value": str(value)}


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o400)


def _strict_json(path: Path) -> dict[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise TransferRejected(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise TransferRejected(f"invalid {path.name}") from error
    if not isinstance(value, dict):
        raise TransferRejected(f"invalid {path.name}")
    return value


def _identity_from_environment() -> dict[str, object]:
    run_id = os.getenv("GITHUB_RUN_ID", "")
    run_attempt = os.getenv("GITHUB_RUN_ATTEMPT", "")
    commit = os.getenv("GITHUB_SHA", "")
    if not run_id.isdecimal() or not run_attempt.isdecimal():
        pytest.fail("GitHub run identity is required for the transfer drill")
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        pytest.fail("GITHUB_SHA must be a lowercase 40-character commit hash")
    return {
        "schema": 1,
        "synthetic_only": True,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "commit": commit,
    }


def _bundle_path() -> Path:
    runner_temp = os.getenv("RUNNER_TEMP")
    if not runner_temp or not Path(runner_temp).is_absolute():
        pytest.fail("RUNNER_TEMP must be an absolute CI temporary directory")
    return Path(runner_temp) / _BUNDLE_DIRECTORY


def _required_transfer_container() -> str:
    container = os.getenv("TEST_POSTGRES_RECOVERY_CONTAINER")
    if not container or not _SAFE_CONTAINER.fullmatch(container):
        pytest.fail(
            "TEST_POSTGRES_RECOVERY_CONTAINER must be the dedicated service's "
            "hexadecimal container ID"
        )
    return container


def _regular_file(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise TransferRejected(f"missing bundle file: {path.name}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise TransferRejected(f"bundle member is not a regular file: {path.name}")
    return info


def _validate_bundle(
    bundle: Path, trusted_manifest_sha256: str, expected_identity: Mapping[str, object]
) -> dict[str, object]:
    """Validate the complete closed bundle without opening any restore target."""
    if len(trusted_manifest_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in trusted_manifest_sha256
    ):
        raise TransferRejected("invalid trusted manifest digest")
    try:
        bundle_info = bundle.lstat()
        names = {entry.name for entry in bundle.iterdir()}
    except OSError as error:
        raise TransferRejected("bundle directory is unavailable") from error
    if stat.S_ISLNK(bundle_info.st_mode) or not stat.S_ISDIR(bundle_info.st_mode):
        raise TransferRejected("bundle path is not a real directory")
    if names != _FILES:
        raise TransferRejected("bundle file list is not allowlisted and complete")
    for name in _FILES:
        info = _regular_file(bundle / name)
        if name == _MANIFEST and not (2 <= info.st_size <= _MAX_MANIFEST_BYTES):
            raise TransferRejected("unsafe manifest size")
    if _sha256(bundle / _MANIFEST) != trusted_manifest_sha256:
        raise TransferRejected("manifest digest does not match trusted job output")

    manifest = _strict_json(bundle / _MANIFEST)
    if set(manifest) != {*expected_identity, "files"}:
        raise TransferRejected("manifest fields are not allowlisted and complete")
    for key, value in expected_identity.items():
        actual = manifest.get(key)
        if type(actual) is not type(value) or actual != value:
            raise TransferRejected("manifest job identity mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(_PAYLOAD_FILES):
        raise TransferRejected("manifest payload list is not allowlisted and complete")
    for name in _PAYLOAD_FILES:
        record = files.get(name)
        if not isinstance(record, dict) or set(record) != {"sha256", "size"}:
            raise TransferRejected(f"invalid manifest record for {name}")
        size = record.get("size")
        digest = record.get("sha256")
        if not isinstance(size, int) or isinstance(size, bool):
            raise TransferRejected(f"invalid size for {name}")
        if size < _MIN_FILE_BYTES[name] or size > _MAX_FILE_BYTES:
            raise TransferRejected(f"unsafe size for {name}")
        if not isinstance(digest, str) or len(digest) != 64:
            raise TransferRejected(f"invalid digest for {name}")
        if any(char not in "0123456789abcdef" for char in digest):
            raise TransferRejected(f"invalid digest for {name}")
        info = _regular_file(bundle / name)
        if info.st_size != size or _sha256(bundle / name) != digest:
            raise TransferRejected(f"payload verification failed for {name}")
    expected = _strict_json(bundle / _EXPECTED)
    if set(expected) != {"paper", "paper_archive", "paper_raw", "testnet"}:
        raise TransferRejected("expected-state fields are not allowlisted and complete")
    return expected


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    os.chmod(destination, 0o400)


def _restore_sqlite_exclusive(snapshot: Path, target: Path) -> None:
    """Restore to a newly created empty file and never replace an existing path."""
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    source = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    except BaseException:
        destination.close()
        source.close()
        target.unlink(missing_ok=True)
        raise
    else:
        destination.close()
        source.close()


def _source_phase(bundle: Path, container: str, identity: dict[str, object]) -> None:
    try:
        bundle.mkdir(mode=0o700)
    except FileExistsError:
        pytest.fail("source bundle directory must not already exist")
    fixture_id = uuid.uuid4().hex[:24]
    source_name = f"spotlab_recovery_{fixture_id}_source"
    source_url = _database_url(source_name)
    admin = create_engine(_ADMIN_URL)
    created: list[str] = []
    sqlite_source = bundle.parent / f"spotlab-transfer-source-{fixture_id}.sqlite"
    try:
        _verify_same_pg16(container, admin)
        _create_database(admin, source_name, created)
        active, archived = _seed_paper(source_url)
        _seed_orders(f"sqlite:///{sqlite_source}")

        paper_engine = create_engine(source_url)
        sqlite_engine = create_engine(f"sqlite:///{sqlite_source}")
        try:
            expected = {
                "paper": _canonical(_schema_and_rows(paper_engine)),
                "paper_archive": _canonical(archived),
                "paper_raw": _canonical(_raw_paper_state(source_url)),
                "testnet": _canonical(_schema_and_rows(sqlite_engine)),
            }
        finally:
            paper_engine.dispose()
            sqlite_engine.dispose()
        assert active["session_id"] == expected["paper_raw"]["session_id"]

        _run_dump(container, source_name, bundle / _PG_ARCHIVE)
        os.chmod(bundle / _PG_ARCHIVE, 0o400)
        _sqlite_snapshot(sqlite_source, bundle / _SQLITE_ARCHIVE)
        _write_json(bundle / _EXPECTED, expected)
        manifest = {
            **identity,
            "files": {
                name: {"sha256": _sha256(bundle / name), "size": (bundle / name).stat().st_size}
                for name in _PAYLOAD_FILES
            },
        }
        _write_json(bundle / _MANIFEST, manifest)
        manifest_sha256 = _sha256(bundle / _MANIFEST)
        # Self-validation makes the producer obey the same closed-world contract.
        _validate_bundle(bundle, manifest_sha256, identity)
        github_output = os.getenv("GITHUB_OUTPUT")
        if not github_output:
            pytest.fail("GITHUB_OUTPUT is required in the source transfer phase")
        with Path(github_output).open("a", encoding="utf-8") as output:
            output.write(f"manifest_sha256={manifest_sha256}\n")
    finally:
        sqlite_source.unlink(missing_ok=True)
        try:
            _drop_created(admin, created)
        finally:
            admin.dispose()


def _assert_paper_restored(url: str, expected: Mapping[str, object]) -> None:
    assert _canonical(_raw_paper_state(url)) == expected["paper_raw"]
    paper = PaperEngine(url)
    try:
        restarted = paper.snapshot()
        raw_after = _canonical(_raw_paper_state(url))
        assert not restarted["entries_enabled"]
        assert isinstance(raw_after, dict)
        expected_raw = copy.deepcopy(expected["paper_raw"])
        assert isinstance(expected_raw, dict)
        before_audit = expected_raw["audit"]
        after_audit = raw_after["audit"]
        assert isinstance(before_audit, list) and isinstance(after_audit, list)
        assert after_audit[:-1] == before_audit
        assert after_audit[-1]["event"] == "restart"
        expected_raw["entries_enabled"] = False
        expected_raw["reason"] = raw_after["reason"]
        expected_raw["audit"] = after_audit
        observation = expected_raw["observation"]
        assert isinstance(observation, dict)
        interruptions = observation["interruptions"]
        assert isinstance(interruptions, dict)
        interruptions["restart"] += 1
        observation["continuity"] = None
        assert raw_after == expected_raw
        archived = expected["paper_archive"]
        assert isinstance(archived, dict)
        archived_state = archived.get("state")
        assert isinstance(archived_state, dict)
        session_id = archived_state.get("session_id")
        assert isinstance(session_id, str)
        assert _canonical(paper.archive_detail(session_id)) == archived
        assert paper.archives()["total"] == 1
    finally:
        paper.db.dispose()


def _assert_testnet_restored(url: str, target: Path, expected: Mapping[str, object]) -> None:
    engine = create_engine(url)
    try:
        assert _canonical(_schema_and_rows(engine)) == expected["testnet"]
        pristine = _schema_and_rows(engine)
    finally:
        engine.dispose()
    unchanged = OrderStore(url, recover_submitting=False)
    try:
        assert unchanged.get("pending-oco").state is OrderState.SUBMITTING
        unchanged.require_account_binding(TESTNET_ACCOUNT_VENUE, _UID)
        with pytest.raises(AccountBindingError, match="account_binding_mismatch"):
            unchanged.require_account_binding(TESTNET_ACCOUNT_VENUE, _UID + 1)
        assert unchanged.account_binding_reason() == "account_history_missing"
    finally:
        unchanged.db.dispose()
    engine = create_engine(url)
    try:
        _assert_identical(pristine, _schema_and_rows(engine))
    finally:
        engine.dispose()
    recovered = OrderStore(url)
    try:
        assert recovered.get("pending-oco").state is OrderState.UNKNOWN
        assert recovered.account_binding_reason() == "account_history_missing"
    finally:
        recovered.db.dispose()

    engine = create_engine(url)
    try:
        recovered_image = _schema_and_rows(engine)
    finally:
        engine.dispose()
    for table, rows in pristine["rows"].items():
        if table != "order_intents":
            assert recovered_image["rows"][table] == rows
    assert recovered_image["schema"] == pristine["schema"]
    columns = [item[0] for item in pristine["schema"]["order_intents"]["columns"]]
    before_rows = {
        row[columns.index("intent_id")]: row for row in pristine["rows"]["order_intents"]
    }
    after_rows = {
        row[columns.index("intent_id")]: row for row in recovered_image["rows"]["order_intents"]
    }
    assert after_rows.keys() == before_rows.keys()
    assert after_rows["filled-buy"] == before_rows["filled-buy"]
    permitted = {"state", "error", "updated_at"}
    for index, column in enumerate(columns):
        if column not in permitted:
            assert after_rows["pending-oco"][index] == before_rows["pending-oco"][index]
    assert after_rows["pending-oco"][columns.index("state")] == "UNKNOWN"
    assert (
        after_rows["pending-oco"][columns.index("error")]
        == "process restarted during submission; exchange query required"
    )

    before_panel = create_engine(url)
    try:
        panel_pristine = _schema_and_rows(before_panel)
    finally:
        before_panel.dispose()
    panel = Panel(url, target.parent / "credentials-must-not-be-read.json")
    panel.credentials_loader = Mock(side_effect=AssertionError("no credentials or exchange I/O"))
    panel.transport_factory = Mock(side_effect=AssertionError("no exchange transport"))
    status_value = asyncio.run(panel.status())
    assert status_value["ledger_status"] == "ready"
    assert status_value["cycles"] == [
        {
            "cycle_id": "recovery-cycle",
            "symbol": "BTCUSDT",
            "state": "PROTECTION_UNVERIFIED",
            "stop_entry_requested": False,
            "updated_at_ms": _AT,
        }
    ]
    assert status_value["watcher"]["phase"] == "idle"
    assert not status_value["watcher"]["allow_mutations"]
    assert not status_value["watcher"]["manager_running"]
    panel.credentials_loader.assert_not_called()
    panel.transport_factory.assert_not_called()
    after_panel = create_engine(url)
    try:
        _assert_identical(panel_pristine, _schema_and_rows(after_panel))
    finally:
        after_panel.dispose()


def _restore_phase(
    bundle: Path, container: str, identity: dict[str, object], trusted_digest: str
) -> None:
    # This must complete before creating either target.
    expected = _validate_bundle(bundle, trusted_digest, identity)
    fixture_id = uuid.uuid4().hex[:24]
    target_name = f"spotlab_recovery_{fixture_id}_target"
    guard_name = f"spotlab_recovery_{fixture_id}_guard"
    target_url = _database_url(target_name)
    sqlite_target = bundle.parent / f"spotlab-transfer-target-{fixture_id}.sqlite"
    admin = create_engine(_ADMIN_URL)
    created: list[str] = []
    try:
        _verify_same_pg16(container, admin)
        _create_database(admin, target_name, created)
        _create_database(admin, guard_name, created)
        guard_url = _database_url(guard_name)
        guard = create_engine(guard_url)
        try:
            with guard.begin() as connection:
                connection.execute(text("CREATE TABLE recovery_sentinel (value TEXT NOT NULL)"))
                connection.execute(text("INSERT INTO recovery_sentinel VALUES ('preserve')"))
            with pytest.raises(ValueError, match="empty database"):
                _restore(container, guard_url, guard_name, bundle / _PG_ARCHIVE)
            with guard.connect() as connection:
                value = connection.execute(text("SELECT value FROM recovery_sentinel")).scalar_one()
                assert value == "preserve"
        finally:
            guard.dispose()
        restored = _restore(container, target_url, target_name, bundle / _PG_ARCHIVE)
        assert restored.returncode == 0, restored.stderr.decode(errors="replace")
        paper_engine = create_engine(target_url)
        try:
            assert _canonical(_schema_and_rows(paper_engine)) == expected["paper"]
        finally:
            paper_engine.dispose()
        _assert_paper_restored(target_url, expected)

        _restore_sqlite_exclusive(bundle / _SQLITE_ARCHIVE, sqlite_target)
        _assert_testnet_restored(f"sqlite:///{sqlite_target}", sqlite_target, expected)
    finally:
        sqlite_target.unlink(missing_ok=True)
        try:
            _drop_created(admin, created)
        finally:
            admin.dispose()


def test_transfer_validator_rejects_missing_tampered_truncated_identity_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = {
        "schema": 1,
        "synthetic_only": True,
        "run_id": "10",
        "run_attempt": "2",
        "commit": "a" * 40,
    }
    expected = {"paper": {}, "paper_archive": {}, "paper_raw": {}, "testnet": {}}

    def make_bundle(root: Path) -> tuple[Path, str]:
        root.mkdir()
        (root / _PG_ARCHIVE).write_bytes(b"PGDMP synthetic")
        database = sqlite3.connect(root / _SQLITE_ARCHIVE)
        database.execute("CREATE TABLE synthetic (id INTEGER PRIMARY KEY)")
        database.commit()
        database.close()
        _write_json(root / _EXPECTED, expected)
        _write_json(
            root / _MANIFEST,
            {
                **identity,
                "files": {
                    name: {"sha256": _sha256(root / name), "size": (root / name).stat().st_size}
                    for name in _PAYLOAD_FILES
                },
            },
        )
        return root, _sha256(root / _MANIFEST)

    good, trusted = make_bundle(tmp_path / "good")
    assert _validate_bundle(good, trusted, identity) == expected
    cases: list[tuple[Path, str, Mapping[str, object]]] = []
    missing, digest = make_bundle(tmp_path / "missing")
    (missing / _SQLITE_ARCHIVE).unlink()
    cases.append((missing, digest, identity))
    tampered, digest = make_bundle(tmp_path / "tampered")
    os.chmod(tampered / _PG_ARCHIVE, 0o600)
    tampered_bytes = bytearray((tampered / _PG_ARCHIVE).read_bytes())
    tampered_bytes[-1] ^= 1
    (tampered / _PG_ARCHIVE).write_bytes(tampered_bytes)
    cases.append((tampered, digest, identity))
    truncated, digest = make_bundle(tmp_path / "truncated")
    os.chmod(truncated / _PG_ARCHIVE, 0o600)
    (truncated / _PG_ARCHIVE).write_bytes(b"PGDMP")
    cases.append((truncated, digest, identity))
    cases.append((good, trusted, {**identity, "run_attempt": "3"}))
    cases.append((good, trusted, {**identity, "run_id": "11"}))
    cases.append((good, trusted, {**identity, "commit": "b" * 40}))
    coordinated, digest = make_bundle(tmp_path / "coordinated")
    os.chmod(coordinated / _EXPECTED, 0o600)
    (coordinated / _EXPECTED).write_text("{}\n", encoding="utf-8")
    manifest = _strict_json(coordinated / _MANIFEST)
    files = manifest["files"]
    assert isinstance(files, dict)
    files[_EXPECTED] = {
        "sha256": _sha256(coordinated / _EXPECTED),
        "size": (coordinated / _EXPECTED).stat().st_size,
    }
    os.chmod(coordinated / _MANIFEST, 0o600)
    _write_json(coordinated / _MANIFEST, manifest)
    cases.append((coordinated, digest, identity))
    extra, digest = make_bundle(tmp_path / "extra")
    (extra / "not-allowlisted").write_text("x", encoding="utf-8")
    cases.append((extra, digest, identity))
    linked, digest = make_bundle(tmp_path / "linked")
    (linked / _PG_ARCHIVE).unlink()
    (linked / _PG_ARCHIVE).symlink_to(good / _PG_ARCHIVE)
    cases.append((linked, digest, identity))
    oversized, digest = make_bundle(tmp_path / "oversized")
    os.chmod(oversized / _MANIFEST, 0o600)
    (oversized / _MANIFEST).write_bytes(b"{" + b" " * _MAX_MANIFEST_BYTES + b"}")
    cases.append((oversized, digest, identity))

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("restore target mutation occurred before validation")

    monkeypatch.setattr("backend.tests.test_backup_transfer.create_engine", forbidden)
    for path, manifest_digest, expected_identity in cases:
        sentinel = tmp_path / f"sentinel-{path.name}"
        sentinel.write_text("not mutated", encoding="utf-8")
        with pytest.raises(TransferRejected):
            _validate_bundle(path, manifest_digest, expected_identity)
        with pytest.raises(TransferRejected):
            _restore_phase(path, "a" * 12, dict(expected_identity), manifest_digest)
        assert sentinel.read_text(encoding="utf-8") == "not mutated"


def test_sqlite_restore_never_overwrites_a_nonempty_target(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE synthetic (value TEXT NOT NULL)")
    connection.execute("INSERT INTO synthetic VALUES ('source')")
    connection.commit()
    connection.close()
    target = tmp_path / "target.sqlite"
    target.write_bytes(b"meaningful sentinel")
    with pytest.raises(FileExistsError):
        _restore_sqlite_exclusive(source, target)
    assert target.read_bytes() == b"meaningful sentinel"


def test_synthetic_backup_transfer_between_ci_jobs() -> None:
    if os.getenv(_OPT_IN) != "1":
        pytest.skip("synthetic backup transfer drill is not enabled")
    phase = os.getenv(_PHASE)
    if phase not in {"source", "restore"}:
        pytest.fail(f"{_PHASE} must be source or restore")
    bundle = _bundle_path()
    identity = _identity_from_environment()
    container = _required_transfer_container()
    if phase == "source":
        _source_phase(bundle, container, identity)
        return
    trusted_digest = os.getenv(_TRUSTED_MANIFEST, "")
    _restore_phase(bundle, container, identity, trusted_digest)
