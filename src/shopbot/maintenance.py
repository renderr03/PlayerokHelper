"""Allowlisted diagnostics and retention of completed update artifacts."""

from __future__ import annotations

import io
import json
import math
import platform
import re
import shutil
import sys
import zipfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import __version__
from .updates import read_json


def connection_diagnostics(db):
    from curl_cffi.requests.impersonate import DEFAULT_CHROME

    packages = {}
    for name in ("curl_cffi", "httpx"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = "not_installed"
    result = {"transport": "curl_cffi", "profile": DEFAULT_CHROME, "packages": packages}
    snapshot = db.get("connection_probe")
    if not isinstance(snapshot, dict):
        return result
    # Do not export arbitrary settings, headers, exceptions or response bodies.
    checks = []
    source = snapshot.get("checks")
    for check in source[:2] if isinstance(source, list) else []:
        if not isinstance(check, dict):
            continue
        status = check.get("status")
        if (
            not isinstance(check.get("endpoint"), str)
            or check["endpoint"] not in {"site", "api"}
            or not isinstance(check.get("kind"), str)
            or check["kind"] not in {"html", "json", "other", "timeout", "network"}
            or not (status is None or type(status) is int and 100 <= status <= 599)
        ):
            continue
        checks.append(
            {
                "endpoint": check["endpoint"],
                "status": status,
                "kind": check["kind"],
                "security": check.get("security") is True,
                "api_ok": check.get("api_ok") is True,
            }
        )
    at = snapshot.get("at")
    if type(at) in {int, float} and 0 < at < 100_000_000_000:
        result["probe"] = {
            "at": at,
            "checks": checks,
            "transport": "curl_cffi" if snapshot.get("transport") == "curl_cffi" else "unknown",
        }
    return result


def workflow_diagnostics(db):
    from .listing_policy import EXTERNAL_MODES, auto_hide_external, safe_reason

    lots = []
    for row in db.all(
        "SELECT item_id,module,fulfillment,paused,hidden_by_bot,hide_reason FROM bindings LIMIT 200"
    ):
        # Only identifiers and enums; titles, instructions and keys are private.
        if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", row["item_id"]):
            continue
        mode = row["fulfillment"] or row["module"]
        if mode not in {"stock", "kosell", "extend", "own_rent", "own_extend", "stars", "smm"}:
            continue
        event = db.get("last_auto_hide:" + row["item_id"])
        last_hide = None
        if (
            isinstance(event, dict)
            and type(event.get("at")) in (int, float)
            and math.isfinite(event["at"])
            and 0 < event["at"] < 100_000_000_000
        ):
            last_hide = {
                "reason": safe_reason(event.get("reason")),
                "at": event["at"],
                "confirmed": event.get("confirmed") is True,
            }
        lots.append(
            {
                "item_id": row["item_id"],
                "mode": mode,
                "delivery_paused": bool(row["paused"]),
                "hidden_by_bot": bool(row["hidden_by_bot"]),
                "availability_reason": safe_reason(row["hide_reason"]),
                "external_auto_hide": auto_hide_external(db, row["item_id"])
                if mode in EXTERNAL_MODES
                else None,
                "hide_intent": db.get("visibility_intent:" + row["item_id"]) == "hide",
                "last_auto_hide": last_hide,
            }
        )
    pending = db.get("visibility_test_pending")
    phase = pending.get("phase", "legacy") if isinstance(pending, dict) else None
    phases = {"legacy", "hide_requested", "hide_verified", "restore_requested", "restore_rejected"}
    status = db.get("visibility_last_status")
    statuses = {
        "APPROVED",
        "DISCONTINUED",
        "SOLD",
        "REMOVED",
        "BLOCKED",
        "EXPIRED",
        "DRAFT",
        "PENDING_MODERATION",
        "PENDING_APPROVAL",
        "PENDING_STATUS_PAYMENT",
        "DECLINED",
    }
    check = db.get("kosell_last_check")
    safe_check = None
    if isinstance(check, dict):
        at = check.get("at")
        safe_check = {
            "ok": check.get("ok") is True,
            "reason": check.get("reason")
            if isinstance(check.get("reason"), str)
            and check.get("reason")
            in {"ok", "timeout", "auth", "unavailable", "invalid_response", "not_configured"}
            else "unknown",
            "at": at if type(at) in {int, float} and 0 < at < 100_000_000_000 else None,
        }
    error = db.get("visibility_last_error")
    safe_error = None
    if isinstance(error, str) and (
        error
        in {
            "timeout",
            "invalid_response",
            "visibility_not_verified",
            "visibility_restore_uncertain",
            "visibility_wrong_account",
            "visibility_recovery_required",
            "visibility_contract_missing",
            "playerok_visibility_network",
            "playerok_visibility_invalid_response",
            "playerok_visibility_failed",
            "playerok_network",
            "playerok_security_check",
            "playerok_rate_limit",
            "playerok_invalid_response",
            "test_requires_own_active_listing",
            "balance_decreased",
            "unexpected",
        }
        or re.fullmatch(r"playerok_(?:visibility_)?http_[45][0-9]{2}", error)
    ):
        safe_error = error
    return {
        "lots": lots,
        "visibility": {
            "pending": bool(pending),
            "last_error": safe_error,
            "phase": phase if isinstance(phase, str) and phase in phases else "unknown",
            "last_status": status if isinstance(status, str) and status in statuses else None,
        },
        "kosell_check": safe_check,
        "uncertain_sends_by_channel": db.all(
            "SELECT channel,count(*) AS count FROM outbox WHERE state='uncertain' "
            "AND channel IN ('telegram','playerok') GROUP BY channel"
        ),
    }


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
        "connection": connection_diagnostics(db),
        "workflows": workflow_diagnostics(db),
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
