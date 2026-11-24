import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from shopbot import __version__, updater_worker
from shopbot.models import now
from shopbot.runtime import Runtime, health_loop
from shopbot.updates import Updates, release_summary, version_tuple

NONCE = "a" * 32
SHA = "b" * 40
TAG = "v0.6.0"


@pytest.fixture(autouse=True)
def stable_update_contract(monkeypatch):
    # Test the stable updater even when the working package is a beta build.
    monkeypatch.setattr("shopbot.updates.__version__", __version__.split("b")[0])


def transport(*, annotated=False, failure=False, body="Новое обновление"):
    def handle(request):
        assert request.url.host == "api.github.com"
        if failure:
            return httpx.Response(403, json={"message": "PRIVATE_TOKEN_MUST_NOT_LEAK"})
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(
                200, json={"tag_name": TAG, "body": body, "draft": False, "prerelease": False}
            )
        if "/git/ref/" in request.url.path:
            return httpx.Response(
                200, json={"object": {"type": "tag" if annotated else "commit", "sha": SHA}}
            )
        return httpx.Response(200, json={"object": {"type": "commit", "sha": SHA}})

    return httpx.MockTransport(handle)


@pytest.mark.parametrize("annotated", [False, True])
async def test_discovery_deduplicates_and_pins_commit(app, tmp_path, annotated):
    _, db, _, _ = app
    db.set("admins", [42])
    updates = Updates(db, tmp_path, transport=transport(annotated=annotated))
    release = await updates.check()
    assert release["sha"] == SHA and release["tag"] == TAG
    db.set("update_check_at", 0)
    await Updates(db, tmp_path, transport=transport()).check()
    assert db.one("SELECT count(*) n FROM outbox WHERE purpose='update'")["n"] == 1
    assert release["url"] == "https://github.com/renderr03/PlayerokHelper/releases/tag/v0.6.0"


def test_release_summary_keeps_complete_lines_without_markdown_or_install_commands():
    body = (
        "<!-- private editorial comment -->\n## Что нового\n\n"
        "- **Своя аренда:** платное продление.\n"
        "- __Чаты:__ команда ` /inbox `.\n"
        "1. [Подробный отчёт](https://example.test/report)\n\n"
        "```bash\ncurl https://example.test/install.sh | bash\n```\n"
        + "- "
        + "Длинный пункт " * 100
        + "\n"
        + "- *Готово*.\n" * 10
    )
    summary = release_summary(body)
    assert summary.startswith("• Своя аренда: платное продление.")
    assert "• Чаты: команда /inbox ." in summary
    assert "• Подробный отчёт" in summary and "• Готово." in summary
    assert not any(mark in summary for mark in ["##", "**", "__", "`", "curl", "<!--", "Длинный"])
    assert len(summary) <= 650 and len(summary.splitlines()) == 6
    assert release_summary("# Заголовок\n\n" + "x" * 1000) == "Изменения и инструкция — в описании релиза."


async def test_release_notification_uses_short_plain_preview(app, tmp_path):
    _, db, _, _ = app
    db.set("admins", [42])
    body = "## Изменения\n- **Чаты:** команда `/inbox`.\n" + "- Следующее улучшение.\n" * 100
    updates = Updates(db, tmp_path, transport=transport(body=body))
    release = await updates.check()
    notification = db.decrypt(db.one("SELECT ciphertext FROM outbox WHERE purpose='update'")["ciphertext"])
    assert release["body"] in notification and release["url"] in notification
    assert "**" not in notification and "##" not in notification and "`" not in notification
    assert notification.startswith("🔔 PlayerokHelper " + TAG)
    assert len(notification) < 1000 and "Нажмите «Обновить бота»" in notification


async def test_check_errors_keep_cache_and_do_not_expose_response(app, tmp_path):
    _, db, _, _ = app
    updates = Updates(db, tmp_path, transport=transport())
    await updates.check()
    db.set("update_check_at", 0)
    updates.transport = transport(failure=True)
    assert (await updates.check())["sha"] == SHA
    assert "PRIVATE_TOKEN" not in db.get("update_check_error")
    db.set("update_notifications", False)
    db.set("update_check_at", 0)
    db.set("admins", [42])
    updates.transport = transport()
    await updates.check()
    assert not db.all("SELECT * FROM outbox")


async def test_current_release_does_not_offer_update_and_checks_are_throttled(app, tmp_path):
    from shopbot.updates import __version__ as installed_version

    _, db, _, _ = app
    calls = []

    def current(request):
        calls.append(request)
        assert request.url.path.endswith("/releases/latest")
        return httpx.Response(
            200, json={"tag_name": "v" + installed_version, "draft": False, "prerelease": False}
        )

    updates = Updates(db, tmp_path, transport=httpx.MockTransport(current))
    assert await updates.check() is None
    assert await updates.check(force=True) is None
    assert len(calls) == 1 and updates.available() is None
    assert not db.all("SELECT * FROM outbox")


def test_requests_are_atomic_single_use_expiring_and_require_helper(app, tmp_path):
    _, db, _, _ = app
    updates = Updates(db, tmp_path)
    db.set("update_release", {"tag": TAG, "sha": SHA, "body": "", "url": ""})
    with pytest.raises(ValueError):
        updates.offer()
    folder = tmp_path / ".updates"
    folder.mkdir()
    (folder / "enabled.json").write_text('{"protocol": 1}')
    offer = updates.offer()
    with pytest.raises(ValueError):
        updates.request("old-button")
    request = updates.request(offer["nonce"])
    assert json.loads((folder / "request.json").read_text()) == request
    assert (folder / "request.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        updates.request(offer["nonce"])
    (folder / "request.json").unlink()
    offer = updates.offer()
    offer["expires"] = now() - 1
    db.set("update_offer", offer)
    with pytest.raises(ValueError):
        updates.request(offer["nonce"])


def test_result_notification_and_history_survive_restart(app, tmp_path):
    _, db, _, _ = app
    db.set("admins", [42])
    folder = tmp_path / ".updates"
    folder.mkdir()
    (folder / "result.json").write_text(
        json.dumps({"nonce": NONCE, "tag": TAG, "status": "rolled_back", "finished_at": now()})
    )
    Updates(db, tmp_path).consume_result()
    Updates(db, tmp_path).consume_result()
    assert db.one("SELECT count(*) n FROM outbox WHERE purpose='update'")["n"] == 1
    assert len(db.get("update_history")) == 1


@pytest.mark.parametrize("value", ["v1.2.3-rc1", "../1.2.3", "v01.2.3", "v1.2.3;bash", "1.2"])
def test_version_validation(value):
    with pytest.raises(ValueError):
        version_tuple(value)


async def test_maintenance_blocks_business_processing_and_emits_health(app, tmp_path):
    import asyncio

    engine, db, _, _ = app
    folder = tmp_path / ".updates"
    folder.mkdir()
    (folder / "maintenance.json").write_text(json.dumps({"nonce": NONCE}))
    engine.recover = lambda: pytest.fail("recovery must wait for update validation")
    engine.poll_orders = AsyncMock(side_effect=AssertionError("orders must be paused"))
    runtime = Runtime(SimpleNamespace(project_dir=tmp_path, updates_enabled=True), db, engine, None)
    await runtime.cycle()
    task = asyncio.create_task(health_loop(tmp_path))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    health = json.loads((folder / "health.json").read_text())
    assert health["nonce"] == NONCE and health["version"] == __version__ and health["pid"] == os.getpid()
    assert not runtime.recovered


def orchestrator_execute(*, fail_prepare=False, bad_boot=False, interrupted=False):
    calls = []
    state = {"request": {"nonce": NONCE, "tag": TAG}, "old_version": "0.3.0"}
    tick = 0
    rolled_back = False

    def execute(args, **kwargs):
        nonlocal tick, rolled_back
        calls.append((args, kwargs))
        if args[0] == "/usr/bin/systemctl":
            return subprocess.CompletedProcess(args, 0, "123\n" if args[1] == "show" else "", "")
        assert kwargs["user"] == os.getuid() and kwargs["group"] == os.getgid()
        assert kwargs["extra_groups"] == []
        assert "--worker" in args and not any("pip" == arg for arg in args)
        phase = args[-1]
        if phase == "inspect":
            value = {**state, "recover": interrupted, "phase": "active" if interrupted else "prepared"}
        elif phase == "prepare":
            if fail_prepare:
                raise subprocess.CalledProcessError(1, args)
            value = state
        elif phase == "rollback":
            rolled_back = True
            value = {**state, "phase": "restored"}
        elif phase == "health":
            tick += 1
            value = {
                "nonce": NONCE,
                "pid": 999 if bad_boot and not rolled_back else 123,
                "version": "0.3.0" if rolled_back else "0.6.0",
                "at": time.time() - 1 + tick * 0.000001,
            }
        elif phase.startswith("finish:"):
            value = {"status": phase.split(":")[1]}
        else:
            value = state
        return subprocess.CompletedProcess(args, 0, json.dumps(value), "")

    return execute, calls


@pytest.mark.parametrize(
    "scenario,expected",
    [("ok", "success"), ("prepare", "failed"), ("boot", "rolled_back"), ("interrupted", "rolled_back")],
)
def test_manager_runs_repository_tasks_as_owner_and_rolls_back(scenario, expected):
    execute, calls = orchestrator_execute(
        fail_prepare=scenario == "prepare", bad_boot=scenario == "boot", interrupted=scenario == "interrupted"
    )
    ticks = iter(range(0, 10000, 10))
    result = updater_worker.orchestrate(
        Path("/fixed/config.json"),
        {"uid": os.getuid(), "gid": os.getgid()},
        execute=execute,
        sleep=lambda _: None,
        clock=lambda: next(ticks),
    )
    assert result["status"] == expected
    controls = [args[1] for args, _ in calls if args[0] == "/usr/bin/systemctl"]
    if scenario == "prepare":
        assert not controls  # failed builds leave the live service running
    elif scenario == "ok":
        assert controls[:2] == ["stop", "start"]
    else:
        assert any(args[-1] == "rollback" for args, _ in calls)


def git(directory, *args):
    return subprocess.check_output(
        ["git", "-C", str(directory), *args], text=True, stderr=subprocess.DEVNULL
    ).strip()


@pytest.fixture
def prepared_worker(tmp_path):
    directory = tmp_path / "project with spaces"
    directory.mkdir()
    git(directory, "init", "-b", "main")
    git(directory, "config", "user.email", "test@example.com")
    git(directory, "config", "user.name", "Test")
    git(directory, "remote", "add", "origin", updater_worker.REPOSITORY)
    (directory / ".gitignore").write_text(".venv\n.updates/\ndata/\n.env\n")
    (directory / "version.txt").write_text("old")
    git(directory, "add", ".")
    git(directory, "commit", "-m", "old")
    old_sha = git(directory, "rev-parse", "HEAD")
    (directory / "version.txt").write_text("new")
    git(directory, "commit", "-am", "new")
    sha = git(directory, "rev-parse", "HEAD")
    git(directory, "reset", "--hard", old_sha)
    folder = directory / ".updates"
    folder.mkdir()
    request = {"nonce": NONCE, "tag": TAG, "sha": sha}
    updater_worker.write(folder / "request.json", request)
    updater_worker.write(
        folder / "prepared.json",
        {"request": request, "old_sha": old_sha, "old_version": "0.3.0", "phase": "prepared"},
    )
    stage = folder / "releases" / NONCE / ".venv"
    stage.mkdir(parents=True)
    (stage / "new-env").write_text("new")
    (directory / ".venv").mkdir()
    (directory / ".venv/bin").mkdir()
    interpreter = directory / ".venv/bin/python"
    interpreter.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
    interpreter.chmod(0o755)
    (directory / ".venv/old-env").write_text("old")
    (directory / ".env").write_text("TOKEN=keep-this-key\n")
    data = directory / "data"
    data.mkdir()
    with sqlite3.connect(data / "shop.sqlite") as connection:
        connection.execute("CREATE TABLE ledger (value TEXT)")
        connection.execute("INSERT INTO ledger VALUES ('before update')")
    config = {"directory": str(directory), "data_dir": str(data), "python": sys.executable}
    return updater_worker.Worker(config), request, old_sha


def test_live_switch_and_rollback_restore_code_environment_database_and_key(prepared_worker):
    worker, request, old_sha = prepared_worker
    worker.activate()
    assert worker.git("rev-parse", "HEAD") == request["sha"]
    assert (worker.directory / ".venv/new-env").exists()
    assert (worker.folder / "maintenance.json").exists()
    with sqlite3.connect(worker.data / "shop.sqlite") as db:
        db.execute("ALTER TABLE ledger ADD COLUMN migrated INTEGER")
        db.execute("UPDATE ledger SET value='new version wrote here'")
    worker.rollback()
    assert worker.git("rev-parse", "HEAD") == old_sha
    assert (worker.directory / ".venv/old-env").exists()
    assert (worker.directory / ".env").read_text() == "TOKEN=keep-this-key\n"
    with sqlite3.connect(worker.data / "shop.sqlite") as db:
        assert db.execute("SELECT * FROM ledger").fetchall() == [("before update",)]
    result = worker.finish("rolled_back")
    assert result["status"] == "rolled_back"
    assert not (worker.folder / "maintenance.json").exists()
    assert not (worker.folder / "request.json").exists()


def test_rollback_recreates_maintenance_gate_after_interruption(prepared_worker):
    worker, _, _ = prepared_worker
    worker.activate()
    (worker.folder / "maintenance.json").unlink()
    worker.rollback()
    assert (worker.folder / "maintenance.json").exists()
    assert worker.state()["phase"] == "restored"


def test_pre_switch_failure_keeps_old_files_and_validates_old_start(prepared_worker):
    worker, _, old_sha = prepared_worker
    state = worker.rollback()
    assert state["phase"] == "restored" and not state["changed"]
    assert (worker.folder / "maintenance.json").exists()
    assert worker.git("rev-parse", "HEAD") == old_sha
    assert (worker.directory / ".venv/old-env").exists()


def test_modified_project_is_not_overwritten(prepared_worker):
    worker, _, old_sha = prepared_worker
    (worker.directory / "version.txt").write_text("local customization")
    with pytest.raises(updater_worker.UpdateError, match="modified_project"):
        worker.activate()
    assert worker.git("rev-parse", "HEAD") == old_sha
    assert (worker.directory / "version.txt").read_text() == "local customization"


def test_invalid_job_is_rejected_and_removed_without_execution(prepared_worker):
    worker, _, _ = prepared_worker
    updater_worker.write(worker.folder / "request.json", {"nonce": "../../etc", "tag": "$(id)", "sha": SHA})
    with pytest.raises(updater_worker.UpdateError):
        worker.request()
    assert worker.finish("failed")["status"] == "failed"
    assert not (worker.folder / "request.json").exists()


def test_prepare_builds_isolated_environment_without_changing_live_files(
    prepared_worker, monkeypatch, tmp_path
):
    worker, request, old_sha = prepared_worker
    # A tiny real wheel keeps the integration test offline while exercising pip,
    # venv creation, git checkout and the version check used in production.
    wheel = tmp_path / "playerokhelper-0.6.0-py3-none-any.whl"
    records = {
        "shopbot/__init__.py": '__version__ = "0.6.0"\n',
        "playerokhelper-0.6.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: PlayerokHelper\nVersion: 0.6.0\n",
        "playerokhelper-0.6.0.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    records["playerokhelper-0.6.0.dist-info/RECORD"] = "".join(f"{name},,\n" for name in records)
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in records.items():
            archive.writestr(name, content)
    original_run = updater_worker.run

    def offline_run(args, **kwargs):
        if "fetch" in args:
            git(worker.directory, "update-ref", "refs/playerokhelper/" + NONCE, request["sha"])
            return ""
        if "pip" in args:
            return original_run(
                [
                    args[0],
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-index",
                    "--no-deps",
                    str(wheel),
                ]
            )
        return original_run(args, **kwargs)

    monkeypatch.setattr(updater_worker, "run", offline_run)
    monkeypatch.setattr(worker, "current_version", lambda: "0.3.0")
    state = worker.prepare()
    assert state["request"]["sha"] == request["sha"]
    assert git(worker.directory, "rev-parse", "HEAD") == old_sha
    assert (worker.directory / ".venv/old-env").exists()
    assert not (worker.folder / "maintenance.json").exists()
    staged = worker.folder / "releases" / NONCE
    assert (staged / ".venv/bin/python").exists()
    assert git(staged, "rev-parse", "HEAD") == request["sha"]


def test_pinned_tag_change_is_rejected_before_build(prepared_worker, monkeypatch):
    worker, request, old_sha = prepared_worker
    original_run = updater_worker.run

    def changed_tag(args, **kwargs):
        if "fetch" in args:
            git(worker.directory, "update-ref", "refs/playerokhelper/" + NONCE, old_sha)
            return ""
        return original_run(args, **kwargs)

    monkeypatch.setattr(updater_worker, "run", changed_tag)
    monkeypatch.setattr(worker, "current_version", lambda: "0.3.0")
    with pytest.raises(updater_worker.UpdateError, match="release_tag_changed"):
        worker.prepare()
    assert git(worker.directory, "rev-parse", "HEAD") == old_sha


def test_changed_data_directory_is_rejected_before_any_switch(prepared_worker):
    worker, _, old_sha = prepared_worker
    (worker.directory / ".env").write_text("DATA_DIR=another-database\n")
    with pytest.raises(updater_worker.UpdateError, match="data_directory_changed"):
        worker.prepare()
    assert worker.git("rev-parse", "HEAD") == old_sha
    assert not (worker.folder / "maintenance.json").exists()
