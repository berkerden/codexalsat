"""Process/session locks for exclusive Testnet cycle management."""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from pathlib import Path
from types import TracebackType

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

try:
    import fcntl
except ImportError:  # pragma: no cover - supported deployment targets provide fcntl
    fcntl = None  # type: ignore[assignment]


POSTGRES_MANAGER_LOCK_ID = 7_311_735_332


class ManagerLockError(RuntimeError):
    """A manager lock cannot be acquired or proven live."""


class ManagerBusy(ManagerLockError):
    """Another cooperative manager owns the cycle database."""


class ManagerLock:
    """Hold one SQLite file lock or PostgreSQL advisory session lock."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._file_descriptor: int | None = None
        self._lock_path: Path | None = None
        self._lock_identity: tuple[int, int] | None = None
        self._connection: Connection | None = None
        self._backend_pid: int | None = None
        self._dbapi_connection: object | None = None

    def __enter__(self) -> ManagerLock:
        dialect = self._engine.url.get_backend_name()
        if dialect == "sqlite":
            self._acquire_sqlite()
        elif dialect == "postgresql":
            self._acquire_postgresql()
        else:
            raise ManagerLockError("unsupported_manager_lock_backend")
        return self

    def _acquire_sqlite(self) -> None:
        if fcntl is None:
            raise ManagerLockError("sqlite_manager_lock_unavailable")
        database = self._engine.url.database
        if not database or database == ":memory:":
            raise ManagerLockError("sqlite_manager_lock_requires_file")
        database_path = Path(database).expanduser().resolve(strict=False)
        lock_path = Path(str(database_path) + ".manager.lock")
        descriptor: int | None = None
        try:
            if lock_path.is_symlink():
                raise ManagerLockError("sqlite_manager_lock_symlink")
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(lock_path, flags, 0o600)
            os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
            descriptor_stat = os.fstat(descriptor)
            if not stat.S_ISREG(descriptor_stat.st_mode):
                raise ManagerLockError("sqlite_manager_lock_not_regular")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            if descriptor is not None:
                os.close(descriptor)
            raise ManagerBusy("manager_busy") from error
        except ManagerLockError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            raise ManagerLockError("sqlite_manager_lock_unavailable") from error
        assert descriptor is not None
        self._file_descriptor = descriptor
        self._lock_path = lock_path
        self._lock_identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)

    def _acquire_postgresql(self) -> None:
        connection = self._engine.connect()
        acquired = False
        try:
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:lock_id)"),
                    {"lock_id": POSTGRES_MANAGER_LOCK_ID},
                ).scalar_one()
            )
            if not acquired:
                raise ManagerBusy("manager_busy")
            self._backend_pid = int(
                connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
            )
            self._dbapi_connection = connection.connection.dbapi_connection
            connection.commit()
        except BaseException as error:
            # An interrupted reply can leave ownership unknown even before
            # acquired becomes true. Never pool that potentially locked session.
            if not isinstance(error, ManagerBusy):
                with suppress(Exception):
                    connection.invalidate()
            connection.close()
            raise
        self._connection = connection

    def check_alive(self) -> None:
        """Fail closed unless the original lock owner still holds the lock."""
        if self._file_descriptor is not None:
            try:
                descriptor_stat = os.fstat(self._file_descriptor)
                assert self._lock_path is not None and self._lock_identity is not None
                path_stat = self._lock_path.stat(follow_symlinks=False)
            except OSError as error:
                raise ManagerLockError("manager_lock_lost") from error
            if (
                not stat.S_ISREG(path_stat.st_mode)
                or (descriptor_stat.st_dev, descriptor_stat.st_ino) != self._lock_identity
                or (path_stat.st_dev, path_stat.st_ino) != self._lock_identity
            ):
                raise ManagerLockError("manager_lock_lost")
            return
        connection = self._connection
        if connection is None or self._backend_pid is None:
            raise ManagerLockError("manager_lock_not_held")
        if connection.closed or connection.invalidated:
            raise ManagerLockError("manager_lock_lost")
        if connection.connection.dbapi_connection is not self._dbapi_connection:
            raise ManagerLockError("manager_lock_lost")
        try:
            row = (
                connection.execute(
                    text(
                        "SELECT pg_backend_pid(), EXISTS ("
                        "SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                        "AND pid=pg_backend_pid() AND granted AND objsubid=1 "
                        "AND ((classid::bigint << 32) | objid::bigint)=:lock_id)"
                    ),
                    {"lock_id": POSTGRES_MANAGER_LOCK_ID},
                )
                .one()
            )
            connection.commit()
        except Exception as error:
            raise ManagerLockError("manager_lock_lost") from error
        if int(row[0]) != self._backend_pid or not bool(row[1]):
            raise ManagerLockError("manager_lock_lost")

    def close(self) -> None:
        if self._file_descriptor is not None:
            descriptor, self._file_descriptor = self._file_descriptor, None
            self._lock_path = None
            self._lock_identity = None
            try:
                assert fcntl is not None
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        if self._connection is not None:
            connection, self._connection = self._connection, None
            try:
                if not connection.closed and not connection.invalidated:
                    try:
                        unlocked = bool(
                            connection.execute(
                                text("SELECT pg_advisory_unlock(:lock_id)"),
                                {"lock_id": POSTGRES_MANAGER_LOCK_ID},
                            ).scalar_one()
                        )
                        connection.commit()
                        if not unlocked:
                            connection.invalidate()
                    except Exception:
                        with suppress(Exception):
                            connection.invalidate()
            finally:
                connection.close()

    def __exit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()
