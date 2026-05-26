# Деинсталляция office-monitoring agent.
# Запуск (от админа):
#   irm https://office.lkdzrkk.pro/uninstall.ps1 -UseBasicParsing | iex

$ErrorActionPreference = "SilentlyContinue"

$InstallDir = "C:\Program Files\office-monitoring"
$DataDir    = "C:\ProgramData\office-monitoring"
$TaskAgent  = "OfficeMonitoring"
$TaskWatch  = "OfficeMonitoringWatchdog"
# legacy для предыдущей версии установщика:
$TaskLegacy = "OfficeMonitoringAgent"

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "[x] Запусти PowerShell от имени администратора" -ForegroundColor Red
    exit 1
}

foreach ($t in @($TaskAgent, $TaskWatch, $TaskLegacy)) {
    Write-Host "[*] Останавливаю задачу $t..."
    Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
}

Write-Host "[*] Убиваю процессы агента и watchdog..."
Get-Process -Name "office-monitoring-agent","office-monitoring-watchdog" -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
# legacy: предыдущая Python-версия инсталлера
Get-Process pythonw, python -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like "$InstallDir*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

foreach ($t in @($TaskAgent, $TaskWatch, $TaskLegacy)) {
    Write-Host "[*] Удаляю задачу $t..."
    Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue
}

Write-Host "[*] Возвращаю ACL и удаляю папку $InstallDir..."
& icacls.exe $InstallDir /reset /T /Q 2>$null | Out-Null
Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue

Write-Host "[*] Удаляю папку данных $DataDir..."
Remove-Item -Recurse -Force $DataDir -ErrorAction SilentlyContinue

Write-Host "[+] Деинсталлировано" -ForegroundColor Green
