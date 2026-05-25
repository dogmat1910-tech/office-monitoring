# office-monitoring agent installer for Windows.
# Запуск (от админа):
#   irm https://raw.githubusercontent.com/dogmat1910-tech/office-monitoring/main/agent/installer.ps1 -UseBasicParsing | iex

$ErrorActionPreference = "Stop"

$ServerUrl   = if ($env:OM_SERVER_URL) { $env:OM_SERVER_URL } else { "http://89.22.225.88" }
$InstallDir  = "C:\Program Files\office-monitoring"
$DataDir     = "C:\ProgramData\office-monitoring"
$TaskName    = "OfficeMonitoringAgent"
$RepoBase    = "https://raw.githubusercontent.com/dogmat1910-tech/office-monitoring/main/agent"
$Files       = @("agent.py", "active_window.py", "audio.py", "requirements.txt")

# Цвета в логе
function Info($m)  { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m)    { Write-Host "[+] $m" -ForegroundColor Green }
function Warn($m)  { Write-Host "[!] $m" -ForegroundColor Yellow }
function Fail($m)  { Write-Host "[x] $m" -ForegroundColor Red; exit 1 }

# Проверка прав администратора
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Fail "Запусти PowerShell от имени администратора (правый клик на PowerShell -> Run as Administrator)"
}

Info "office-monitoring agent installer"
Info "Сервер: $ServerUrl"
Info "Папка установки: $InstallDir"

# --- 1. Проверка Python ---
function Get-PythonExe {
    # Сначала пробуем py.exe (Python launcher), потом python.exe
    $candidates = @(
        "C:\Program Files\Python312\python.exe",
        "C:\Program Files\Python311\python.exe",
        "C:\Program Files\Python310\python.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        # отфильтруем заглушку из Microsoft Store
        if ($cmd.Source -notlike "*WindowsApps*") { return $cmd.Source }
    }
    return $null
}

$python = Get-PythonExe
if (-not $python) {
    Info "Python не найден, ставлю через winget (Python 3.12)..."
    & winget install --id Python.Python.3.12 -e --source winget --silent --accept-source-agreements --accept-package-agreements --scope machine
    if ($LASTEXITCODE -ne 0) { Fail "winget install Python завершился с ошибкой" }
    Start-Sleep -Seconds 3
    $python = Get-PythonExe
    if (-not $python) { Fail "Python всё равно не найден после winget. Поставь вручную с python.org и перезапусти." }
}
Ok "Python: $python"

# --- 2. Установка файлов ---
Info "Создаю $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

Info "Скачиваю файлы агента..."
foreach ($f in $Files) {
    $dest = Join-Path $InstallDir $f
    Invoke-WebRequest -Uri "$RepoBase/$f" -OutFile $dest -UseBasicParsing
    Ok "  $f"
}

# --- 3. venv + зависимости (обход SOCKS-прокси через --proxy "") ---
Info "Создаю venv (--without-pip чтобы не упереться в прокси на ensurepip)..."
$venvDir = Join-Path $InstallDir ".venv"
if (Test-Path $venvDir) {
    Remove-Item -Recurse -Force $venvDir
}
& $python -m venv $venvDir --without-pip
if ($LASTEXITCODE -ne 0) { Fail "python -m venv упал" }

$venvPython = Join-Path $venvDir "Scripts\python.exe"
if (-not (Test-Path $venvPython)) { Fail "$venvPython не создан" }

Info "Доустанавливаю pip через ensurepip..."
& $venvPython -m ensurepip --upgrade
if ($LASTEXITCODE -ne 0) { Fail "ensurepip упал" }

Info "Ставлю зависимости (с --proxy `"`" для обхода SOCKS)..."
& $venvPython -m pip install --proxy "" --upgrade pip
& $venvPython -m pip install --proxy "" -r (Join-Path $InstallDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { Fail "pip install зависимостей упал" }
Ok "Зависимости установлены"

# --- 4. Создаём Scheduled Task для автостарта при логине ---
Info "Регистрирую Scheduled Task '$TaskName' (запуск при логине любого пользователя)..."

# удаляем старую задачу если была
schtasks.exe /Delete /TN $TaskName /F 2>$null | Out-Null

# Используем PowerShell ScheduledTasks модуль (надёжнее чем schtasks XML)
$wrapper = Join-Path $InstallDir "run-agent.cmd"
@"
@echo off
setlocal
set OM_SERVER_URL=$ServerUrl
set OM_LOG_DIR=$DataDir
cd /d "$InstallDir"
"$venvPython" "$InstallDir\agent.py"
"@ | Set-Content -Path $wrapper -Encoding ASCII

$action    = New-ScheduledTaskAction -Execute $wrapper
$trigger   = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -GroupId "BUILTIN\Users" -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -ExecutionTimeLimit (New-TimeSpan -Days 0) `
                -RestartCount 9999 -RestartInterval (New-TimeSpan -Minutes 1) `
                -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Ok "Scheduled Task создана"

# --- 5. Защита: блокируем удаление файлов простым пользователем ---
Info "Защищаю папку от удаления (отзываем Modify у обычных Users)..."
& icacls.exe $InstallDir /inheritance:r /grant:r "Administrators:(OI)(CI)F" /grant:r "SYSTEM:(OI)(CI)F" /grant:r "Users:(OI)(CI)RX" /T | Out-Null
Ok "ACL обновлён"

# --- 6. Запуск задачи прямо сейчас ---
Info "Запускаю агент сейчас..."
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3

$task = Get-ScheduledTask -TaskName $TaskName
$info = $task | Get-ScheduledTaskInfo
Ok "Статус задачи: $($task.State), последний запуск: $($info.LastRunTime), результат: $($info.LastTaskResult)"

Write-Host ""
Ok "========================================"
Ok "Установка завершена"
Ok "========================================"
Write-Host ""
Write-Host "Лог агента: $DataDir\agent.log"
Write-Host "Сервер: $ServerUrl"
Write-Host "Дашборд: $ServerUrl"
Write-Host ""
Write-Host "Команды для админа:"
Write-Host "  Get-ScheduledTask -TaskName $TaskName    # статус"
Write-Host "  Stop-ScheduledTask -TaskName $TaskName   # остановить"
Write-Host "  Start-ScheduledTask -TaskName $TaskName  # запустить"
Write-Host "  Get-Content '$DataDir\agent.log' -Tail 20 -Wait    # смотреть лог"
Write-Host ""
Write-Host "Деинсталляция:"
Write-Host "  irm $RepoBase/uninstall.ps1 -UseBasicParsing | iex"
