#!/usr/bin/env python3
"""Install and control SpotLab as a bounded macOS user-session launchd job."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
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
    command: Sequence[str], *, timeout: int = COMMAND_TIMEOUT
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
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    except (KeyError, OSError) as exc:
        raise ServiceError("Kullanıcı ana dizini doğrulanamadı.") from exc
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


def _manifest(root: Path, revision: str, data: Path, keep_awake: bool) -> dict[str, Any]:
    return {
        "format": 1,
        "label": LABEL,
        "repo_root": str(root),
        "revision": revision,
        "data_dir": str(data),
        "testnet_ledger_present": _testnet_ledger_present(data),
        "keep_awake": keep_awake,
    }


def _plist(root: Path, paths: Mapping[str, Path], manifest: Mapping[str, Any]) -> dict[str, Any]:
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
        "StandardOutPath": str(paths["stdout"]),
        "StandardErrorPath": str(paths["stderr"]),
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


def start(root: Path, env: Mapping[str, str], *, keep_awake: bool, accept: bool) -> None:
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
    candidate = _manifest(root, revision, data, keep_awake)
    plist = _plist(root, paths, candidate)

    if _local_state_directory(root) != paths["base"]:
        raise ServiceError("Yerel servis dizini doğrulanamadı.")
    for log_name in ("stdout", "stderr"):
        log_path = paths[log_name]
        _safe_existing_file(log_path)
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(log_path, flags, 0o600)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise ServiceError("Servis günlük dosyası güvenli değil.")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
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


def status(root: Path) -> None:
    paths = service_paths(root.resolve())
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
    }
    if manifest is not None:
        configured_root = Path(str(manifest["repo_root"]))
        payload.update(
            {
                "configured_revision": manifest["revision"],
                "configured_repo_root": str(configured_root),
                "configured_data_dir": manifest["data_dir"],
                "manifest_matches_this_checkout": configured_root.resolve() == root.resolve(),
            }
        )
    print(json.dumps(payload, indent=2, sort_keys=True))


def runtime(manifest_path: Path) -> None:
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
    arguments = ["/bin/bash", str(root / "scripts" / "run-local.sh")]
    if manifest["keep_awake"]:
        arguments.append("--keep-awake")
    os.execve(arguments[0], arguments, environment)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="SpotLab macOS kullanıcı oturumu servisi")
    commands = result.add_subparsers(
        dest="command", required=True, metavar="{start,status,stop}"
    )
    start_parser = commands.add_parser("start", help="Arka planda başlatma isteği gönder")
    start_parser.add_argument("--keep-awake", action="store_true")
    start_parser.add_argument("--accept-current-version", action="store_true")
    commands.add_parser("status", help="Servis kaydını salt okunur kontrol et")
    commands.add_parser("stop", help="SpotLab arka plan servisini durdur")
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
                    )
                    print("SpotLab kullanıcı oturumu servisi başlatma talebi kabul edildi.")
                else:
                    stop()
                    print("SpotLab kullanıcı oturumu servisi durduruldu; veriler korundu.")
    except (ServiceError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
