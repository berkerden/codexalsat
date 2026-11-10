from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from spotlab.engine import empty_state

REPO_ROOT = Path(__file__).resolve().parents[2]
PAPER_STATE = {
    "mode": "suggestions", "entries_enabled": False, "config": None, "cash": "0",
    "equity": "0", "peak": "0", "day_start_equity": "0", "day": "",
    "daily_trades": 0, "loss_streak": 0, "position": None, "orders": [],
    "fills": [], "audit": [], "last_signal_time": 0, "reason": "",
    "expires_at": 0, "daily_loss": "0", "drawdown": "0", "realized_pnl": "0",
}


def write_paper_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE bot_state (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO bot_state (id, payload) VALUES (1, ?)", (json.dumps(PAPER_STATE),)
        )


@pytest.fixture
def service() -> ModuleType:
    path = REPO_ROOT / "scripts" / "macos_service.py"
    spec = importlib.util.spec_from_file_location("macos_service_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def macos_platform(service: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.sys, "platform", "darwin")


@pytest.fixture(autouse=True)
def fake_user_home(
    service: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    home = tmp_path / "home"
    (home / "Library" / "Logs").mkdir(parents=True)
    monkeypatch.setattr(
        service.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(home))
    )
    return home.resolve()


@pytest.fixture
def fake_repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    (root / ".venv" / "bin" / "python").chmod(0o700)
    data = root / "data"
    data.mkdir()
    write_paper_database(data / "paper.sqlite")
    return root.resolve(), data.resolve()


def completed(argv: tuple[str, ...], returncode: int = 0, stdout: str = ""):
    return subprocess.CompletedProcess(argv, returncode, stdout, "")


def prepare_start(service, monkeypatch, root: Path, revision: str = "a" * 40):
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(service, "source_state", lambda candidate: (revision, root))
    monkeypatch.setattr(
        service,
        "_runtime_environment",
        lambda candidate, data: {
            "HOME": "/Users/example",
            "PATH": "/usr/bin:/bin",
            "SPOTLAB_DATA_DIR": str(data),
            "DATABASE_URL": f"sqlite:///{data / 'paper.sqlite'}",
            "SPOTLAB_TESTNET_DATABASE_URL": f"sqlite:///{data / 'testnet-orders.sqlite'}",
            "PYTHONPATH": f"{root / 'backend/src'}:{root}",
        },
    )

    def run(argv, **kwargs):
        command = tuple(argv)
        calls.append(command)
        return completed(command)

    monkeypatch.setattr(service, "_run", run)
    return calls


def test_source_state_requires_clean_exact_repository(service, monkeypatch, tmp_path: Path) -> None:
    root = tmp_path.resolve()
    outputs = {
        ("rev-parse", "--show-toplevel"): str(root),
        ("rev-parse", "HEAD"): "1" * 40,
        ("status", "--porcelain", "--untracked-files=all"): "?? new-file",
    }
    monkeypatch.setattr(service, "_git", lambda candidate, *args: outputs[args])

    with pytest.raises(service.ServiceError, match="değişmiş"):
        service.source_state(root)

    outputs[("status", "--porcelain", "--untracked-files=all")] = ""
    assert service.source_state(root) == ("1" * 40, root)


def test_existing_data_is_pinned_and_testnet_removal_is_rejected(
    service, fake_repo, monkeypatch
) -> None:
    root, data = fake_repo
    other = root / "other"
    other.mkdir()
    write_paper_database(other / "paper.sqlite")
    previous = service._manifest(root, "a" * 40, data, False)

    with pytest.raises(service.ServiceError, match="çakışıyor"):
        service._data_for_start(root, previous, {"SPOTLAB_DATA_DIR": str(other)})

    previous["testnet_ledger_present"] = True
    with pytest.raises(service.ServiceError, match="Testnet"):
        service._data_for_start(root, previous, {})

    (data / "testnet-orders.sqlite").write_bytes(b"")
    previous["testnet_ledger_present"] = False
    with pytest.raises(service.ServiceError, match="boş"):
        service._data_for_start(root, previous, {})


def test_relative_data_directory_is_resolved_from_repo(service, fake_repo) -> None:
    root, data = fake_repo
    assert service._data_for_start(root, None, {"SPOTLAB_DATA_DIR": "data"}) == data


def test_non_spotlab_sqlite_is_rejected(service, tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with sqlite3.connect(data / "paper.sqlite") as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")

    with pytest.raises(service.ServiceError, match="SpotLab işlem defteri"):
        service._canonical_data_dir(data)


def test_current_engine_state_is_accepted_read_only(service, tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    database = data / "paper.sqlite"
    current = empty_state()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE bot_state (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO bot_state (id, payload) VALUES (1, ?)", (json.dumps(current),)
        )
    bytes_before = database.read_bytes()

    assert service._canonical_data_dir(data) == data.resolve()

    assert database.read_bytes() == bytes_before
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        saved = connection.execute("SELECT payload FROM bot_state WHERE id = 1").fetchone()
    assert saved is not None
    assert json.loads(saved[0]) == current


def test_pinned_directory_rejects_symlink_replacement(service, tmp_path: Path) -> None:
    original = tmp_path / "original"
    replacement = tmp_path / "replacement"
    original.mkdir()
    replacement.mkdir()
    original.rmdir()
    original.symlink_to(replacement, target_is_directory=True)

    with pytest.raises(service.ServiceError, match="aynı gerçek yolu"):
        service._pinned_directory(original, description="veri")


def test_sqlite_url_metacharacter_in_data_path_is_rejected(service, tmp_path: Path) -> None:
    data = tmp_path / "ledger?copy"
    data.mkdir()
    write_paper_database(data / "paper.sqlite")

    with pytest.raises(service.ServiceError, match="SQLite URL"):
        service._canonical_data_dir(data)


@pytest.mark.parametrize("field", ["keep_awake", "testnet_ledger_present"])
def test_manifest_requires_real_booleans(service, tmp_path: Path, field: str) -> None:
    manifest = service._manifest(tmp_path.resolve(), "a" * 40, tmp_path.resolve(), False)
    manifest[field] = "false"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(service.ServiceError, match="geçersiz"):
        service._load_manifest(path)


@pytest.mark.parametrize(
    "name",
    [
        "DATABASE_URL",
        "SPOTLAB_TESTNET_DATABASE_URL",
        "CORS_ORIGINS",
        "SPOTLAB_TESTNET_API_KEY",
        "SPOTLAB_TESTNET_API_SECRET",
    ],
)
def test_external_configuration_overrides_are_rejected_without_values(service, name: str) -> None:
    secret = "must-not-appear"
    with pytest.raises(service.ServiceError) as error:
        service._unsupported_overrides({name: secret})
    assert name in str(error.value)
    assert secret not in str(error.value)


def test_changed_revision_requires_explicit_accept_before_bootstrap(
    service, fake_repo, monkeypatch
) -> None:
    root, data = fake_repo
    paths = service.service_paths(root)
    paths["base"].mkdir(parents=True)
    paths["manifest"].write_text(
        json.dumps(service._manifest(root, "a" * 40, data, False)), encoding="utf-8"
    )
    calls = prepare_start(service, monkeypatch, root, revision="b" * 40)

    with pytest.raises(service.ServiceError, match="--accept-current-version"):
        service.start(root, {}, keep_awake=False, accept=False)
    assert calls == []

    service.start(root, {}, keep_awake=True, accept=True)
    assert calls[0][:3] == ("launchctl", "bootstrap", service.domain())
    assert calls[1] == ("launchctl", "kickstart", service.service_target())
    saved = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert saved["revision"] == "b" * 40
    assert saved["keep_awake"] is True


def test_bootstrap_failure_retains_previous_manifest(service, fake_repo, monkeypatch) -> None:
    root, data = fake_repo
    paths = service.service_paths(root)
    paths["base"].mkdir(parents=True)
    previous = service._manifest(root, "a" * 40, data, False)
    paths["manifest"].write_text(json.dumps(previous), encoding="utf-8")
    prepare_start(service, monkeypatch, root)
    monkeypatch.setattr(
        service,
        "_run",
        lambda argv, **kwargs: (
            completed(tuple(argv), 5) if argv[1] == "bootstrap" else completed(tuple(argv))
        ),
    )

    with pytest.raises(service.ServiceError, match="etiketi"):
        service.start(root, {}, keep_awake=True, accept=False)

    assert json.loads(paths["manifest"].read_text(encoding="utf-8")) == previous


def test_kickstart_failure_boots_out_only_acquired_job_and_rolls_back(
    service, fake_repo, monkeypatch
) -> None:
    root, data = fake_repo
    paths = service.service_paths(root)
    paths["base"].mkdir(parents=True)
    previous = service._manifest(root, "a" * 40, data, False)
    paths["manifest"].write_text(json.dumps(previous), encoding="utf-8")
    prepare_start(service, monkeypatch, root)
    calls: list[tuple[str, ...]] = []

    def run(argv, **kwargs):
        command = tuple(argv)
        calls.append(command)
        return completed(command, 4 if command[1] == "kickstart" else 0)

    monkeypatch.setattr(service, "_run", run)
    with pytest.raises(service.ServiceError, match="başlatılamadı"):
        service.start(root, {}, keep_awake=True, accept=False)

    assert calls == [
        ("launchctl", "bootstrap", service.domain(), str(paths["plist"])),
        ("launchctl", "kickstart", service.service_target()),
        ("launchctl", "bootout", service.service_target()),
    ]
    assert json.loads(paths["manifest"].read_text(encoding="utf-8")) == previous


def test_uncertain_rollback_preserves_candidate_manifest(service, fake_repo, monkeypatch) -> None:
    root, data = fake_repo
    paths = service.service_paths(root)
    paths["base"].mkdir(parents=True)
    previous = service._manifest(root, "a" * 40, data, False)
    paths["manifest"].write_text(json.dumps(previous), encoding="utf-8")
    prepare_start(service, monkeypatch, root)

    def run(argv, **kwargs):
        command = tuple(argv)
        if command[1] in {"kickstart", "bootout"}:
            return completed(command, 4)
        return completed(command)

    monkeypatch.setattr(service, "_run", run)
    with pytest.raises(service.ServiceError, match="temizlendiği doğrulanamadı"):
        service.start(root, {}, keep_awake=True, accept=False)

    candidate = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert candidate["keep_awake"] is True
    assert candidate["revision"] == "a" * 40


def test_private_files_and_safe_plist(
    service, fake_repo, fake_user_home, monkeypatch
) -> None:
    root, _ = fake_repo
    prepare_start(service, monkeypatch, root)
    service.start(root, {}, keep_awake=False, accept=False)
    paths = service.service_paths(root)
    logs = service.log_paths(fake_user_home)

    assert paths["base"].stat().st_mode & 0o777 == 0o700
    for name in ("manifest", "plist"):
        assert paths[name].stat().st_mode & 0o777 == 0o600
    assert logs["base"].stat().st_mode & 0o777 == 0o700
    for name in ("stdout", "stderr"):
        assert not logs[name].exists()
    assert not (paths["base"] / "stdout.log").exists()
    assert not (paths["base"] / "stderr.log").exists()
    plist = plistlib.loads(paths["plist"].read_bytes())
    assert plist["RunAtLoad"] is False
    assert plist["KeepAlive"] is False
    assert plist["ExitTimeOut"] == 20
    assert plist["Umask"] == 0o077
    assert plist["AbandonProcessGroup"] is False
    assert plist["ProgramArguments"] == [
        str(root / ".venv/bin/python"),
        str(root / "scripts/macos_service.py"),
        "_run",
        str(paths["manifest"]),
    ]
    assert plist["StandardOutPath"] == "/dev/null"
    assert plist["StandardErrorPath"] == "/dev/null"
    assert "EnvironmentVariables" in plist
    assert not {"SPOTLAB_TESTNET_API_KEY", "SPOTLAB_TESTNET_API_SECRET"} & set(
        plist["EnvironmentVariables"]
    )


def test_log_setup_preserves_shared_directory_modes(
    service, fake_repo, fake_user_home, monkeypatch
) -> None:
    root, _ = fake_repo
    library = fake_user_home / "Library"
    shared_logs = library / "Logs"
    library.chmod(0o755)
    shared_logs.chmod(0o711)
    prepare_start(service, monkeypatch, root)

    service.start(root, {}, keep_awake=False, accept=False)

    assert library.stat().st_mode & 0o777 == 0o755
    assert shared_logs.stat().st_mode & 0o777 == 0o711
    assert service.log_paths(fake_user_home)["base"].stat().st_mode & 0o777 == 0o700


def test_log_directory_symlink_is_rejected(
    service, fake_repo, fake_user_home, monkeypatch, tmp_path: Path
) -> None:
    root, _ = fake_repo
    target = tmp_path / "redirected-logs"
    target.mkdir()
    service.log_paths(fake_user_home)["base"].symlink_to(target, target_is_directory=True)
    prepare_start(service, monkeypatch, root)

    with pytest.raises(service.ServiceError, match="güvenli değil"):
        service.start(root, {}, keep_awake=False, accept=False)


def test_external_data_allows_missing_local_data_directory(
    service, fake_repo, monkeypatch, tmp_path: Path
) -> None:
    root, local_data = fake_repo
    (local_data / "paper.sqlite").unlink()
    local_data.rmdir()
    external = tmp_path / "external"
    external.mkdir()
    write_paper_database(external / "paper.sqlite")
    prepare_start(service, monkeypatch, root)

    service.start(
        root, {"SPOTLAB_DATA_DIR": str(external)}, keep_awake=False, accept=False
    )

    assert service.service_paths(root)["base"].is_dir()


def test_status_is_read_only_and_does_not_expose_launchctl_output(
    service, fake_repo, fake_user_home, monkeypatch, capsys
) -> None:
    root, _ = fake_repo
    paths = service.service_paths(root)
    logs = service.log_paths(fake_user_home)
    assert not paths["base"].exists()
    assert not logs["base"].exists()
    raw = "private launchctl environment should not be printed"
    monkeypatch.setattr(
        service, "_run", lambda argv, **kwargs: completed(tuple(argv), 1, raw)
    )

    service.status(root)

    output = capsys.readouterr().out
    assert not paths["base"].exists()
    assert not logs["base"].exists()
    assert raw not in output
    assert json.loads(output)["global_job"] == "not-confirmed"
    assert json.loads(output)["local_manifest"] == "absent"
    assert json.loads(output)["logs_dir"] == str(logs["base"])


def test_status_treats_launchctl_timeout_as_unknown(
    service, fake_repo, monkeypatch, capsys
) -> None:
    root, _ = fake_repo
    monkeypatch.setattr(
        service,
        "_run",
        lambda argv, **kwargs: (_ for _ in ()).throw(service.ServiceError("launchctl timeout")),
    )

    service.status(root)

    output = json.loads(capsys.readouterr().out)
    assert output["global_job"] == "not-confirmed"


def test_status_treats_malformed_snapshot_as_stale_without_probe(
    service, fake_repo, monkeypatch, capsys
) -> None:
    root, data = fake_repo
    paths = service.service_paths(root)
    paths["base"].mkdir(parents=True)
    paths["manifest"].write_text(
        json.dumps(service._manifest(root, "a" * 40, data, False)), encoding="utf-8"
    )
    paths["runtime"].write_text(
        json.dumps(
            {
                "schema_version": True,
                "run_id": "not-an-id",
                "revision": "a" * 40,
                "root": str(root),
                "supervisor_pid": 10**100,
                "phase": [],
                "checked_at": float("inf"),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(service, "_fresh_health", lambda identity: pytest.fail("no probe"))
    monkeypatch.setattr(service, "_run", lambda argv, **kwargs: completed(tuple(argv)))

    service.status(root)

    output = json.loads(capsys.readouterr().out)
    assert output["service_state"] == "stale"
    assert output["current_check"] == {"responsive": False}


def test_stop_targets_only_fixed_user_session_job(service, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    timeouts: list[int] = []
    monkeypatch.setattr(
        service,
        "_run",
        lambda argv, **kwargs: (
            calls.append(tuple(argv)),
            timeouts.append(kwargs["timeout"]),
            completed(tuple(argv)),
        )[2],
    )

    service.stop()

    assert calls == [("launchctl", "bootout", service.service_target())]
    assert timeouts == [service.STOP_TIMEOUT]


def test_non_macos_rejected_before_start_mutation(service, fake_repo, monkeypatch) -> None:
    root, _ = fake_repo
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(
        service,
        "source_state",
        lambda candidate: pytest.fail("source inspection must not run"),
    )

    with pytest.raises(service.ServiceError, match="macOS"):
        service.start(root, {}, keep_awake=False, accept=False)

    assert not service.service_paths(root)["base"].exists()


@pytest.mark.parametrize("notify", [False, True])
def test_runtime_uses_sanitized_environment_and_current_manifest(
    service, fake_repo, monkeypatch, notify
) -> None:
    root, data = fake_repo
    manifest = service._manifest(root, "a" * 40, data, True, notify)
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(service, "source_state", lambda candidate: ("a" * 40, root))
    safe_env = {"HOME": "/Users/example", "PATH": "/usr/bin", "SPOTLAB_DATA_DIR": str(data)}
    monkeypatch.setattr(service, "_runtime_environment", lambda candidate, selected: safe_env)
    captured: dict[str, object] = {}

    def execve(program, argv, env):
        captured.update(program=program, argv=argv, env=env)
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(os, "execve", execve)
    with pytest.raises(RuntimeError, match="intercepted"):
        service.runtime(path)

    instance = captured["env"]["SPOTLAB_SERVICE_INSTANCE_ID"]
    assert len(instance) == 32
    int(instance, 16)
    assert captured == {
        "program": str(root / ".venv/bin/python"),
        "argv": [
            str(root / ".venv/bin/python"),
            str(root / "scripts/service_supervisor.py"),
            "--root", str(root),
            "--snapshot", str(root / "data/service/runtime.json"),
            "--revision", "a" * 40,
            "--keep-awake",
        ] + (["--notify-local"] if notify else []),
        "env": safe_env,
    }


def loaded_job_text(service, root: Path) -> str:
    paths = service.service_paths(root)
    arguments = [str(root / '.venv/bin/python'), str(root / 'scripts/macos_service.py'),
                 '_run', str(paths['manifest'])]
    return (service.service_target() + ' = {\n'
            + '\tpath = ' + str(paths['plist']) + '\n'
            + '\tprogram = ' + arguments[0] + '\n'
            + '\tworking directory = ' + str(root) + '\n'
            + '\targuments = {\n' + ''.join('\t\t' + arg + '\n' for arg in arguments)
            + '\t}\n}\n')


def prepare_restart(service, fake_repo, monkeypatch):
    root, data = fake_repo
    paths = service.service_paths(root)
    paths['base'].mkdir()
    manifest = service._manifest(root, 'a' * 40, data, True, True)
    paths['manifest'].write_text(json.dumps(manifest))
    home = service._user_home()
    (home / 'Library/Logs/SpotLab').mkdir()
    for name in ('scripts/run-local.sh', 'scripts/service_supervisor.py',
                 'scripts/node-env.sh', 'frontend/node_modules/vite/bin/vite.js',
                 '.venv/bin/uvicorn', '.tools/npm/bin/npm-cli.js'):
        path = root / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text('fixture')
        path.chmod(0o700)
    monkeypatch.setattr(service, 'source_state', lambda _: ('a' * 40, root))
    monkeypatch.setattr(service, '_runtime_environment', lambda *_: {})
    monkeypatch.setattr(service, '_wait_stopped', lambda pid: None)
    access = os.access
    monkeypatch.setattr(service.os, 'access', lambda path, mode: (
        True if path == '/usr/bin/caffeinate' else access(path, mode)))
    monkeypatch.setattr(service, '_run', lambda argv, **kw: completed(
        tuple(argv), stdout=loaded_job_text(service, root)))
    return root, data, paths, manifest


def test_notifications_legacy_default_and_strict_flag(service, fake_repo) -> None:
    root, data = fake_repo
    manifest = service._manifest(root, 'a' * 40, data, False)
    assert manifest['notify_local'] is False
    path = root / 'legacy.json'
    del manifest['notify_local']
    path.write_text(json.dumps(manifest))
    assert service._load_manifest(path).get('notify_local', False) is False
    for bad in ('false', 1, None, []):
        manifest['notify_local'] = bad
        path.write_text(json.dumps(manifest))
        with pytest.raises(service.ServiceError):
            service._load_manifest(path)


@pytest.mark.parametrize('legacy', [False, True])
def test_restart_preserves_settings_without_http_dependency(
    service, fake_repo, monkeypatch, legacy
):
    root, _, paths, manifest = prepare_restart(service, fake_repo, monkeypatch)
    if legacy:
        del manifest['notify_local']
        paths['manifest'].write_text(json.dumps(manifest))
    events = []
    monkeypatch.setattr(service, '_fresh_health', lambda _: pytest.fail('no HTTP dependency'))
    monkeypatch.setattr(service, 'stop', lambda: events.append('stop'))
    monkeypatch.setattr(service, '_wait_stopped', lambda pid: events.append('wait'))
    monkeypatch.setattr(service, 'start', lambda *a, **kw: events.append(kw))
    service.restart(root, {})
    assert events == [
        'stop', 'wait', {'keep_awake': True, 'accept': False, 'notify_local': not legacy}
    ]


@pytest.mark.parametrize('fault', ['revision', 'dirty', 'missing_manifest', 'bad_manifest',
                                  'missing_data', 'missing_testnet', 'retargeted_data',
                                  'environment', 'python', 'uvicorn', 'caffeinate', 'npm',
                                  'vite', 'unsafe_logs', 'node',
                                  'foreign_job', 'ambiguous_job'])
def test_restart_preflight_faults_never_stop(
    service, fake_repo, monkeypatch, fault: str
) -> None:
    root, data, paths, manifest = prepare_restart(service, fake_repo, monkeypatch)
    env = {}
    if fault == 'revision':
        monkeypatch.setattr(service, 'source_state', lambda _: ('b' * 40, root))
    elif fault == 'dirty':
        def dirty(_):
            raise service.ServiceError('dirty')
        monkeypatch.setattr(service, 'source_state', dirty)
    elif fault == 'missing_manifest':
        paths['manifest'].unlink()
    elif fault == 'bad_manifest':
        paths['manifest'].write_text('not json')
    elif fault == 'missing_data':
        (data / 'paper.sqlite').unlink()
    elif fault == 'missing_testnet':
        manifest['testnet_ledger_present'] = True
        paths['manifest'].write_text(json.dumps(manifest))
    elif fault == 'retargeted_data':
        link = root / 'link'
        link.symlink_to(data, target_is_directory=True)
        manifest['data_dir'] = str(link)
        paths['manifest'].write_text(json.dumps(manifest))
    elif fault == 'environment':
        env = {'DATABASE_URL': 'unsupported'}
    elif fault == 'python':
        (root / '.venv/bin/python').unlink()
    elif fault == 'uvicorn':
        (root / '.venv/bin/uvicorn').unlink()
    elif fault == 'caffeinate':
        access = os.access
        monkeypatch.setattr(service.os, 'access', lambda path, mode: (
            False if path == '/usr/bin/caffeinate' else access(path, mode)))
    elif fault == 'npm':
        (root / '.tools/npm/bin/npm-cli.js').unlink()
    elif fault == 'vite':
        (root / 'frontend/node_modules/vite/bin/vite.js').unlink()
    elif fault == 'unsafe_logs':
        logs = service.log_paths(service._user_home())['base']
        (logs / 'stdout.log').symlink_to(data / 'paper.sqlite')
    elif fault == 'node':
        def invalid(*_):
            raise service.ServiceError('runtime missing')
        monkeypatch.setattr(service, '_runtime_environment', invalid)
    elif fault in {'foreign_job', 'ambiguous_job'}:
        text = loaded_job_text(service, root / 'other') if fault == 'foreign_job' else (
            loaded_job_text(service, root) + '\tpath = duplicate\n')
        monkeypatch.setattr(service, '_run', lambda argv, **kw: completed(tuple(argv), stdout=text))
    monkeypatch.setattr(service, 'stop', lambda: pytest.fail('preflight must not stop'))
    monkeypatch.setattr(service, 'start', lambda *a, **kw: pytest.fail('must not start'))
    with pytest.raises((service.ServiceError, OSError)):
        service.restart(root, env)


def test_restart_stop_failure_prevents_start_and_start_failure_is_explicit(
    service, fake_repo, monkeypatch
) -> None:
    root, _, _, _ = prepare_restart(service, fake_repo, monkeypatch)
    def failed_stop():
        raise service.ServiceError('stop failed')
    monkeypatch.setattr(service, 'stop', failed_stop)
    monkeypatch.setattr(service, 'start', lambda *a, **kw: pytest.fail('must not start'))
    with pytest.raises(service.ServiceError, match='stop failed'):
        service.restart(root, {})
    monkeypatch.setattr(service, 'stop', lambda: None)
    def failed_start(*a, **kw):
        raise service.ServiceError('start failed')
    monkeypatch.setattr(service, 'start', failed_start)
    with pytest.raises(service.ServiceError, match='talebi kabul edildi ancak'):
        service.restart(root, {})


def test_restart_cli_holds_one_lock_and_rejects_version_override(service, monkeypatch):
    from contextlib import contextmanager
    events = []
    @contextmanager
    def lock():
        events.append('lock')
        yield
        events.append('unlock')
    monkeypatch.setattr(service, 'mutation_lock', lock)
    monkeypatch.setattr(service, 'restart', lambda *a: events.append('restart'))
    assert service.main(['restart']) == 0
    assert events == ['lock', 'restart', 'unlock']
    with pytest.raises(SystemExit):
        service.parser().parse_args(['restart', '--accept-current-version'])


@pytest.mark.parametrize('fault', ['program', 'working directory', 'arguments',
                                  'missing_arguments', 'duplicate_arguments',
                                  'unloaded', 'timeout'])
def test_restart_rejects_partial_ownership_mismatches(service, fake_repo, monkeypatch, fault):
    root, _, _, _ = prepare_restart(service, fake_repo, monkeypatch)
    text = loaded_job_text(service, root)
    if fault in {'program', 'working directory'}:
        text = text.replace('\t' + fault + ' = ', '\t' + fault + ' = foreign-')
    elif fault == 'arguments':
        text = text.replace('\t\t_run\n', '\t\t_other\n')
    elif fault == 'missing_arguments':
        text = text.replace('\targuments = {\n', '\tmissing = {\n')
    elif fault == 'duplicate_arguments':
        text += '\targuments = {\n'
    def run(argv, **kw):
        if fault == 'timeout':
            raise service.ServiceError('timeout')
        return completed(tuple(argv), returncode=1 if fault == 'unloaded' else 0, stdout=text)
    monkeypatch.setattr(service, '_run', run)
    monkeypatch.setattr(service, 'stop', lambda: pytest.fail('must not stop'))
    with pytest.raises(service.ServiceError):
        service.restart(root, {})


def test_restart_waits_for_both_old_pid_and_confirmed_missing_job(service, monkeypatch):
    clock = [0.0]
    calls = []
    monkeypatch.setattr(service.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(service.time, 'sleep',
                        lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    missing = (f'Bad request.\nCould not find service "{service.LABEL}" '
               f'in domain for user gui: {os.getuid()}\n')
    def run(argv, **kw):
        calls.append(kw['timeout'])
        if len(calls) < 2:
            return completed(tuple(argv))
        return subprocess.CompletedProcess(argv, 113, '', missing)
    def kill(pid, signal):
        assert pid == 123 and signal == 0
        if len(calls) == 2:
            raise PermissionError('not proof of exit')
        if len(calls) >= 3:
            raise ProcessLookupError('exited')
    monkeypatch.setattr(service, '_run', run)
    monkeypatch.setattr(service.os, 'kill', kill)
    service._wait_stopped(123)
    assert len(calls) == 3
    assert all(timeout <= 2 for timeout in calls)


def test_restart_unknown_removal_and_timeout_never_start(service, fake_repo, monkeypatch):
    root, _, _, _ = prepare_restart(service, fake_repo, monkeypatch)
    events = []
    monkeypatch.setattr(service, 'stop', lambda: events.append('stop'))
    def not_stopped(pid):
        raise service.ServiceError('not confirmed')
    monkeypatch.setattr(service, '_wait_stopped', not_stopped)
    monkeypatch.setattr(service, 'start', lambda *a, **kw: pytest.fail('must not start'))
    with pytest.raises(service.ServiceError, match='talebi kabul edildi ancak'):
        service.restart(root, {})
    assert events == ['stop']


def test_removal_barrier_rejects_generic_print_error_and_has_deadline(service, monkeypatch):
    monkeypatch.setattr(service, '_run', lambda argv, **kw: completed(tuple(argv), returncode=1))
    with pytest.raises(service.ServiceError, match='kaldırıldığı'):
        service._wait_stopped(None)
    clock = [0.0]
    monkeypatch.setattr(service, 'STOP_TIMEOUT', 0.3)
    monkeypatch.setattr(service.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(service.time, 'sleep',
                        lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    monkeypatch.setattr(service, '_run', lambda argv, **kw: completed(tuple(argv)))
    with pytest.raises(service.ServiceError, match='süresi'):
        service._wait_stopped(None)
    assert clock[0] == 0.3
