#!/usr/bin/env bash
set -euo pipefail
umask 077
echo "PlayerokHelper — установка [1/6]: проверка Python и зависимостей"
SHOPBOT_PYTHON="${SHOPBOT_PYTHON:-python3}"
command -v "$SHOPBOT_PYTHON" >/dev/null 2>&1 || { echo "Установите Python 3.11+ и python3-venv." >&2; exit 1; }
"$SHOPBOT_PYTHON" -c 'import sys, venv; raise SystemExit(0 if sys.version_info >= (3, 11) else "Нужен Python 3.11+")'
for SHOPBOT_DEPENDENCY in git curl; do
    command -v "$SHOPBOT_DEPENDENCY" >/dev/null 2>&1 || {
        echo "Не найден $SHOPBOT_DEPENDENCY. Для Ubuntu/Debian: sudo apt-get install git curl python3 python3-venv" >&2
        exit 1
    }
done
shopbot_git() {
    if command -v timeout >/dev/null 2>&1; then
        timeout 180 git -c http.lowSpeedLimit=100 -c http.lowSpeedTime=30 "$@"
    else
        git -c http.lowSpeedLimit=100 -c http.lowSpeedTime=30 "$@"
    fi
}
echo "[2/6] Подготовка проекта; загрузка из GitHub может занять несколько минут"
if [[ "${SHOPBOT_AUTOSTART:-1}" != "0" && "${SHOPBOT_AUTOSTART:-1}" != "1" ]]; then
    echo "SHOPBOT_AUTOSTART должен быть 1 (по умолчанию) или 0." >&2
    exit 1
fi

SHOPBOT_REPOSITORY="https://github.com/renderr03/PlayerokHelper.git"
SHOPBOT_SCRIPT_PATH="${BASH_SOURCE[0]:-}"
SHOPBOT_SOURCE_DIR=""
if [[ -n "$SHOPBOT_SCRIPT_PATH" && -f "$SHOPBOT_SCRIPT_PATH" ]]; then
    SHOPBOT_SOURCE_DIR="$(cd -- "$(dirname -- "$SHOPBOT_SCRIPT_PATH")" && pwd)"
fi

if [[ -n "$SHOPBOT_SOURCE_DIR" && -f "$SHOPBOT_SOURCE_DIR/pyproject.toml" && -d "$SHOPBOT_SOURCE_DIR/src/shopbot" ]]; then
    SHOPBOT_TARGET="$SHOPBOT_SOURCE_DIR"
else
    SHOPBOT_TARGET="${SHOPBOT_INSTALL_DIR:-${HOME}/PlayerokHelper}"
    command -v git >/dev/null 2>&1 || {
        echo "Нужен git. Для Ubuntu 24.04 / Debian 12: sudo apt-get install git python3 python3-venv" >&2
        exit 1
    }
    if [[ -e "$SHOPBOT_TARGET" ]]; then
        if [[ -e "$SHOPBOT_TARGET/.updates/request.json" || -e "$SHOPBOT_TARGET/.updates/maintenance.json" ]]; then
            echo "Обновление из Telegram уже выполняется или требует восстановления. Проверьте службу обновлятора." >&2
            exit 1
        fi
        if [[ ! -d "$SHOPBOT_TARGET/.git" ]]; then
            echo "Папка $SHOPBOT_TARGET уже существует и не является клоном проекта. Выберите другую через SHOPBOT_INSTALL_DIR." >&2
            exit 1
        fi
        SHOPBOT_ORIGIN="$(git -C "$SHOPBOT_TARGET" remote get-url origin)"
        if [[ "$SHOPBOT_ORIGIN" != "$SHOPBOT_REPOSITORY" && "$SHOPBOT_ORIGIN" != "git@github.com:renderr03/PlayerokHelper.git" ]]; then
            echo "Папка принадлежит другому репозиторию. Установка остановлена." >&2
            exit 1
        fi
        if [[ "$(git -C "$SHOPBOT_TARGET" branch --show-current)" != "main" ]]; then
            echo "Перед обновлением переключите репозиторий на main." >&2
            exit 1
        fi
        git -C "$SHOPBOT_TARGET" diff --quiet && git -C "$SHOPBOT_TARGET" diff --cached --quiet || {
            echo "Есть изменённые файлы проекта. Сохраните их перед обновлением; настройки и данные не удаляются." >&2
            exit 1
        }
        shopbot_git -C "$SHOPBOT_TARGET" pull --ff-only origin main
    else
        shopbot_git clone --depth 1 --branch main "$SHOPBOT_REPOSITORY" "$SHOPBOT_TARGET"
    fi
fi

SHOPBOT_PYTHON="${SHOPBOT_PYTHON:-python3}"
command -v "$SHOPBOT_PYTHON" >/dev/null 2>&1 || {
    echo "Установите Python 3.11 или новее и модуль venv." >&2
    exit 1
}
"$SHOPBOT_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else "Нужен Python 3.11+. Выберите его через SHOPBOT_PYTHON.")'

cd -- "$SHOPBOT_TARGET"
"$SHOPBOT_PYTHON" -c 'import shutil; raise SystemExit(0 if shutil.disk_usage(".").free >= 512*1024*1024 else "Для установки нужно не менее 512 МиБ свободного места")'
if [[ -e .updates/request.json || -e .updates/maintenance.json ]]; then
    echo "Выполняется обновление из Telegram или требуется восстановление. Проверьте /version и службу обновлятора перед установкой." >&2
    exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
    echo "[3/6] Создание Python-окружения"
    "$SHOPBOT_PYTHON" -m venv .venv || {
        echo "Не удалось создать окружение. Для Ubuntu 24.04 / Debian 12 установите python3-venv." >&2
        exit 1
    }
fi
echo "[4/6] Установка пакетов"
.venv/bin/python -m pip install --disable-pip-version-check -c constraints.txt .
echo "[5/6] Настройка: вводите токен только после соответствующего приглашения"
.venv/bin/playerokhelper setup --directory "$SHOPBOT_TARGET"
.venv/bin/playerokhelper telegram-check
echo "PlayerokHelper установлен в: $SHOPBOT_TARGET"
if [[ "${SHOPBOT_AUTOSTART:-1}" == "1" ]]; then
    echo "[6/6] Запуск службы и обновлятора"
    .venv/bin/playerokhelper install-service --directory "$SHOPBOT_TARGET"
elif [[ "${SHOPBOT_AUTOSTART:-1}" == "0" ]]; then
    echo "Автозапуск отключён через SHOPBOT_AUTOSTART=0."
    echo "Запуск вручную: bash \"$SHOPBOT_TARGET/start.sh\""
else
    echo "SHOPBOT_AUTOSTART должен быть 1 (по умолчанию) или 0." >&2
    exit 1
fi
echo "Готово. Откройте бота в Telegram: /start, /setup. Статус версии и обновлений: /version."
