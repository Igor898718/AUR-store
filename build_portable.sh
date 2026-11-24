#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

BUILD_VENV="${BUILD_VENV:-$PROJECT_DIR/.build-venv}"
DIST_DIR="$PROJECT_DIR/dist"
WORK_DIR="$PROJECT_DIR/build/portable"
ARCH="$(uname -m)"
ASSET="aur-store-linux-${ARCH}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Ошибка: не найден python3." >&2
    exit 1
fi

if [[ ! -x "$BUILD_VENV/bin/python" ]]; then
    rm -rf "$BUILD_VENV"
    python3 -m venv "$BUILD_VENV"
fi

"$BUILD_VENV/bin/python" -m pip install --upgrade pip
"$BUILD_VENV/bin/python" -m pip install -r requirements-build.txt

rm -rf "$DIST_DIR" "$WORK_DIR"

"$BUILD_VENV/bin/python" -m PyInstaller \
    --noconfirm \
    --clean \
    --onefile \
    --name aur-store \
    --distpath "$DIST_DIR" \
    --workpath "$WORK_DIR" \
    --specpath "$WORK_DIR" \
    --add-data "$PROJECT_DIR/images/archicon.png:images" \
    --hidden-import aur_helper \
    "$PROJECT_DIR/main.py"

cp "$DIST_DIR/aur-store" "$DIST_DIR/$ASSET"
chmod 755 "$DIST_DIR/$ASSET"
rm -f "$DIST_DIR/aur-store"

(
    cd "$DIST_DIR"
    tar -czf "$ASSET.tar.gz" "$ASSET"
    sha256sum "$ASSET" > "$ASSET.sha256"
    sha256sum "$ASSET.tar.gz" > "$ASSET.tar.gz.sha256"
)

VERSION="$(git -C "$PROJECT_DIR" describe --tags --always --dirty 2>/dev/null || echo dev)"

echo
echo "Сборка готова (${VERSION}):"
echo "  $DIST_DIR/$ASSET"
echo "  $DIST_DIR/$ASSET.tar.gz"
echo "  $DIST_DIR/$ASSET.sha256"
echo "  $DIST_DIR/$ASSET.tar.gz.sha256"
