#!/usr/bin/env bash
set -euo pipefail
umask 077

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
        git -C "$SHOPBOT_TARGET" pull --ff-only origin main
    else
        git clone --depth 1 --branch main "$SHOPBOT_REPOSITORY" "$SHOPBOT_TARGET"
    fi
fi

SHOPBOT_PYTHON="${SHOPBOT_PYTHON:-python3}"
command -v "$SHOPBOT_PYTHON" >/dev/null 2>&1 || {
    echo "Установите Python 3.11 или новее и модуль venv." >&2
    exit 1
}
"$SHOPBOT_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else "Нужен Python 3.11+. Выберите его через SHOPBOT_PYTHON.")'

cd -- "$SHOPBOT_TARGET"
if [[ ! -x .venv/bin/python ]]; then
    "$SHOPBOT_PYTHON" -m venv .venv || {
        echo "Не удалось создать окружение. Для Ubuntu 24.04 / Debian 12 установите python3-venv." >&2
        exit 1
    }
fi
.venv/bin/python -m pip install --disable-pip-version-check -c constraints.txt .
.venv/bin/playerokhelper setup --directory "$SHOPBOT_TARGET"
echo "PlayerokHelper установлен в: $SHOPBOT_TARGET"
echo "Запуск: bash \"$SHOPBOT_TARGET/start.sh\""
