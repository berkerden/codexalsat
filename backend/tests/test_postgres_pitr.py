"""Opt-in PostgreSQL 16 physical PITR drill in an owned Docker container."""

from __future__ import annotations

import asyncio
import copy
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from backend.tests.test_postgres_recovery import (
    _assert_identical,
    _raw_paper_state,
    _schema_and_rows,
    _seed_source,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError

from spotlab.engine import PaperEngine
from spotlab.testnet_panel import TestnetPanel as Panel

_OPT_IN = "SPOTLAB_REQUIRE_POSTGRES_PITR"
_IMAGE = "postgres:16-alpine"
_OWNER_LABEL = "spotlab.pitr.owner"
_SAFE_ID = re.compile(r"^[a-f0-9]{12,64}$")
_SAFE_NAME = re.compile(r"^spotlab-pitr-[a-f0-9]{24}$")
_SAFE_WAL = re.compile(r"^[A-F0-9]{24}$")
_ARCHIVE = "/var/lib/postgresql/pitr-archive"
_BASE = "/var/lib/postgresql/pitr-base"
_RESTORE = "/var/lib/postgresql/pitr-restore"
_MISSING = "/var/lib/postgresql/pitr-missing"
_BAD = "/var/lib/postgresql/pitr-bad"
_MISSING_ARCHIVE = "/var/lib/postgresql/pitr-missing-archive"


def _required_docker() -> str:
    if os.getenv(_OPT_IN) != "1":
        pytest.skip("PostgreSQL physical PITR drill is not enabled")
    docker = shutil.which("docker")
    if docker is None:
        pytest.fail("Docker is required when SPOTLAB_REQUIRE_POSTGRES_PITR=1")
    return docker


def _run(
    command: list[str], *, payload: bytes | None = None, timeout: float = 60
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        input=payload,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _exec(
    docker: str,
    container: str,
    *command: str,
    payload: bytes | None = None,
    user: str | None = None,
    timeout: float = 60,
) -> subprocess.CompletedProcess[bytes]:
    assert _SAFE_ID.fullmatch(container)
    prefix = [docker, "exec"]
    if payload is not None:
        prefix.append("-i")
    if user:
        prefix.extend(("--user", user))
    return _run(prefix + [container, *command], payload=payload, timeout=timeout)


def _ok(result: subprocess.CompletedProcess[bytes]) -> bytes:
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout


def _wait(probe: Callable[[], object], timeout: float = 30) -> object:
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        try:
            last = probe()
            if last:
                return last
        except (DBAPIError, OSError):
            last = None
        time.sleep(0.2)
    raise AssertionError(f"condition not reached in {timeout}s; last={last!r}")


def _port(docker: str, container: str, internal: int) -> int:
    output = _ok(_run([docker, "port", container, f"{internal}/tcp"])).decode().strip()
    match = re.fullmatch(r"127\.0\.0\.1:(\d+)", output)
    assert match, f"unexpected loopback port mapping: {output!r}"
    return int(match.group(1))


def _url(port: int, password: str) -> str:
    return (
        make_url("postgresql+psycopg://postgres@127.0.0.1/spotlab_pitr")
        .set(
            port=port,
            password=password,
            query={"connect_timeout": "5", "options": "-c statement_timeout=10000"},
        )
        .render_as_string(hide_password=False)
    )


def _wait_database(url: str) -> None:
    def ready() -> bool:
        engine = create_engine(url, pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                return connection.execute(text("SELECT 1")).scalar_one() == 1
        finally:
            engine.dispose()

    _wait(ready)


def _identity() -> tuple[str, str, str]:
    name = f"spotlab-pitr-{uuid.uuid4().hex[:24]}"
    owner = uuid.uuid4().hex
    credential = uuid.uuid4().hex
    assert _SAFE_NAME.fullmatch(name)
    return name, owner, credential


def _create_container(docker: str, name: str, owner: str, password: str) -> str:
    assert _SAFE_NAME.fullmatch(name)
    image = _run([docker, "image", "inspect", _IMAGE], timeout=15)
    if image.returncode != 0:
        pytest.fail(f"required fixed Docker image is unavailable: {_IMAGE}")
    result = _run(
        [
            docker,
            "run",
            "--detach",
            "--pull",
            "never",
            "--name",
            name,
            "--label",
            f"{_OWNER_LABEL}={owner}",
            "--memory",
            "1g",
            "--cpus",
            "2",
            "--env",
            f"POSTGRES_PASSWORD={password}",
            "--env",
            "POSTGRES_DB=spotlab_pitr",
            "--publish",
            "127.0.0.1::5432",
            "--publish",
            "127.0.0.1::5433",
            _IMAGE,
            "postgres",
            "-c",
            "wal_level=replica",
            "-c",
            "archive_mode=on",
            "-c",
            (
                f"archive_command=mkdir -p {_ARCHIVE} && "
                f"test ! -f {_ARCHIVE}/%f && cp %p {_ARCHIVE}/%f"
            ),
        ],
        timeout=60,
    )
    _ok(result)
    container = result.stdout.decode().strip()
    assert _SAFE_ID.fullmatch(container)
    return container


def _cleanup(docker: str, name: str, owner: str, container: str | None) -> None:
    assert _SAFE_NAME.fullmatch(name)
    inspected = _run(
        [
            docker,
            "inspect",
            "--format",
            f'{{{{.Id}}}} {{{{index .Config.Labels "{_OWNER_LABEL}"}}}}',
            name,
        ]
    )
    if inspected.returncode != 0:
        assert container is None
        return
    parts = inspected.stdout.decode().split()
    assert len(parts) == 2 and parts[1] == owner and _SAFE_ID.fullmatch(parts[0])
    if container:
        assert parts[0] == container
    _ok(_run([docker, "rm", "--force", "--volumes", parts[0]], timeout=30))
    assert _run([docker, "inspect", parts[0]]).returncode != 0


def _copy(docker: str, container: str, source: str, target: str) -> None:
    allowed = {_BASE, _RESTORE, _MISSING, _BAD, _ARCHIVE, _MISSING_ARCHIVE}
    assert source in allowed and target in allowed
    _ok(_exec(docker, container, "cp", "-a", source, target))


def _append_recovery_config(
    docker: str,
    container: str,
    directory: str,
    target: datetime,
    *,
    port: int,
    archive: str,
) -> None:
    assert directory in {_RESTORE, _MISSING}
    assert port in {5433, 5434}
    assert archive in {_ARCHIVE, _MISSING_ARCHIVE}
    stamp = target.isoformat()
    assert target.utcoffset() is not None and "'" not in stamp
    config = (
        "\n".join(
            [
                "listen_addresses = '*'",
                f"port = {port}",
                "unix_socket_directories = '/tmp'",
                "archive_mode = 'off'",
                "primary_conninfo = ''",
                "recovery_target_timeline = 'current'",
                "recovery_target_inclusive = 'on'",
                f"restore_command = 'cp {archive}/%f %p'",
                f"recovery_target_time = '{stamp}'",
                "recovery_target_action = 'pause'",
            ]
        )
        + "\n"
    )
    _ok(
        _exec(
            docker,
            container,
            "sh",
            "-c",
            'cat >> "$1/postgresql.auto.conf" && touch "$1/recovery.signal"',
            "spotlab-config",
            directory,
            payload=config.encode(),
            user="postgres",
        )
    )


def _pg_ctl(
    docker: str, container: str, directory: str, action: str, timeout: int = 30
) -> subprocess.CompletedProcess[bytes]:
    assert directory in {_RESTORE, _MISSING} and action in {"start", "status", "stop"}
    command = ["pg_ctl", "-D", directory]
    if action == "start":
        command += ["-l", f"{directory}/pitr.log", "-w", "-t", str(timeout), "start"]
    elif action == "stop":
        command += ["-m", "immediate", "-w", "stop"]
    else:
        command.append("status")
    return _exec(docker, container, *command, user="postgres", timeout=timeout + 10)


def _stop_if_running(docker: str, container: str, directory: str) -> None:
    if _pg_ctl(docker, container, directory, "status").returncode == 0:
        _ok(_pg_ctl(docker, container, directory, "stop"))


def _base_backup(docker: str, container: str) -> None:
    _ok(
        _exec(
            docker,
            container,
            "pg_basebackup",
            "--username=postgres",
            "--no-password",
            f"--pgdata={_BASE}",
            "--format=plain",
            "--wal-method=stream",
            "--checkpoint=fast",
            user="postgres",
            timeout=90,
        )
    )
    assert (
        _ok(_exec(docker, container, "pg_basebackup", "--version"))
        .decode()
        .startswith("pg_basebackup (PostgreSQL) 16.")
    )
    _ok(_exec(docker, container, "pg_verifybackup", _BASE, user="postgres"))


def _archive_wal(engine: Engine, wal: str | None = None) -> None:
    if wal:
        assert _SAFE_WAL.fullmatch(wal)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("SELECT pg_switch_wal()"))

    def archived() -> bool:
        with engine.connect() as connection:
            if wal:
                return connection.execute(
                    text("SELECT last_archived_wal >= :wal FROM pg_stat_archiver"), {"wal": wal}
                ).scalar_one()
            return connection.execute(
                text("SELECT last_archived_wal IS NOT NULL FROM pg_stat_archiver")
            ).scalar_one()

    _wait(archived, 20)


def _negative_drills(
    docker: str, container: str, target: datetime, required_wal: str, relation_path: str
) -> None:
    assert _SAFE_WAL.fullmatch(required_wal)
    assert re.fullmatch(r"base/\d+/\d+", relation_path)
    _copy(docker, container, _BASE, _BAD)
    relation = f"{_BAD}/{relation_path}"
    _ok(_exec(docker, container, "test", "-s", relation))
    corrupted = _exec(
        docker, container, "sh", "-c", 'printf x >> "$1"', "spotlab-corrupt", relation
    )
    _ok(corrupted)
    rejected = _exec(docker, container, "pg_verifybackup", _BAD, user="postgres", timeout=30)
    assert rejected.returncode != 0
    rejection = rejected.stderr.decode(errors="replace")
    assert relation_path in rejection and ("checksum" in rejection or "size" in rejection)

    _copy(docker, container, _ARCHIVE, _MISSING_ARCHIVE)
    _ok(_exec(docker, container, "rm", f"{_MISSING_ARCHIVE}/{required_wal}"))
    _copy(docker, container, _BASE, _MISSING)
    _append_recovery_config(
        docker, container, _MISSING, target, port=5434, archive=_MISSING_ARCHIVE
    )
    _pg_ctl(docker, container, _MISSING, "start", timeout=8)
    _wait(
        lambda: _pg_ctl(docker, container, _MISSING, "status").returncode != 0,
        timeout=15,
    )
    log = _ok(_exec(docker, container, "cat", f"{_MISSING}/pitr.log")).decode(errors="replace")
    assert "recovery ended before configured recovery target was reached" in log


def test_postgres16_physical_pitr_recovery_target_time(tmp_path: Path) -> None:
    docker = _required_docker()
    name, owner, password = _identity()
    container: str | None = None
    primary: Engine | None = None
    recovered: Engine | None = None
    try:
        container = _create_container(docker, name, owner, password)
        source_url = _url(_port(docker, container, 5432), password)
        recovered_url = _url(_port(docker, container, 5433), password)
        _wait_database(source_url)
        primary = create_engine(source_url)
        with primary.connect() as connection:
            version = int(connection.execute(text("SHOW server_version_num")).scalar_one())
            assert version // 10_000 == 16
            system_id = connection.execute(
                text("SELECT system_identifier FROM pg_control_system()")
            ).scalar_one()
            relation_path = connection.execute(
                text("SELECT pg_relation_filepath('pg_class')")
            ).scalar_one()
        _base_backup(docker, container)
        # Move all subsequent application transactions beyond WAL streamed in the base.
        _archive_wal(primary)

        original_paper, original_archive = _seed_source(source_url)
        with primary.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE pitr_probe (sequence INTEGER PRIMARY KEY, "
                    "phase TEXT NOT NULL UNIQUE)"
                )
            )
            connection.execute(text("INSERT INTO pitr_probe VALUES (1, 'afterbackup')"))
        with primary.begin() as connection:
            connection.execute(text("INSERT INTO pitr_probe VALUES (2, 'pretarget')"))
        with primary.connect() as connection:
            required_wal = connection.execute(
                text("SELECT pg_walfile_name(pg_current_wal_insert_lsn())")
            ).scalar_one()
            target = connection.execute(text("SELECT clock_timestamp()")).scalar_one()
        expected = _schema_and_rows(primary)

        with primary.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO pitr_probe SELECT 3, 'posttarget' "
                    "WHERE clock_timestamp() > :target"
                ),
                {"target": target},
            )
            connection.execute(text("UPDATE bot_state SET payload='{}' WHERE id=1"))
        with primary.connect() as connection:
            assert connection.execute(
                text("SELECT EXISTS (SELECT FROM pitr_probe WHERE phase='posttarget')")
            ).scalar_one()
            posttarget_wal = connection.execute(
                text("SELECT pg_walfile_name(pg_current_wal_insert_lsn())")
            ).scalar_one()
        _archive_wal(primary, posttarget_wal)
        _ok(_exec(docker, container, "test", "-f", f"{_ARCHIVE}/{required_wal}"))
        assert (
            _exec(docker, container, "test", "-e", f"{_BASE}/pg_wal/{required_wal}").returncode != 0
        )
        _negative_drills(docker, container, target, required_wal, relation_path)

        _copy(docker, container, _BASE, _RESTORE)
        _append_recovery_config(docker, container, _RESTORE, target, port=5433, archive=_ARCHIVE)
        _ok(_pg_ctl(docker, container, _RESTORE, "start"))
        _wait_database(recovered_url)
        recovered = create_engine(recovered_url, pool_pre_ping=True)

        def paused() -> bool:
            assert recovered is not None
            with recovered.connect() as connection:
                return connection.execute(
                    text(
                        "SELECT pg_is_in_recovery() AND pg_get_wal_replay_pause_state() = 'paused'"
                    )
                ).scalar_one()

        _wait(paused)
        with recovered.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT system_identifier FROM pg_control_system()")
                ).scalar_one()
                == system_id
            )
        _assert_identical(expected, _schema_and_rows(recovered))
        with pytest.raises(DBAPIError) as readonly:
            with recovered.begin() as connection:
                connection.execute(text("INSERT INTO pitr_probe VALUES (9, 'readonly')"))
        assert readonly.value.orig.sqlstate == "25006"

        with recovered.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            assert connection.execute(text("SELECT pg_promote(true, 30)")).scalar_one()
        _wait(lambda: not _in_recovery(recovered))
        _assert_identical(expected, _schema_and_rows(recovered))

        raw_before = _raw_paper_state(recovered_url)
        paper = PaperEngine(recovered_url)
        try:
            restarted, raw_after = paper.snapshot(), _raw_paper_state(recovered_url)
            assert restarted["session_id"] == original_paper["session_id"]
            assert not restarted["entries_enabled"]
            assert raw_after["audit"][:-1] == raw_before["audit"]
            assert raw_after["audit"][-1]["event"] == "restart"
            expected_raw = copy.deepcopy(raw_before)
            expected_raw.update(
                entries_enabled=False, reason=raw_after["reason"], audit=raw_after["audit"]
            )
            expected_raw["observation"]["interruptions"]["restart"] += 1
            expected_raw["observation"]["continuity"] = None
            assert raw_after == expected_raw
            assert paper.archive_detail(original_archive["state"]["session_id"]) == original_archive
        finally:
            paper.db.dispose()

        panel = Panel(recovered_url, tmp_path / "credentials-must-not-be-read.json")
        panel.credentials_loader = Mock(side_effect=AssertionError("no credentials"))
        panel.transport_factory = Mock(side_effect=AssertionError("no exchange transport"))
        before_panel = _schema_and_rows(recovered)
        status = asyncio.run(panel.status())
        assert status["ledger_status"] == "ready"
        assert status["cycles"] == [
            {
                "cycle_id": "recovery-cycle",
                "symbol": "BTCUSDT",
                "state": "PROTECTION_UNVERIFIED",
                "stop_entry_requested": False,
                "updated_at_ms": 1_790_000_000_000,
            }
        ]
        assert status["watcher"]["phase"] == "idle"
        assert not status["watcher"]["allow_mutations"]
        assert not status["watcher"]["manager_running"]
        panel.credentials_loader.assert_not_called()
        panel.transport_factory.assert_not_called()
        _assert_identical(before_panel, _schema_and_rows(recovered))
    finally:
        try:
            if recovered:
                recovered.dispose()
            if primary:
                primary.dispose()
        finally:
            try:
                if container:
                    _stop_if_running(docker, container, _RESTORE)
            finally:
                _cleanup(docker, name, owner, container)


def _in_recovery(engine: Engine) -> bool:
    with engine.connect() as connection:
        return connection.execute(text("SELECT pg_is_in_recovery()")).scalar_one()


def test_pitr_opt_in_is_explicit_and_docker_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_OPT_IN, raising=False)
    with pytest.raises(pytest.skip.Exception):
        _required_docker()
    monkeypatch.setenv(_OPT_IN, "1")
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(pytest.fail.Exception, match="Docker is required"):
        _required_docker()
