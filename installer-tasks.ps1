# Installs the two scheduled tasks of the GPU lock/unlock status icon.
#
#   NvUnlock-Tray  : at logon, user rights. Runs the icon under pythonw
#                    (no window). Loads no driver.
#   NvUnlock-Probe : at logon, "run with highest privileges". Runs ssdiag,
#                    which installs then removes its driver on every probe
#                    (EventID 7045 in the System log).
#
# Run as administrator:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\installer-tasks.ps1
# Remove:
#   powershell -NoProfile -Command "Unregister-ScheduledTask NvUnlock-Tray,NvUnlock-Probe -Confirm:$false"

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# no hardcoded path: the interpreter is resolved from PATH
$pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
$py  = (Get-Command python.exe   -ErrorAction SilentlyContinue).Source
if (-not $pyw) { throw "pythonw.exe not found in PATH. Install Python (check 'Add to PATH')." }
if (-not $py ) { throw "python.exe not found in PATH." }
Write-Host "pythonw : $pyw"
Write-Host "python  : $py"

foreach ($m in 'pystray','PIL') {
  if (-not (python -c "import $m" 2>$null)) {
    Write-Host "missing Python module: $m -> py -m pip install pystray pillow" -ForegroundColor Yellow
  }
}

$tray  = New-ScheduledTaskAction -Execute $pyw -Argument "`"$here\tray.py`""         -WorkingDirectory $here
$probe = New-ScheduledTaskAction -Execute $py  -Argument "`"$here\tray.py`" --probe-once" -WorkingDirectory $here

$trigger = New-ScheduledTaskTrigger -AtLogOn
$setTray  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
$setProbe = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -StartWhenAvailable -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName 'NvUnlock-Tray'  -Action $tray  -Trigger $trigger -Settings $setTray `
    -Description "GPU lock/unlock status icon (nv-uefi-unlocker)" -Force | Out-Null
Register-ScheduledTask -TaskName 'NvUnlock-Probe' -Action $probe -Trigger $trigger -Settings $setProbe `
    -Description "ssdiag probe at logon (installs/removes its driver on every probe)" -Force | Out-Null

Set-ScheduledTask -TaskName 'NvUnlock-Probe' `
    -Principal (New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType S4U -RunLevel Highest) | Out-Null

Write-Host ''
Write-Host 'Tasks installed:' -ForegroundColor Green
Get-ScheduledTask -TaskName 'NvUnlock-Tray','NvUnlock-Probe' |
    Select-Object TaskName, State, @{n='Level';e={ $_.Principal.RunLevel }} | Format-Table -AutoSize

Write-Host 'Starting the icon now...' -ForegroundColor Cyan
Start-ScheduledTask -TaskName 'NvUnlock-Tray'
Write-Host 'OK. Icon in the taskbar (grey until the probe has run).'
Write-Host 'Manual probe: powershell -Command "Start-ScheduledTask -TaskName NvUnlock-Probe"'
