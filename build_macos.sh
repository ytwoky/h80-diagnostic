#!/usr/bin/env bash
# Сборка GE H80-200 Diagnostic для macOS
# Использование: bash build_macos.sh

set -euo pipefail
cd "$(dirname "$0")"

echo "=== H80 Diagnostic — macOS build ==="

# Проверка зависимостей
python3 -c "import matplotlib, numpy, scipy, PIL, tkinter" 2>/dev/null || {
  echo "[!] Устанавливаю зависимости..."
  pip3 install matplotlib numpy scipy pillow --break-system-packages -q
}

which pyinstaller >/dev/null 2>&1 || {
  echo "[!] Устанавливаю PyInstaller..."
  pip3 install pyinstaller --break-system-packages -q
}

# Иконка
if [ ! -f mgtu_logo.icns ]; then
  echo "[*] Создаю mgtu_logo.icns..."
  mkdir -p /tmp/AppIcon.iconset
  for s in 16 32 64 128 256 512 1024; do
    sips -z $s $s mgtu_logo.png --out /tmp/AppIcon.iconset/icon_${s}x${s}.png >/dev/null
  done
  cp /tmp/AppIcon.iconset/icon_32x32.png   /tmp/AppIcon.iconset/icon_16x16@2x.png
  cp /tmp/AppIcon.iconset/icon_64x64.png   /tmp/AppIcon.iconset/icon_32x32@2x.png
  cp /tmp/AppIcon.iconset/icon_256x256.png /tmp/AppIcon.iconset/icon_128x128@2x.png
  cp /tmp/AppIcon.iconset/icon_512x512.png /tmp/AppIcon.iconset/icon_256x256@2x.png
  cp /tmp/AppIcon.iconset/icon_1024x1024.png /tmp/AppIcon.iconset/icon_512x512@2x.png
  iconutil -c icns /tmp/AppIcon.iconset -o mgtu_logo.icns
fi

echo "[*] Запускаю PyInstaller..."
pyinstaller h80_diagnostic.spec --clean --noconfirm

APP="dist/H80 Diagnostic.app"
echo ""
echo "=== Готово ==="
echo "Приложение: $(du -sh "$APP" | cut -f1)  $APP"
echo "Архитектура: $(file "$APP/Contents/MacOS/H80 Diagnostic" | grep -o 'arm64\|x86_64')"
echo ""
echo "Для запуска: open \"$APP\""
echo "Для zip-архива: cd dist && zip -r 'H80_Diagnostic_macOS.zip' 'H80 Diagnostic.app'"
