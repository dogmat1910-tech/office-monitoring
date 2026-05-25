# Деинсталляция office-monitoring agent.
# Запуск (от админа):
#   irm https://raw.githubusercontent.com/dogmat1910-tech/office-monitoring/main/agent/uninstall.ps1 -UseBasicParsing | iex

$ErrorActionPreference = "SilentlyContinue"

$InstallDir = "C:\Program Files\office-monitoring"
$DataDir    = "C:\ProgramData\office-monitoring"
$TaskName   = "OfficeMonitoringAgent"

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "[x] Запусти PowerShell от имени администратора" -ForegroundColor Red
    exit 1
}

Write-Host "[*] Останавливаю задачу $TaskName..."
Stop-ScheduledTask -TaskName $TaskName

Write-Host "[*] Убиваю процессы python.exe из $InstallDir..."
Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$InstallDir*" } | Stop-Process -Force

Write-Host "[*] Удаляю задачу..."
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false

Write-Host "[*] Возвращаю ACL и удаляю папку $InstallDir..."
& icacls.exe $InstallDir /reset /T /Q 2>$null | Out-Null
Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue

Write-Host "[*] Удаляю папку данных $DataDir..."
Remove-Item -Recurse -Force $DataDir -ErrorAction SilentlyContinue

Write-Host "[+] Деинсталлировано" -ForegroundColor Green
