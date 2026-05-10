@echo off
REM ============================================================
REM  Сборка GE H80-200 Diagnostic для Windows
REM  Запускать на Windows-машине с Python 3.12 или 3.13
REM  Использование: build_windows.bat
REM ============================================================

cd /d "%~dp0"
echo === H80 Diagnostic — Windows build ===

REM Проверка Python
python --version 2>nul || (
    echo [!] Python не найден. Установите Python 3.12+ с python.org
    pause & exit /b 1
)

REM Зависимости
echo [*] Проверяю зависимости...
pip install matplotlib numpy scipy pillow pyinstaller --quiet

REM Иконка — конвертируем PNG -> ICO через Python+Pillow
if not exist mgtu_logo.ico (
    echo [*] Создаю mgtu_logo.ico...
    python -c "from PIL import Image; img=Image.open('mgtu_logo.png').convert('RGBA'); img.save('mgtu_logo.ico',format='ICO',sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"
)

echo [*] Запускаю PyInstaller...
pyinstaller h80_diagnostic.spec --clean --noconfirm

echo.
echo === Готово ===
echo Папка с .exe: dist\H80 Diagnostic\
echo Запускать:    dist\H80 Diagnostic\H80 Diagnostic.exe
echo.
echo Для zip-архива используйте правую кнопку -> Отправить -> Сжатая папка
pause
