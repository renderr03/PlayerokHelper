"""Release discovery and a file-based request protocol for the installed updater."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

import httpx

from . import __version__
from .models import now
from .setup_console import atomic_write

REPOSITORY = "renderr03/PlayerokHelper"
CHECK_INTERVAL = 6 * 3600


def release_summary(body):
    """A compact plain-text preview; complete release notes remain on GitHub."""
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    body = re.sub(r"^\s*(`{3,}|~{3,}).*?^\s*\1\s*$", "", body, flags=re.M | re.S)
    lines, length = [], 0
    for raw in body.splitlines():
        line = raw.strip()
        if not line or re.match(r"^(?:#{1,6}\s|[-*_]{3,}$)", line):
            continue
        if re.match(r"^(?:(?:Отч[её]т|Подробности|Описание):\s*)?https?://\S+$", line):
            continue
        line = re.sub(r"!?\[([^\]]+)\]\([^\n)]*\)", r"\1", line)
        line = re.sub(r"^\s*(?:[-*+] |\d+[.)] )", "• ", line)
        line = line.replace("**", "").replace("__", "").replace("`", "")
        line = re.sub(r"(?<!\w)([*_])([^\n]+?)\1(?!\w)", r"\2", line)
        line = re.sub(r"\s+", " ", line).strip()
        if not line or len(line) > 650:
            continue
        if length + len(line) + bool(lines) > 650 or len(lines) == 6:
            break
        lines.append(line)
        length += len(line) + (len(lines) > 1)
    return "\n".join(lines) or "Изменения и инструкция — в описании релиза."


def version_tuple(value):
    match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value)
    if not match:
        raise ValueError("invalid_version")
    return tuple(map(int, match.groups()))


def version_rank(value):
    match = re.fullmatch(r"v?(\d+\.\d+\.\d+)(?:b([1-9]\d*))?", value)
    if not match:
        raise ValueError("invalid_version")
    base = version_tuple(match[1])
    return (*base, -1, int(match[2])) if match[2] else (*base, 0, 0)


def read_json(path, default=None):
    try:
        if path.stat().st_size > 100_000:
            return default
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


class Updates:
    def __init__(self, db, directory=None, *, transport=None):
        self.db = db
        self.directory = Path(directory).resolve() if directory else None
        self.transport = transport

    @property
    def folder(self):
        return self.directory / ".updates" if self.directory else None

    def available(self):
        release = self.db.get("update_release")
        try:
            return release if release and version_rank(release["tag"]) > version_rank(__version__) else None
        except (ValueError, KeyError, TypeError):
            return None

    def enabled(self):
        if re.fullmatch(r"\d+\.\d+\.\d+b\d+", __version__):
            return False
        value = read_json(self.folder / "enabled.json", {}) if self.folder else {}
        return isinstance(value, dict) and value.get("protocol") == 1

    def busy(self):
        return bool(
            self.folder and any((self.folder / p).exists() for p in ["request.json", "maintenance.json"])
        )

    async def check(self, *, force=False):
        interval = 60 if force else CHECK_INTERVAL
        if now() - self.db.get("update_check_at", 0) < interval:
            return self.available()
        self.db.set("update_check_at", now())
        try:
            async with httpx.AsyncClient(
                base_url="https://api.github.com",
                timeout=12,
                follow_redirects=False,
                headers={"Accept": "application/vnd.github+json", "User-Agent": "PlayerokHelper"},
                transport=self.transport,
            ) as client:
                response = await client.get(f"/repos/{REPOSITORY}/releases/latest")
                if response.status_code == 404:
                    self.db.set("update_release", None)
                    self.db.set("update_check_error", None)
                    return None
                response.raise_for_status()
                release = response.json()
                tag = release["tag_name"]
                version_tuple(tag)
                if release.get("draft") or release.get("prerelease"):
                    raise ValueError()
                if version_rank(tag) <= version_rank(__version__):
                    self.db.set("update_release", None)
                    self.db.set("update_check_error", None)
                    return None
                ref = await client.get(f"/repos/{REPOSITORY}/git/ref/tags/{tag}")
                ref.raise_for_status()
                obj = ref.json()["object"]
                for _ in range(4):
                    if obj["type"] == "commit":
                        break
                    if obj["type"] != "tag" or not re.fullmatch(r"[0-9a-f]{40}", obj["sha"]):
                        raise ValueError()
                    ref = await client.get(f"/repos/{REPOSITORY}/git/tags/{obj['sha']}")
                    ref.raise_for_status()
                    obj = ref.json()["object"]
                if obj["type"] != "commit" or not re.fullmatch(r"[0-9a-f]{40}", obj["sha"]):
                    raise ValueError()
                cached = {
                    "tag": tag,
                    "sha": obj["sha"],
                    "body": release_summary(str(release.get("body") or "")),
                    "url": f"https://github.com/{REPOSITORY}/releases/tag/{tag}",
                    "checked_at": now(),
                }
                self.db.set("update_release", cached)
                self.db.set("update_check_error", None)
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            self.db.set("update_check_error", "GitHub временно недоступен или ответ не распознан.")
            return self.available()
        if self.db.get("update_notifications", True):
            self.db.notify(
                f"🔔 PlayerokHelper {tag}\nУстановлена версия {__version__}.\n\n"
                f"{cached['body']}\n\n"
                "⬆️ Откройте обновление кнопкой ниже. Изменения — в разделе «Что нового».",
                "release:" + tag,
                purpose="update",
            )
        return cached

    def offer(self):
        release = self.available()
        if not release or not self.enabled() or self.busy():
            raise ValueError("Обновление сейчас недоступно.")
        offer = {**release, "nonce": uuid.uuid4().hex, "expires": now() + 600}
        self.db.set("update_offer", offer)
        return offer

    def request(self, nonce):
        offer = self.db.get("update_offer") or {}
        if (
            not self.enabled()
            or self.busy()
            or offer.get("nonce") != nonce
            or offer.get("expires", 0) <= now()
            or self.available() is None
            or offer["sha"] != self.available()["sha"]
        ):
            raise ValueError("Предложение устарело или обновление уже запущено. Проверьте версию снова.")
        from .maintenance import require_update_space

        require_update_space(self.directory, self.db.path)
        request = {"nonce": nonce, "tag": offer["tag"], "sha": offer["sha"], "created_at": now()}
        request["data_dir"] = str(self.db.path.parent.resolve())
        # Publish a complete file atomically. A hard link gives O_EXCL semantics.
        temporary = self.folder / ("request-" + nonce + ".tmp")
        atomic_write(temporary, json.dumps(request))
        try:
            os.link(temporary, self.folder / "request.json")
        except FileExistsError:
            raise ValueError("Обновление уже запущено.") from None
        finally:
            temporary.unlink(missing_ok=True)
        self.db.set("update_offer", None)
        return request

    def consume_result(self):
        result = read_json(self.folder / "result.json") if self.folder else None
        if not isinstance(result, dict) or result.get("nonce") == self.db.get("update_result_seen"):
            return
        if not re.fullmatch(r"[0-9a-f]{32}", str(result.get("nonce", ""))):
            return
        messages = {
            "success": "✅ Обновление установлено и запуск проверен.",
            "rolled_back": "↩️ Новая версия не запустилась. Предыдущая версия и база восстановлены.",
            "failed": "⚠️ Обновление не установлено. Работающая версия сохранена.",
            "manual_required": "🛠 Восстановление остановлено. Откройте статус обновления и инструкцию по восстановлению.",
        }
        if result.get("status") not in messages:
            return
        self.db.set("update_result_seen", result["nonce"])
        self.db.set("update_last_result", result)
        history = self.db.get("update_history", [])
        self.db.set("update_history", [result, *history][:10])
        self.db.notify(
            messages[result["status"]] + " Подробности — по кнопке ниже.",
            "update-result:" + result["nonce"],
            purpose="update",
        )
