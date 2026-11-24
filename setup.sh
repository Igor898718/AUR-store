#!/usr/bin/env bash
# Автоматическая настройка виртуального окружения AUR Store.
#
# Скрипт создаёт (или пересоздаёт несовместимое) окружение, обновляет pip
# и устанавливает зависимости из requirements.txt.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="${VENV_DIR:-.venv}"
PYTHON="${PYTHON:-python3}"
PYTHON_EXPLICIT=0
FORCE_RECREATE=0

usage() {
    cat <<'EOF'
Использование: ./setup.sh [параметры]

Параметры:
  --venv PATH       каталог виртуального окружения (по умолчанию .venv)
  --python PATH     интерпретатор Python (по умолчанию python3)
  --recreate        пересоздать окружение, даже если оно рабочее
  -h, --help        показать эту справку
EOF
}

while (($#)); do
    case "$1" in
        --venv)
            if (($# < 2)); then
                echo "Для --venv нужен путь." >&2
                exit 2
            fi
            VENV_DIR="$2"
            shift 2
            ;;
        --venv=*)
            VENV_DIR="${1#*=}"
            shift
            ;;
        --python)
            if (($# < 2)); then
                echo "Для --python нужен путь." >&2
                exit 2
            fi
            PYTHON="$2"
            PYTHON_EXPLICIT=1
            shift 2
            ;;
        --python=*)
            PYTHON="${1#*=}"
            PYTHON_EXPLICIT=1
            shift
            ;;
        --recreate)
            FORCE_RECREATE=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Неизвестный параметр: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ -z "$VENV_DIR" ]]; then
    echo "Путь к виртуальному окружению не может быть пустым." >&2
    exit 1
fi

VENV_REAL="$(realpath -m -- "$VENV_DIR")"
if [[ "$VENV_REAL" == "/" || "$VENV_REAL" == "$SCRIPT_DIR" ]] || \
   [[ "$SCRIPT_DIR" == "$VENV_REAL"/* ]]; then
    echo "Недопустимый путь для виртуального окружения: $VENV_DIR" >&2
    exit 1
fi

PYTHON_PATH="$(command -v "$PYTHON" 2>/dev/null || true)"
if [[ -n "$PYTHON_PATH" ]]; then
    # Нельзя использовать интерпретатор из окружения, которое мы собираемся
    # удалить или пересоздать. Сравниваем путь как он введён: bin/python3 —
    # это симлинк на системный Python, и realpath увёл бы нас из venv.
    PYTHON_INSIDE_VENV=0
    case "$PYTHON_PATH" in
        "$VENV_DIR"/*|"$VENV_REAL"/*) PYTHON_INSIDE_VENV=1 ;;
    esac
    if [[ $PYTHON_EXPLICIT -eq 0 && -n "${VIRTUAL_ENV:-}" ]]; then
        case "$PYTHON_PATH" in
            "$VIRTUAL_ENV"/*) PYTHON_INSIDE_VENV=1 ;;
        esac
    fi
    if [[ $PYTHON_INSIDE_VENV -eq 1 ]]; then
        echo "Python «$PYTHON» находится внутри виртуального окружения."
        echo "Использую системный Python, чтобы не удалить работающий интерпретатор."
        SYSTEM_PYTHON=""
        for candidate in /usr/bin/python3 /usr/local/bin/python3; do
            if [[ -x "$candidate" ]]; then
                SYSTEM_PYTHON="$candidate"
                break
            fi
        done
        if [[ -z "$SYSTEM_PYTHON" ]]; then
            echo "Не удалось найти системный Python. Выполните deactivate и повторите." >&2
            exit 1
        fi
        PYTHON="$SYSTEM_PYTHON"
        PYTHON_PATH="$SYSTEM_PYTHON"
    fi
fi

if [[ -z "$PYTHON_PATH" ]] || ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "Не найден Python «$PYTHON»." >&2
    echo "На Arch Linux установите его командой:" >&2
    echo "  sudo pacman -S --needed python" >&2
    exit 1
fi

PYTHON_VERSION="$("$PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Нужен Python 3.10 или новее, найден $PYTHON_VERSION." >&2
    exit 1
fi

echo "Python: $PYTHON_VERSION ($(command -v "$PYTHON"))"

venv_has_python() {
    local venv="$1"
    [[ -x "$venv/bin/python" ]] && "$venv/bin/python" -c 'import sys' >/dev/null 2>&1
}

venv_is_usable() {
    local venv="$1"
    local venv_real

    [[ -f "$venv/pyvenv.cfg" ]] || return 1
    venv_has_python "$venv" || return 1
    "$venv/bin/python" -m pip --version >/dev/null 2>&1 || return 1

    # Окружение, созданное на другой машине, содержит в activate старый
    # абсолютный путь. Активация такого окружения подменяет PATH неправильно.
    # Не выполняем activate: достаточно проверить, что текущий путь есть в файле.
    if [[ -f "$venv/bin/activate" ]]; then
        venv_real="$(realpath -m -- "$venv")"
        if ! grep -Fq -- "$venv_real" "$venv/bin/activate"; then
            return 1
        fi
    fi

    return 0
}

CREATE_VENV=0
if [[ -e "$VENV_DIR" ]]; then
    if [[ ! -d "$VENV_DIR" ]]; then
        echo "Путь $VENV_DIR существует, но это не каталог." >&2
        exit 1
    fi
    if [[ ! -f "$VENV_DIR/pyvenv.cfg" ]]; then
        echo "Каталог $VENV_DIR существует, но не похож на виртуальное окружение." >&2
        echo "Удалите его вручную или укажите другой каталог через --venv." >&2
        exit 1
    fi

    if [[ $FORCE_RECREATE -eq 1 ]] || ! venv_is_usable "$VENV_DIR"; then
        echo "Окружение $VENV_DIR несовместимо с этой машиной — пересоздаю."
        rm -rf -- "$VENV_DIR"
        CREATE_VENV=1
    else
        echo "Использую существующее окружение: $VENV_DIR"
    fi
else
    CREATE_VENV=1
fi

if [[ $CREATE_VENV -eq 1 ]]; then
    echo "Создаю виртуальное окружение: $VENV_DIR"
    if ! "$PYTHON" -m venv --clear "$VENV_DIR"; then
        echo "Не удалось создать виртуальное окружение." >&2
        echo "На Arch Linux модуль venv входит в пакет python:" >&2
        echo "  sudo pacman -S --needed python" >&2
        exit 1
    fi
fi

if ! "$VENV_DIR/bin/python" -m pip --version >/dev/null 2>&1; then
    echo "В окружении нет pip — устанавливаю через ensurepip..."
    if ! "$VENV_DIR/bin/python" -m ensurepip --upgrade; then
        echo "Не удалось установить pip в $VENV_DIR." >&2
        exit 1
    fi
fi

echo "Обновляю pip, setuptools и wheel..."
if ! "$VENV_DIR/bin/python" -m pip install \
    --disable-pip-version-check --upgrade pip setuptools wheel; then
    echo "Не удалось обновить инструменты pip. Проверьте подключение к интернету." >&2
    exit 1
fi

if [[ ! -f "$SCRIPT_DIR/requirements.txt" ]]; then
    echo "Не найден файл requirements.txt рядом со скриптом." >&2
    exit 1
fi

echo "Устанавливаю зависимости из requirements.txt..."
if ! "$VENV_DIR/bin/python" -m pip install \
    --disable-pip-version-check --upgrade -r "$SCRIPT_DIR/requirements.txt"; then
    echo "Не удалось установить зависимости." >&2
    exit 1
fi

echo "Проверяю PyQt6..."
if "$VENV_DIR/bin/python" -c 'import PyQt6.QtWidgets' >/dev/null 2>&1; then
    echo "PyQt6 установлен и импортируется."
else
    echo "Предупреждение: PyQt6 не импортируется." >&2
    echo "Проверьте, что он установлен, и системные библиотеки Qt." >&2
    "$VENV_DIR/bin/python" -c 'import PyQt6.QtWidgets' || true
fi

ACTIVATE_PATH="$(realpath -m -- "$VENV_DIR/bin/activate")"

if [[ -n "${VIRTUAL_ENV:-}" ]] && \
   [[ "$(realpath -m -- "$VIRTUAL_ENV")" != "$VENV_REAL" ]]; then
    echo
    echo "Сейчас в этом shell активно другое окружение:"
    echo "  $VIRTUAL_ENV"
    echo "Выполните deactivate и активируйте $VENV_DIR заново либо откройте новый терминал."
fi

if command -v git >/dev/null 2>&1 && \
   git -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 && \
   [[ -n "$(git -C "$SCRIPT_DIR" ls-files -- "$VENV_DIR" 2>/dev/null | head -n 1)" ]]; then
    echo
    echo "Внимание: каталог $VENV_DIR отслеживается git."
    echo "После пересоздания git покажет сотни изменений. Уберите его из индекса:"
    echo "  git rm -r --cached $(printf '%q' "$VENV_DIR")"
fi

echo
echo "Готово. Виртуальное окружение: $VENV_REAL"
echo "Активация:"
echo "  source $(printf '%q' "$ACTIVATE_PATH")"
echo "Запуск:"
echo "  python main.py"
echo "Без активации:"
echo "  $(printf '%q' "$VENV_DIR/bin/python") main.py"
