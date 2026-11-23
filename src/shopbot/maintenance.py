"""Allowlisted diagnostics and retention of completed update artifacts."""

from __future__ import annotations

import io
import json
import platform
import re
import shutil
import sys
import zipfile
from pathlib import Path

from . import __version__
from .updates import read_json


def update_status(directory):
    folder = Path(directory) / ".updates"
    request = read_json(folder / "request.json", {})
    state = read_json(folder / "prepared.json", {})
    if not isinstance(request, dict) or not request:
        if (folder / "maintenance.json").exists():
            return "Режим проверки или восстановления: заказы и настройки приостановлены. Проверьте результат обновления."
        return "Обновление не выполняется."
    phase = (
        state.get("phase")
        if isinstance(state, dict) and state.get("request", {}).get("nonce") == request.get("nonce")
        else "preparing"
    )
    return {
        "preparing": "Подготовка релиза и отдельного окружения; текущий бот продолжает работать.",
        "prepared": "Версия подготовлена; ожидается остановка службы и резервное копирование.",
        "activating": "Создана копия базы; переключается версия.",
        "active": "Проверяется запуск новой версии; заказы временно приостановлены.",
        "restored": "Предыдущая версия восстановлена; проверяется её запуск.",
        "finished": "Обновление завершено; ожидается сообщение с результатом.",
    }.get(phase, "Обновление выполняется.")


def require_update_space(directory, db_path):
    needed = 512 * 1024 * 1024 + (db_path.stat().st_size * 2 if db_path.exists() else 0)
    if shutil.disk_usage(directory).free < needed:
        raise ValueError(
            f"Недостаточно места для обновления: требуется примерно {needed // 1024 // 1024} МиБ свободного места."
        )


def diagnostics(db, directory):
    # Nothing is copied from .env, SQLite files, raw exceptions, chats or journals.
    payload = {
        "version": __version__,
        "python": platform.python_version(),
        "system": platform.system(),
        "free_disk_mib": shutil.disk_usage(directory).free // (1024 * 1024),
        "paused": bool(db.get("paused")),
        "live_enabled": bool(db.get("live_enabled")),
        "orders_by_stage": db.all("SELECT stage,count(*) AS count FROM orders GROUP BY stage"),
        "lots": db.one("SELECT count(*) count FROM bindings")["count"],
        "uncertain_sends": db.one("SELECT count(*) count FROM outbox WHERE state='uncertain'")["count"],
        "update": update_status(directory),
    }
    result = db.get("update_last_result", {})
    if isinstance(result, dict):
        payload["last_update"] = {key: result.get(key) for key in ("tag", "status", "reason", "finished_at")}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("diagnostics.json", json.dumps(payload, ensure_ascii=False, indent=2))
    return stream.getvalue()


def prune_updates(directory, keep=3):
    folder = Path(directory) / ".updates"

    def busy():
        return (folder / "request.json").exists() or (folder / "maintenance.json").exists()

    state = read_json(folder / "prepared.json", {})
    if (
        busy()
        or not isinstance(state, dict)
        or state.get("phase") != "finished"
        or state.get("status") == "manual_required"
    ):
        return 0
    nonce = state.get("request", {}).get("nonce", "")
    if not re.fullmatch(r"[0-9a-f]{32}", nonce):
        return 0
    backups = folder / "backups"
    jobs = sorted(
        [
            p
            for p in backups.glob("*")
            if re.fullmatch(r"[0-9a-f]{32}", p.name) and p.is_dir() and not p.is_symlink()
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    retained = {p.name for p in jobs[:keep]} | {nonce}
    protected = {(Path(directory) / ".venv").resolve(), Path(sys.prefix).resolve()}
    for job in retained:
        saved = folder / ("env-before-" + job)
        if saved.exists():
            protected.add(saved.resolve())
    removed = 0

    def discard(path):
        nonlocal removed
        if busy() or path.resolve() in protected or any(path.resolve() in p.parents for p in protected):
            return
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        removed += 1

    for job in jobs:
        if job.name not in retained:
            discard(job)
            saved = folder / ("env-before-" + job.name)
            if saved.exists() or saved.is_symlink():
                discard(saved)
    for release in (folder / "releases").glob("*"):
        if re.fullmatch(r"[0-9a-f]{32}", release.name) and release.name not in retained:
            discard(release)
    return removed
