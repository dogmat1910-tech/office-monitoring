# office-monitoring agent installer for Windows (EXE-based).
# Запуск (от админа):
#   irm https://office.lkdzrkk.pro/install.ps1 -UseBasicParsing | iex

$ErrorActionPreference = "Stop"

$ServerUrl   = if ($env:OM_SERVER_URL) { $env:OM_SERVER_URL } else { "https://office.lkdzrkk.pro" }
$InstallDir  = "C:\Program Files\office-monitoring"
$DataDir     = "C:\ProgramData\office-monitoring"
$TaskAgent   = "OfficeMonitoring"
$TaskWatch   = "OfficeMonitoringWatchdog"
# Качаем .exe с того же домена, что и сервер — в корп-сетках github.com часто заблочен.
# Сервер раздаёт /agent.exe и /watchdog.exe из /opt/office-monitoring/public через Caddy.
$ReleaseBase = $ServerUrl
$AgentExe    = "office-monitoring-agent.exe"
$WatchExe    = "office-monitoring-watchdog.exe"
$AgentUrl    = "$ReleaseBase/agent.exe"
$WatchUrl    = "$ReleaseBase/watchdog.exe"

function Info($m)  { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m)    { Write-Host "[+] $m" -ForegroundColor Green }
function Warn($m)  { Write-Host "[!] $m" -ForegroundColor Yellow }
function Fail($m)  { Write-Host "[x] $m" -ForegroundColor Red; exit 1 }

# Админ-права обязательны
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Fail "Запусти PowerShell от имени администратора"
}

Info "office-monitoring agent installer (EXE-based)"
Info "Сервер: $ServerUrl"
Info "Папка установки: $InstallDir"

# --- Папки ---
Info "Создаю $InstallDir, $DataDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

# --- Останавливаем старые задачи и процессы (для апгрейда) ---
foreach ($t in @($TaskAgent, $TaskWatch)) {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
        Info "Останавливаю старую задачу $t"
        Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    }
}
Get-Process -Name "office-monitoring-agent","office-monitoring-watchdog" -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

# --- Скачиваем .exe ---
Info "Скачиваю $AgentExe с $AgentUrl..."
$agentPath = Join-Path $InstallDir $AgentExe
Invoke-WebRequest -Uri $AgentUrl -OutFile $agentPath -UseBasicParsing
Unblock-File -Path $agentPath
$agentSize = [math]::Round((Get-Item $agentPath).Length / 1MB, 1)
Ok "  $AgentExe ($agentSize MB)"

Info "Скачиваю $WatchExe с $WatchUrl..."
$watchPath = Join-Path $InstallDir $WatchExe
Invoke-WebRequest -Uri $WatchUrl -OutFile $watchPath -UseBasicParsing
Unblock-File -Path $watchPath
$watchSize = [math]::Round((Get-Item $watchPath).Length / 1MB, 1)
Ok "  $WatchExe ($watchSize MB)"

# --- Wrapper-скрипты (для env vars + start без окна) ---
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
start "" "$agentPath"
"@ | Set-Content -Path $runAgentBat -Encoding ASCII

$runWatchBat = Join-Path $InstallDir "run-watchdog.cmd"
@"
@echo off
setlocal
set OM_INSTALL_DIR=$InstallDir
set OM_DATA_DIR=$DataDir
cd /d "$InstallDir"
start "" "$watchPath"
"@ | Set-Content -Path $runWatchBat -Encoding ASCII

# --- Scheduled Task: основной агент ---
Info "Регистрирую Scheduled Task '$TaskAgent'..."
Unregister-ScheduledTask -TaskName $TaskAgent -Confirm:$false -ErrorAction SilentlyContinue

# BUILTIN\Users локализуется на ru-RU как BUILTIN\Пользователи —
# резолвим через SID, который одинаков на любой локали.
$usersGroup = (New-Object System.Security.Principal.SecurityIdentifier("S-1-5-32-545")).Translate([System.Security.Principal.NTAccount]).Value

$action    = New-ScheduledTaskAction -Execute $runAgentBat
$trigger   = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -GroupId $usersGroup -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -ExecutionTimeLimit (New-TimeSpan -Days 0) `
                -RestartCount 9999 -RestartInterval (New-TimeSpan -Minutes 1) `
                -StartWhenAvailable -Hidden
Register-ScheduledTask -TaskName $TaskAgent -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Ok "Scheduled Task '$TaskAgent' создан"

# --- Scheduled Task: watchdog (At-LogOn + каждые 15 мин) ---
Info "Регистрирую Scheduled Task '$TaskWatch'..."
Unregister-ScheduledTask -TaskName $TaskWatch -Confirm:$false -ErrorAction SilentlyContinue

$actionW    = New-ScheduledTaskAction -Execute $runWatchBat
$triggerW1  = New-ScheduledTaskTrigger -AtLogOn
$triggerW2  = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(15) `
                -RepetitionInterval (New-TimeSpan -Minutes 15) `
                -RepetitionDuration (New-TimeSpan -Days 365)
Register-ScheduledTask -TaskName $TaskWatch -Action $actionW -Trigger @($triggerW1, $triggerW2) `
    -Principal $principal -Settings $settings -Force | Out-Null
Ok "Scheduled Task '$TaskWatch' создан"

# --- ACL: только админ может удалить/изменить ---
Info "Защищаю папку через ACL..."
& icacls.exe $InstallDir /inheritance:r `
    /grant:r "Administrators:(OI)(CI)F" `
    /grant:r "SYSTEM:(OI)(CI)F" `
    /grant:r "Users:(OI)(CI)RX" /T | Out-Null
& icacls.exe $InstallDir /deny "Users:(DE,DC,WDAC,WO)" /T 2>$null | Out-Null
Ok "ACL обновлён"

# DataDir — лог должен быть на запись от пользователя, но без удаления файлов
& icacls.exe $DataDir /inheritance:r `
    /grant:r "Administrators:(OI)(CI)F" `
    /grant:r "SYSTEM:(OI)(CI)F" `
    /grant:r "Users:(OI)(CI)RX(WD,AD)" /T | Out-Null
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
Write-Host "  Get-Content '$DataDir\agent.log' -Tail 20 -Wait -Encoding UTF8"
Write-Host ""
Write-Host "Деинсталляция:"
Write-Host "  irm https://office.lkdzrkk.pro/uninstall.ps1 -UseBasicParsing | iex"
