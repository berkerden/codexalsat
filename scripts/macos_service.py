#!/usr/bin/env python3
"""Install and control SpotLab as a bounded macOS user-session launchd job."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import plistlib
import pwd
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

LABEL = "com.spotlab.local"
COMMAND_TIMEOUT = 15
STOP_TIMEOUT = 30
UNSUPPORTED_ENV = (
    "DATABASE_URL",
    "SPOTLAB_TESTNET_DATABASE_URL",
    "CORS_ORIGINS",
    "SPOTLAB_TESTNET_API_KEY",
    "SPOTLAB_TESTNET_API_SECRET",
)
LEGACY_PAPER_STATE_KEYS = frozenset(
    {
        "mode", "entries_enabled", "config", "cash", "equity", "peak",
        "day_start_equity", "day", "daily_trades", "loss_streak", "position",
        "orders", "fills", "audit", "last_signal_time", "reason", "expires_at",
        "daily_loss", "drawdown", "realized_pnl",
    }
)
KNOWN_PAPER_STATE_KEYS = LEGACY_PAPER_STATE_KEYS | {
    "session_id", "started_at", "observation", "observed_drawdown",
}


class ServiceError(RuntimeError):
    """A safe, user-facing service configuration error."""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def service_paths(root: Path) -> dict[str, Path]:
    base = root / "data" / "service"
    return {
        "base": base,
        "manifest": base / "manifest.json",
        "plist": base / f"{LABEL}.plist",
        "runtime": base / "runtime.json",
    }


def _user_home() -> Path:
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
        metadata = home.stat()
    except (KeyError, OSError) as exc:
        raise ServiceError("Kullanıcı ana dizini doğrulanamadı.") from exc
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise ServiceError("Kullanıcı ana dizini güvenli değil.")
    return home


def log_paths(home: Path) -> dict[str, Path]:
    base = home / "Library" / "Logs" / "SpotLab"
    return {
        "base": base,
        "stdout": base / "stdout.log",
        "stderr": base / "stderr.log",
    }


def domain() -> str:
    return f"gui/{os.getuid()}"


def service_target() -> str:
    return f"{domain()}/{LABEL}"


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise ServiceError("Bu servis yalnızca macOS üzerinde desteklenir.")


def _run(
    command: Sequence[str], *, timeout: float = COMMAND_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command), text=True, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError(f"Komut tamamlanamadı: {command[0]}") from exc


def _git(root: Path, *args: str) -> str:
    result = _run(("git", "-C", str(root), *args))
    if result.returncode != 0:
        raise ServiceError("Kaynak deposu doğrulanamadı.")
    return result.stdout.strip()


def source_state(root: Path) -> tuple[str, Path]:
    real_root = Path(_git(root, "rev-parse", "--show-toplevel")).resolve()
    if real_root != root.resolve():
        raise ServiceError("Servis yalnızca depo kökünden yapılandırılabilir.")
    if ":" in str(real_root):
        raise ServiceError("Kaynak yolu PYTHONPATH içinde güvenle kullanılamıyor.")
    revision = _git(real_root, "rev-parse", "HEAD")
    if _git(real_root, "status", "--porcelain", "--untracked-files=all"):
        raise ServiceError("Kaynak ağacı değişmiş; servis başlatılmadı.")
    return revision, real_root


def _unsupported_overrides(env: Mapping[str, str]) -> None:
    present = [name for name in UNSUPPORTED_ENV if name in env]
    if present:
        raise ServiceError(
            "Desteklenmeyen servis yapılandırma değişkeni ayarlı: " + ", ".join(present)
        )


def _canonical_data_dir(value: str | Path, *, relative_to: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() and relative_to is not None:
        path = relative_to / path
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise ServiceError("Yapılandırılmış veri dizini bulunamadı.") from exc
    if not path.is_dir():
        raise ServiceError("Yapılandırılmış veri yolu bir dizin değil.")
    if "?" in str(path):
        raise ServiceError("Veri yolu SQLite URL'sinde güvenle kullanılamıyor.")
    paper = path / "paper.sqlite"
    if not paper.is_file() or paper.stat().st_size == 0:
        raise ServiceError("Mevcut ve boş olmayan paper.sqlite gerekli.")
    try:
        connection = sqlite3.connect(f"{paper.as_uri()}?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT payload FROM bot_state WHERE id = 1").fetchone()
        finally:
            connection.close()
        state = json.loads(row[0]) if row is not None else None
    except (sqlite3.Error, json.JSONDecodeError, TypeError) as exc:
        raise ServiceError("paper.sqlite mevcut bir SpotLab işlem defteri değil.") from exc
    if not isinstance(state, dict):
        raise ServiceError("paper.sqlite içinde mevcut SpotLab durumu bulunamadı.")
    keys = set(state)
    if not LEGACY_PAPER_STATE_KEYS.issubset(keys):
        raise ServiceError("paper.sqlite içindeki SpotLab durumu geçersiz.")
    if not keys.issubset(KNOWN_PAPER_STATE_KEYS):
        raise ServiceError("paper.sqlite içindeki SpotLab durumu geçersiz.")
    return path


def _pinned_directory(value: str | Path, *, description: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ServiceError(f"Kayıtlı {description} yolu mutlak değil.")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ServiceError(f"Kayıtlı {description} yolu bulunamadı.") from exc
    if resolved != path:
        raise ServiceError(f"Kayıtlı {description} yolu artık aynı gerçek yolu göstermiyor.")
    return path


def _testnet_ledger_present(data: Path) -> bool:
    ledger = data / "testnet-orders.sqlite"
    if not ledger.exists():
        return False
    if not ledger.is_file() or ledger.stat().st_size == 0:
        raise ServiceError("Testnet işlem defteri boş veya geçersiz.")
    return True


def _load_manifest(path: Path) -> dict[str, Any] | None:
    try:
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ServiceError("Servis bildirimi okunamadı.") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ServiceError("Servis bildirimi güvenli bir kullanıcı dosyası değil.")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            raw = stream.read()
    except UnicodeError as exc:
        raise ServiceError("Servis bildirimi geçersiz.") from exc
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ServiceError("Servis bildirimi geçersiz.") from exc
    if not isinstance(value, dict):
        raise ServiceError("Servis bildirimi geçersiz.")
    required = {
        "format",
        "label",
        "repo_root",
        "revision",
        "data_dir",
        "testnet_ledger_present",
        "keep_awake",
    }
    if not required.issubset(value):
        raise ServiceError("Servis bildirimi eksik.")
    string_fields = ("label", "repo_root", "revision", "data_dir")
    if any(not isinstance(value[name], str) for name in string_fields):
        raise ServiceError("Servis bildirimi geçersiz.")
    if type(value["format"]) is not int or value["format"] != 1:
        raise ServiceError("Servis bildirimi geçersiz.")
    if type(value["keep_awake"]) is not bool or type(value["testnet_ledger_present"]) is not bool:
        raise ServiceError("Servis bildirimi geçersiz.")
    if type(value.get("notify_local", False)) is not bool:
        raise ServiceError("Servis bildirimi geçersiz.")
    if value["label"] != LABEL or re.fullmatch(r"[0-9a-f]{40}", value["revision"]) is None:
        raise ServiceError("Servis bildirimi geçersiz.")
    if not Path(value["repo_root"]).is_absolute() or not Path(value["data_dir"]).is_absolute():
        raise ServiceError("Servis bildirimi geçersiz.")
    return value


def _data_for_start(root: Path, previous: dict[str, Any] | None, env: Mapping[str, str]) -> Path:
    configured = env.get("SPOTLAB_DATA_DIR")
    if previous is not None:
        pinned_data = _pinned_directory(previous["data_dir"], description="veri")
        data = _canonical_data_dir(pinned_data)
        if configured is not None and _canonical_data_dir(configured, relative_to=root) != data:
            raise ServiceError("SPOTLAB_DATA_DIR kayıtlı servis veri yoluyla çakışıyor.")
        testnet_present = _testnet_ledger_present(data)
        if previous["testnet_ledger_present"] and not testnet_present:
            raise ServiceError("Daha önce bulunan Testnet işlem defteri artık yok.")
        return data
    data = _canonical_data_dir(
        configured if configured is not None else root / "data", relative_to=root
    )
    _testnet_ledger_present(data)
    return data


def _node_path(root: Path, home: Path) -> Path:
    candidates: list[Path] = []
    found = shutil.which("node")
    if found:
        candidates.append(Path(found))
    candidates.extend(
        (
            root / ".tools" / "node" / "bin" / "node",
            Path("/opt/homebrew/bin/node"),
            Path("/usr/local/bin/node"),
            home / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node",
            Path("/usr/bin/node"),
        )
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise ServiceError("Node bulunamadı; önce scripts/setup-local.sh çalıştırın.")


def _runtime_environment(root: Path, data: Path) -> dict[str, str]:
    home = _user_home()
    node = _node_path(root, home)
    if ":" in str(node.parent):
        raise ServiceError("Node yolu PATH içinde güvenle kullanılamıyor.")
    path_parts = [str(node.parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return {
        "HOME": str(home),
        "PATH": ":".join(dict.fromkeys(path_parts)),
        "SPOTLAB_DATA_DIR": str(data),
        "DATABASE_URL": f"sqlite:///{data / 'paper.sqlite'}",
        "SPOTLAB_TESTNET_DATABASE_URL": f"sqlite:///{data / 'testnet-orders.sqlite'}",
        "PYTHONPATH": f"{root / 'backend' / 'src'}:{root}",
    }


def _manifest(
    root: Path, revision: str, data: Path, keep_awake: bool, notify_local: bool = False
) -> dict[str, Any]:
    return {
        "format": 1,
        "label": LABEL,
        "repo_root": str(root),
        "revision": revision,
        "data_dir": str(data),
        "testnet_ledger_present": _testnet_ledger_present(data),
        "keep_awake": keep_awake,
        "notify_local": notify_local,
    }


def _plist(
    root: Path,
    paths: Mapping[str, Path],
    logs: Mapping[str, Path],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    python = root / ".venv" / "bin" / "python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ServiceError("Python ortamı bulunamadı; önce scripts/setup-local.sh çalıştırın.")
    environment = _runtime_environment(root, Path(str(manifest["data_dir"])))
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(python),
            str(root / "scripts" / "macos_service.py"),
            "_run",
            str(paths["manifest"]),
        ],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": environment,
        "RunAtLoad": False,
        "KeepAlive": False,
        "ExitTimeOut": 20,
        "Umask": 0o077,
        "AbandonProcessGroup": False,
        # The supervisor owns bounded output files.  launchd must never impose a
        # file-size resource limit on the process group (which also writes SQLite).
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }


def _private_directory(path: Path) -> None:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ServiceError("Özel servis dizini oluşturulamadı.") from exc
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise ServiceError("Özel servis dizini güvenli değil.")
    os.chmod(path, 0o700)


def _owned_directory_without_chmod(path: Path) -> None:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ServiceError("Kullanıcı günlük dizini oluşturulamadı.") from exc
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise ServiceError("Kullanıcı günlük dizini güvenli değil.")


def _log_directory(home: Path) -> Path:
    library = home / "Library"
    logs = library / "Logs"
    _owned_directory_without_chmod(library)
    _owned_directory_without_chmod(logs)
    private_logs = logs / "SpotLab"
    _private_directory(private_logs)
    return private_logs


def _local_state_directory(root: Path) -> Path:
    data = root / "data"
    try:
        os.mkdir(data, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ServiceError("Yerel servis veri dizini oluşturulamadı.") from exc
    metadata = data.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise ServiceError("Yerel servis veri dizini güvenli değil.")
    service = data / "service"
    _private_directory(service)
    return service


def _safe_existing_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise ServiceError("Özel servis dosyası güvenli değil.")


def _open_private_log(path: Path) -> None:
    _safe_existing_file(path)
    flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ServiceError("Servis günlük dosyası güvenli değil.")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _private_file(path: Path, content: bytes) -> None:
    _private_directory(path.parent)
    _safe_existing_file(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _restore_manifest(path: Path, previous: bytes | None) -> None:
    if previous is None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    else:
        _private_file(path, previous)


@contextlib.contextmanager
def mutation_lock() -> Iterator[None]:
    lock_path = Path("/tmp") / f"spotlab-{LABEL}-{os.getuid()}.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ServiceError("Servis işlem kilidi açılamadı.") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ServiceError("Servis işlem kilidi güvenli değil.")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ServiceError("Başka bir servis işlemi devam ediyor.") from exc
        yield
    finally:
        os.close(fd)


def start(
    root: Path, env: Mapping[str, str], *, keep_awake: bool, accept: bool,
    notify_local: bool = False,
) -> None:
    _require_macos()
    _unsupported_overrides(env)
    revision, root = source_state(root)
    paths = service_paths(root)
    previous = _load_manifest(paths["manifest"])
    if previous is not None:
        pinned_root = _pinned_directory(previous["repo_root"], description="kaynak")
        if pinned_root != root:
            raise ServiceError("Yerel servis bildirimi başka bir kaynak köküne ait.")
        if str(previous["revision"]) != revision and not accept:
            raise ServiceError(
                "Kaynak sürümü kayıtlı sürümden farklı; --accept-current-version gerekli."
            )
    data = _data_for_start(root, previous, env)
    candidate = _manifest(root, revision, data, keep_awake, notify_local)
    home = _user_home()
    logs = log_paths(home)
    plist = _plist(root, paths, logs, candidate)

    if _local_state_directory(root) != paths["base"]:
        raise ServiceError("Yerel servis dizini doğrulanamadı.")
    if _log_directory(home) != logs["base"]:
        raise ServiceError("Servis günlük dizini doğrulanamadı.")
    # Existing logs are intentionally left untouched until the newly acquired
    # supervisor starts and holds the single-writer lock.
    _private_file(paths["plist"], plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True))
    old_manifest = None
    if previous is not None:
        old_manifest = (json.dumps(previous, indent=2, sort_keys=True) + "\n").encode()

    acquired = _run(("launchctl", "bootstrap", domain(), str(paths["plist"])))
    if acquired.returncode != 0:
        raise ServiceError("Kullanıcı oturumundaki SpotLab servis etiketi edinilemedi.")
    try:
        serialized = (json.dumps(candidate, indent=2, sort_keys=True) + "\n").encode()
        _private_file(paths["manifest"], serialized)
        kicked = _run(("launchctl", "kickstart", service_target()))
        if kicked.returncode != 0:
            raise ServiceError("Servis işi başlatılamadı.")
    except BaseException as original:
        try:
            cleanup = _run(
                ("launchctl", "bootout", service_target()), timeout=STOP_TIMEOUT
            )
        except ServiceError:
            cleanup = None
        if cleanup is None or cleanup.returncode != 0:
            with contextlib.suppress(ServiceError, OSError):
                serialized = (json.dumps(candidate, indent=2, sort_keys=True) + "\n").encode()
                _private_file(paths["manifest"], serialized)
            raise ServiceError(
                "Başlatma başarısız; edinilen servis işinin temizlendiği doğrulanamadı."
            ) from original
        _restore_manifest(paths["manifest"], old_manifest)
        raise


def stop() -> None:
    _require_macos()
    result = _run(("launchctl", "bootout", service_target()), timeout=STOP_TIMEOUT)
    if result.returncode != 0:
        raise ServiceError("SpotLab servis işi durdurulamadı veya yüklü olduğu doğrulanamadı.")


def _restart_paths(root: Path, home: Path) -> None:
    # Read-only validation: never rotate/chmod logs or create files before stop.
    for path in (
        root / "data", service_paths(root)["base"],
        home / "Library", home / "Library" / "Logs", log_paths(home)["base"],
    ):
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ServiceError("Yeniden başlatma için servis dizini güvenli değil.")
    paths = service_paths(root)
    files = [paths["manifest"], paths["plist"], paths["runtime"]]
    logs = log_paths(home)["base"]
    files += [logs / ".supervisor.lock"]
    files += [logs / (name + suffix) for name in ("stdout.log", "stderr.log")
              for suffix in ("", ".1", ".2", ".3")]
    for path in files:
        _safe_existing_file(path)
        if path.exists() and path.stat().st_nlink != 1:
            raise ServiceError("Yeniden başlatma için servis dosyası güvenli değil.")
    for path in (root / ".venv/bin/python", root / ".venv/bin/uvicorn"):
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ServiceError("Yeniden başlatma için Python ortamı eksik.")
    for name in ("scripts/run-local.sh", "scripts/service_supervisor.py",
                 "scripts/node-env.sh", "frontend/node_modules/vite/bin/vite.js"):
        if not (root / name).is_file():
            raise ServiceError("Yeniden başlatma için uygulama dosyası eksik.")


def _owned_loaded_job(root: Path, paths: Mapping[str, Path]) -> int | None:
    result = _run(("launchctl", "print", service_target()))
    # launchctl print is diagnostic text, not a stable API. Accept only the
    # observed unambiguous top-level fields; format changes must fail closed.
    expected = {
        "path": str(paths["plist"]),
        "program": str(root / ".venv/bin/python"),
        "working directory": str(root),
    }
    if result.returncode or not result.stdout.startswith(service_target() + " = {\n"):
        raise ServiceError("Yüklü servisin bu kaynak klasörüne ait olduğu doğrulanamadı.")
    lines = result.stdout.splitlines()
    for name, value in expected.items():
        values = [line[len(name) + 4:] for line in lines if line.startswith("\t" + name + " = ")]
        if values != [value]:
            raise ServiceError("Yüklü servis başka klasöre ait veya kaydı belirsiz.")
    expected_arguments = [
        str(root / ".venv/bin/python"), str(root / "scripts/macos_service.py"),
        "_run", str(paths["manifest"]),
    ]
    block = "\targuments = {\n" + "".join("\t\t" + arg + "\n" for arg in expected_arguments) + "\t}"
    if result.stdout.count("\targuments = {\n") != 1 or block not in result.stdout:
        raise ServiceError("Yüklü servisin başlatma komutu doğrulanamadı.")
    pids = [line.removeprefix("\tpid = ") for line in lines if line.startswith("\tpid = ")]
    if not pids:
        return None
    if len(pids) != 1 or re.fullmatch(r"[1-9][0-9]{0,9}", pids[0]) is None:
        raise ServiceError("Yüklü servis süreç kimliği belirsiz.")
    pid = int(pids[0])
    if pid > 2**31 - 1:
        raise ServiceError("Yüklü servis süreç kimliği geçersiz.")
    return pid


def _wait_stopped(pid: int | None) -> None:
    deadline = time.monotonic() + STOP_TIMEOUT
    absent_message = f'Could not find service "{LABEL}" in domain for user gui: {os.getuid()}'
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ServiceError("Servisin tamamen kapandığı süresi içinde doğrulanamadı.")
        result = _run(("launchctl", "print", service_target()), timeout=min(2.0, remaining))
        absent = (
            result.returncode == 113 and not result.stdout.strip()
            and result.stderr.strip() in {absent_message, "Bad request.\n" + absent_message}
        )
        if result.returncode != 0 and not absent:
            raise ServiceError("Servis kaydının kaldırıldığı doğrulanamadı.")
        gone = pid is None
        if pid is not None:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                gone = True
            except PermissionError:
                gone = False
        if absent and gone:
            return
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def restart(root: Path, env: Mapping[str, str]) -> None:
    """Explicit same-version stop/start; caller holds mutation_lock throughout."""
    _require_macos()
    _unsupported_overrides(env)
    revision, root = source_state(root)
    paths = service_paths(root)
    previous = _load_manifest(paths["manifest"])
    if previous is None:
        raise ServiceError("Yeniden başlatma için kayıtlı servis gerekli.")
    if _pinned_directory(previous["repo_root"], description="kaynak") != root:
        raise ServiceError("Kayıtlı servis başka kaynak klasörüne ait.")
    if previous["revision"] != revision:
        raise ServiceError("Yeniden başlatma sürüm değiştiremez; önce kontrollü güncelleme yapın.")
    _data_for_start(root, previous, env)
    home = _user_home()
    _restart_paths(root, home)
    prospective = _plist(root, paths, log_paths(home), previous)
    runtime_path = prospective["EnvironmentVariables"].get("PATH", "")
    if not shutil.which("npm", path=runtime_path) and not (
        root / ".tools/npm/bin/npm-cli.js"
    ).is_file():
        raise ServiceError("Yeniden başlatma için npm bulunamadı.")
    if previous["keep_awake"] and not os.access("/usr/bin/caffeinate", os.X_OK):
        raise ServiceError("Kayıtlı uyku koruması kullanılamıyor.")
    previous_pid = _owned_loaded_job(root, paths)
    stop()  # failure must prevent a new bootstrap
    try:
        _wait_stopped(previous_pid)
        start(root, env, keep_awake=previous["keep_awake"], accept=False,
              notify_local=previous.get("notify_local", False))
    except (ServiceError, OSError) as exc:
        raise ServiceError(
            "Durdurma talebi kabul edildi ancak yeniden başlatma tamamlanamadı; "
            "status ile kontrol edin."
        ) from exc


def status(root: Path) -> None:
    paths = service_paths(root.resolve())
    logs = log_paths(_user_home())
    manifest = _load_manifest(paths["manifest"])
    try:
        result = _run(("launchctl", "print", service_target()))
        global_job = "loaded" if result.returncode == 0 else "not-confirmed"
    except ServiceError:
        global_job = "not-confirmed"
    payload: dict[str, Any] = {
        "label": LABEL,
        "global_job": global_job,
        "local_manifest": "present" if manifest is not None else "absent",
        "logs_dir": str(logs["base"]),
        "service_state": "unknown",
        "current_check": {"responsive": False},
    }
    if manifest is not None:
        configured_root = Path(str(manifest["repo_root"]))
        payload.update(
            {
                "configured_revision": manifest["revision"],
                "configured_repo_root": str(configured_root),
                "configured_data_dir": manifest["data_dir"],
                "manifest_matches_this_checkout": configured_root.resolve() == root.resolve(),
                "notify_local": manifest.get("notify_local", False),
            }
        )
        snapshot = _load_runtime_snapshot(paths["runtime"])
        if snapshot is not None:
            payload["last_known_snapshot"] = snapshot
            now = time.time()
            checked = snapshot.get("checked_at")
            pid = snapshot.get("supervisor_pid")
            phase = snapshot.get("phase")
            identity = snapshot.get("run_id")
            matches = (
                type(snapshot.get("schema_version")) is int
                and snapshot.get("schema_version") == 1
                and snapshot.get("root") == str(configured_root)
                and snapshot.get("revision") == manifest["revision"]
                and isinstance(phase, str)
                and phase in {"starting", "running"}
                and _fresh_snapshot_time(checked, now)
                and type(pid) is int
                and pid > 0
                and pid <= 2**31 - 1
                and isinstance(identity, str)
                and re.fullmatch(r"[0-9a-f]{32}", identity) is not None
                and _pid_alive(pid)
            )
            if matches:
                assert isinstance(identity, str)
                responsive = _fresh_health(identity)
                payload["current_check"] = {
                    "responsive": responsive,
                    "identity_confirmed": responsive,
                    "supervisor_pid_confirmed": True,
                }
                if global_job == "loaded" and responsive:
                    payload["service_state"] = "responsive"
                else:
                    payload["service_state"] = "stale"
            else:
                payload["service_state"] = "stale"
    print(json.dumps(payload, indent=2, sort_keys=True))


def _load_runtime_snapshot(path: Path) -> dict[str, Any] | None:
    try:
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
    except (FileNotFoundError, OSError):
        return None
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_size > 16 * 1024
        ):
            os.close(fd)
            return None
        with os.fdopen(fd, "rb") as stream:
            value = json.loads(stream.read(16 * 1024 + 1))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError, RecursionError):
        with contextlib.suppress(OSError):
            os.close(fd)
        return None


def _fresh_snapshot_time(value: Any, now: float) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and now - 15 <= value <= now + 1
    except (OverflowError, TypeError):
        return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False


def _fresh_health(identity: str) -> bool:
    try:
        # Resolve the exact sibling, independent of cwd, PYTHONPATH, or an
        # unrelated module with the same name.
        import importlib.util

        sibling = Path(__file__).resolve().with_name("service_supervisor.py")
        spec = importlib.util.spec_from_file_location("spotlab_service_supervisor", sibling)
        if spec is None or spec.loader is None:
            return False
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for port in module.HEALTH_PORTS:
            module.probe_health(port, identity)
        return True
    except (ImportError, OSError, RuntimeError):
        return False


def runtime(manifest_path: Path) -> None:
    try:
        _validated_runtime(manifest_path)
    except (ServiceError, OSError):
        # launchd output is /dev/null by design, so retain one bounded,
        # non-sensitive diagnostic code for bootstrap failures.
        with contextlib.suppress(ServiceError, OSError):
            _private_file(
                manifest_path.parent / "runtime.json",
                (json.dumps({
                    "schema_version": 1,
                    "run_id": None,
                    "revision": None,
                    "root": None,
                    "supervisor_pid": os.getpid(),
                    "phase": "bootstrap_failed",
                    "checked_at": time.time(),
                    "components": {},
                    "logging": {"failed": False},
                    "current_warning": "runtime_validation_failed",
                    "last_transition": {
                        "at": time.time(), "warning": "runtime_validation_failed"
                    },
                }, sort_keys=True) + "\n").encode(),
            )
        raise


def _validated_runtime(manifest_path: Path) -> None:
    manifest = _load_manifest(manifest_path)
    if manifest is None or manifest.get("label") != LABEL:
        raise ServiceError("Servis bildirimi bulunamadı.")
    root = _pinned_directory(manifest["repo_root"], description="kaynak")
    revision, checked_root = source_state(root)
    if revision != manifest["revision"] or checked_root != root:
        raise ServiceError("Kaynak sürümü servis bildirimiyle eşleşmiyor.")
    pinned_data = _pinned_directory(manifest["data_dir"], description="veri")
    data = _canonical_data_dir(pinned_data)
    testnet_present = _testnet_ledger_present(data)
    if manifest["testnet_ledger_present"] and not testnet_present:
        raise ServiceError("Daha önce bulunan Testnet işlem defteri artık yok.")
    environment = _runtime_environment(root, data)
    # A caller-provided value can never join a previous run's identity.
    environment["SPOTLAB_SERVICE_INSTANCE_ID"] = uuid.uuid4().hex
    python = root / ".venv" / "bin" / "python"
    arguments = [
        str(python),
        str(root / "scripts" / "service_supervisor.py"),
        "--root",
        str(root),
        "--snapshot",
        str(service_paths(root)["runtime"]),
        "--revision",
        revision,
    ]
    if manifest["keep_awake"]:
        arguments.append("--keep-awake")
    if manifest.get("notify_local", False):
        arguments.append("--notify-local")
    os.execve(str(python), arguments, environment)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="SpotLab macOS kullanıcı oturumu servisi")
    commands = result.add_subparsers(
        dest="command", required=True, metavar="{start,status,stop,restart}"
    )
    start_parser = commands.add_parser("start", help="Arka planda başlatma isteği gönder")
    start_parser.add_argument("--keep-awake", action="store_true")
    start_parser.add_argument("--accept-current-version", action="store_true")
    start_parser.add_argument("--notify-local", action="store_true")
    commands.add_parser("status", help="Servis kaydını salt okunur kontrol et")
    commands.add_parser("stop", help="SpotLab arka plan servisini durdur")
    commands.add_parser("restart", help="Kayıtlı aynı sürümü ve ayarları kontrollü yeniden başlat")
    private = commands.add_parser("_run")
    private.add_argument("manifest", type=Path)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "status":
            status(repo_root())
        elif args.command == "_run":
            runtime(args.manifest)
        else:
            _require_macos()
            with mutation_lock():
                if args.command == "start":
                    start(
                        repo_root(),
                        os.environ,
                        keep_awake=args.keep_awake,
                        accept=args.accept_current_version,
                        notify_local=args.notify_local,
                    )
                    print("SpotLab kullanıcı oturumu servisi başlatma talebi kabul edildi.")
                elif args.command == "restart":
                    restart(repo_root(), os.environ)
                    print(
                        "SpotLab aynı sürümle yeniden başlatma talebi kabul edildi; "
                        "status ile kontrol edin."
                    )
                else:
                    stop()
                    print("SpotLab kullanıcı oturumu servisi durduruldu; veriler korundu.")
    except (ServiceError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
