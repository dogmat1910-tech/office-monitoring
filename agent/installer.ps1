# office-monitoring agent installer for Windows (EXE-based).
# Запуск (от админа):
#   irm https://office.lkdzrkk.pro/install.ps1 -UseBasicParsing | iex

$ErrorActionPreference = "Stop"

$ServerUrl   = if ($env:OM_SERVER_URL) { $env:OM_SERVER_URL } else { "https://office.lkdzrkk.pro" }
$InstallCode = if ($env:OM_INSTALL_CODE) { $env:OM_INSTALL_CODE } else { "" }
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

# Install-код. Без него агент не сможет зарегистрироваться на сервере
# (если на сервере включён OM_REQUIRE_AGENT_AUTH). Берётся из env OM_INSTALL_CODE,
# иначе спрашиваем интерактивно.
if (-not $InstallCode) {
    $InstallCode = Read-Host "Install-код от сервера (узнать у админа; пусто = пропустить)"
}

Info "office-monitoring agent installer (EXE-based)"
Info "Сервер: $ServerUrl"
Info "Папка установки: $InstallDir"
if ($InstallCode) {
    Info "Install-код задан, агент зарегистрируется при первом старте"
} else {
    Warn "Install-код не задан — авторизация не сработает если на сервере включён enforce"
}

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

# --- Скачиваем .exe с проверкой SHA256 ---
# Сначала получаем хеши от сервера (тот же источник что и auto-update)
Info "Получаю версию и SHA256..."
try {
    $versionInfo = Invoke-RestMethod -Uri "$ReleaseBase/agent/version" -UseBasicParsing -TimeoutSec 10
    $sha256Agent = $versionInfo.sha256_agent
    $sha256Watch = $versionInfo.sha256_watchdog
    Ok "  Версия: $($versionInfo.version), SHA256 получены"
} catch {
    Warn "Не смог получить SHA256 от сервера — скачаю без проверки: $_"
    $sha256Agent = $null
    $sha256Watch = $null
}

Info "Скачиваю $AgentExe с $AgentUrl..."
$agentPath = Join-Path $InstallDir $AgentExe
Invoke-WebRequest -Uri $AgentUrl -OutFile $agentPath -UseBasicParsing
Unblock-File -Path $agentPath
$agentSize = [math]::Round((Get-Item $agentPath).Length / 1MB, 1)
if ($sha256Agent) {
    $actualHash = (Get-FileHash -Path $agentPath -Algorithm SHA256).Hash.ToLower()
    if ($actualHash -ne $sha256Agent) {
        Fail "SHA256 mismatch для $AgentExe! Ожидали: $sha256Agent, получили: $actualHash. Файл повреждён или подменён."
    }
    Ok "  $AgentExe ($agentSize MB) SHA256 OK"
} else {
    Ok "  $AgentExe ($agentSize MB)"
}

Info "Скачиваю $WatchExe с $WatchUrl..."
$watchPath = Join-Path $InstallDir $WatchExe
Invoke-WebRequest -Uri $WatchUrl -OutFile $watchPath -UseBasicParsing
Unblock-File -Path $watchPath
$watchSize = [math]::Round((Get-Item $watchPath).Length / 1MB, 1)
if ($sha256Watch) {
    $actualHash = (Get-FileHash -Path $watchPath -Algorithm SHA256).Hash.ToLower()
    if ($actualHash -ne $sha256Watch) {
        Fail "SHA256 mismatch для $WatchExe! Ожидали: $sha256Watch, получили: $actualHash."
    }
    Ok "  $WatchExe ($watchSize MB) SHA256 OK"
} else {
    Ok "  $WatchExe ($watchSize MB)"
}

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
set OM_INSTALL_CODE=$InstallCode
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

# ACL не трогаем — Program Files и так защищён от записи/удаления для
# обычных пользователей через унаследованные права (Users: только RX).
# Своя icacls /inheritance:r + /deny ломает доступ даже админу
# (особенно на не-en локали), а реальной защиты сверх дефолта не даёт.
# Если будет нужна доп. защита от удаления — добавим точечный /deny
# на конкретные .exe через явный SID-grant без сброса inheritance.

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
