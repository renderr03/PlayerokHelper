"""Interactive configuration. No credentials are accepted in command-line arguments."""

from __future__ import annotations

import getpass
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import dotenv_values


@contextmanager
def console():
    # stdin belongs to curl when installing with `curl ... | bash`.
    # Prompts must read the controlling terminal, not the script stream.
    if sys.stdin.isatty():
        yield sys.stdin, sys.stdout
        return
    try:
        tty_input = open("/dev/tty", "r", encoding="utf-8")
        tty_output = open("/dev/tty", "w", encoding="utf-8")
    except OSError:
        raise ValueError("Нужна интерактивная консоль. Запустите playerokhelper setup в терминале.") from None
    try:
        yield tty_input, tty_output
    finally:
        tty_input.close()
        tty_output.close()


class Console:
    def __init__(self, input_stream, output_stream):
        self.input = input_stream
        self.output = output_stream

    def write(self, text):
        print(text, file=self.output, flush=True)

    def ask(self, prompt, *, secret=False):
        if secret:
            return getpass.getpass(prompt, stream=self.output)
        print(prompt, end="", file=self.output, flush=True)
        line = self.input.readline()
        if not line:
            raise EOFError("Ввод прерван")
        return line.strip()


def atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".env." if path.name == ".env" else ".config.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def env_text(original: str, updates: dict[str, str]):
    lines = original.splitlines()
    for key, value in updates.items():
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        line = key + "='" + escaped + "'"
        pattern = re.compile(r"^\s*(?:export\s+)?" + re.escape(key) + r"\s*=")
        indexes = [i for i, old in enumerate(lines) if pattern.match(old)]
        if indexes:
            lines[indexes[0]] = line
            # Remove duplicate assignments which could otherwise override the new value.
            for index in reversed(indexes[1:]):
                lines.pop(index)
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"


def configure(directory: Path, terminal: Console, *, connections=False):
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / ".env"
    existing = dict(dotenv_values(path, interpolate=False)) if path.exists() else {}
    key = existing.get("STOCK_ENCRYPTION_KEY")
    data_dir = Path(existing.get("DATA_DIR") or "data")
    if not data_dir.is_absolute():
        data_dir = directory / data_dir
    if key:
        Fernet(key.encode())
    else:
        if (data_dir / "shop.sqlite").exists():
            raise ValueError(
                "База уже существует, но ключ отсутствует. Верните исходный STOCK_ENCRYPTION_KEY."
            )
        key = Fernet.generate_key().decode()
    terminal.write(
        "PlayerokHelper — настройка\nТокен вводится скрыто. Enter сохраняет существующее значение."
    )
    while True:
        token = terminal.ask("Токен Telegram-бота (BotFather): ", secret=True) or existing.get(
            "TELEGRAM_BOT_TOKEN", ""
        )
        if re.fullmatch(r"[1-9][0-9]*:[A-Za-z0-9_-]{20,}", token or ""):
            break
        terminal.write("Неверный формат токена. Нужен токен BotFather вида число:секрет.")
    current_admins = existing.get("ADMIN_IDS") or ""
    while True:
        prompt = "ID администратора"
        if current_admins:
            prompt += f" [сейчас {current_admins}]"
        text = terminal.ask(prompt + " (несколько — через запятую): ") or current_admins
        ids = [s.strip() for s in text.split(",")]
        if ids and all(re.fullmatch(r"[1-9][0-9]*", s) for s in ids):
            admins = ",".join(dict.fromkeys(ids))
            break
        terminal.write("Введите положительный числовой Telegram ID, например 123456789.")
    updates = {"TELEGRAM_BOT_TOKEN": token, "ADMIN_IDS": admins, "STOCK_ENCRYPTION_KEY": key}
    if connections:
        api_key = terminal.ask("API-ключ KOSell (Enter — сохранить или настроить позже): ", secret=True)
        if api_key:
            updates["KOSELL_API_KEY"] = api_key
        cookie = terminal.ask("Cookie Playerok (Enter — сохранить или настроить позже): ", secret=True)
        if cookie:
            user_agent = terminal.ask("User-Agent того же браузера: ")
            if not user_agent:
                raise ValueError("Для новой сессии Playerok требуется User-Agent. Настройки не изменены.")
            session_path = Path(existing.get("PLAYEROK_SESSION_FILE") or "secrets/playerok.json")
            if not session_path.is_absolute():
                session_path = directory / session_path
            atomic_write(
                session_path,
                json.dumps({"cookie": cookie, "user_agent": user_agent}, ensure_ascii=False) + "\n",
            )
    original = (
        path.read_text()
        if path.exists()
        else ((directory / ".env.example").read_text() if (directory / ".env.example").exists() else "")
    )
    atomic_write(path, env_text(original, updates))
    terminal.write(
        "Настройки сохранены в .env (доступ только владельцу). Ключ шифрования сохранён.\n"
        "Ручной запуск без systemd-службы: bash start.sh\n"
        "Подключить KOSell и Playerok: .venv/bin/playerokhelper setup --connections\n"
        "Подключения можно добавить кнопками в Telegram: профиль и баланс проверяются автоматически. Тест скрытия не требуется."
    )


def setup(directory: Path, *, connections=False):
    with console() as (input_stream, output_stream):
        configure(directory, Console(input_stream, output_stream), connections=connections)
