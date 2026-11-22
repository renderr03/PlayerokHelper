"""Install a systemd service without putting credentials in its unit or arguments."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values

SERVICE_NAME = "playerokhelper.service"
UNIT_PATH = Path("/etc/systemd/system") / SERVICE_NAME
DIRECTORY_MARKER = "# PlayerokHelper-Directory: "


class ServiceSetupError(Exception):
    """User-facing messages never include subprocess output or credential values."""


def unit_quote(value: str, *, executable=False) -> str:
    if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ServiceSetupError("Путь проекта содержит неподдерживаемые управляющие символы.")
    # WorkingDirectory takes a single raw path; quotes would become part of it.
    if not executable:
        return value.replace("%", "%%")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    escaped = escaped.replace("$", "$$")
    return '"' + escaped + '"'


def render_unit(directory: Path, uid: int, gid: int) -> str:
    directory = directory.resolve()
    executable = directory / ".venv/bin/playerokhelper"
    return f"""# Managed by PlayerokHelper
{DIRECTORY_MARKER}{json.dumps(str(directory), ensure_ascii=True)}
[Unit]
Description=PlayerokHelper Telegram bot
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User={uid}
Group={gid}
WorkingDirectory={unit_quote(str(directory))}
ExecStart={unit_quote(str(executable), executable=True)} run
Restart=always
RestartSec=10
TimeoutStopSec=45
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1

[Install]
WantedBy=multi-user.target
"""


def check_platform():
    if sys.platform != "linux" or not shutil.which("systemctl") or not Path("/run/systemd/system").is_dir():
        raise ServiceSetupError(
            "Автозапуск требует Linux с работающим systemd. "
            "Для установки без службы задайте SHOPBOT_AUTOSTART=0; запуск вручную: bash start.sh."
        )


def check_existing_unit(directory: Path):
    if not UNIT_PATH.exists():
        return
    text = UNIT_PATH.read_text()
    marker = next(
        (line[len(DIRECTORY_MARKER) :] for line in text.splitlines() if line.startswith(DIRECTORY_MARKER)),
        None,
    )
    if "# Managed by PlayerokHelper" not in text or marker != json.dumps(str(directory), ensure_ascii=True):
        raise ServiceSetupError(
            "Служба playerokhelper уже принадлежит другой установке или настроена вручную. "
            "Используйте исходный каталог проекта либо настройте службу самостоятельно."
        )


def install_service(directory: Path, *, runner=None, output=print):
    runner = runner or subprocess.run
    check_platform()
    directory = directory.resolve()
    if not (directory / ".venv/bin/playerokhelper").is_file():
        raise ServiceSetupError("Сначала установите PlayerokHelper в виртуальное окружение.")
    settings = dotenv_values(directory / ".env", interpolate=False)
    if not all(settings.get(key) for key in ("TELEGRAM_BOT_TOKEN", "ADMIN_IDS", "STOCK_ENCRYPTION_KEY")):
        raise ServiceSetupError(
            "Сначала выполните playerokhelper setup: нужны токен, ID администратора и ключ."
        )
    check_existing_unit(directory)
    privilege = []
    if os.geteuid() != 0:
        if not shutil.which("sudo"):
            raise ServiceSetupError("Для создания системной службы нужен sudo или запуск установки от root.")
        privilege = ["sudo"]
        output("Для установки службы sudo может запросить пароль вашего Linux-пользователя.")

    def run(args):
        try:
            return runner(privilege + args, check=True, capture_output=True, text=True)
        except (subprocess.CalledProcessError, OSError):
            raise ServiceSetupError(
                "Не удалось выполнить установку/запуск службы. Проверьте права sudo и "
                "systemctl status playerokhelper; журнал: journalctl -u playerokhelper -n 50 --no-pager."
            ) from None

    unit = render_unit(directory, os.geteuid(), os.getegid())
    with tempfile.TemporaryDirectory(prefix="PlayerokHelper-systemd-") as temporary:
        source = Path(temporary) / SERVICE_NAME
        source.write_text(unit)
        source.chmod(0o600)
        run(["install", "-m", "0644", str(source), str(UNIT_PATH)])
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", SERVICE_NAME])
    run(["systemctl", "reset-failed", SERVICE_NAME])
    run(["systemctl", "restart", SERVICE_NAME])
    run(["systemctl", "is-active", "--quiet", SERVICE_NAME])
    output(
        "PlayerokHelper запущен. Автозапуск после перезагрузки и перезапуск при сбое включены.\n"
        "Статус: systemctl status playerokhelper\n"
        "Журнал: journalctl -u playerokhelper -f"
    )
