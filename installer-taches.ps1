# Installe les deux taches planifiees de l'icone de statut GPU.
#
#   NvUnlock-Tray  : logon, droits utilisateur. Lance l'icone sous pythonw
#                    (aucune fenetre). Ne charge aucun pilote.
#   NvUnlock-Probe : logon, "run with highest privileges". Lance ssdiag, qui
#                    installe puis supprime son pilote a chaque sonde
#                    (EventID 7045 dans le journal Systeme).
#
# Lancer en administrateur:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\installer-taches.ps1
# Retirer:
#   powershell -NoProfile -Command "Unregister-ScheduledTask NvUnlock-Tray,NvUnlock-Probe -Confirm:$false"

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# aucun chemin cods en dur: on resout l'interpreteur depuis le PATH
$pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
$py  = (Get-Command python.exe   -ErrorAction SilentlyContinue).Source
if (-not $pyw) { throw "pythonw.exe introuvable dans le PATH. Installez Python (cochez 'Add to PATH')." }
if (-not $py ) { throw "python.exe introuvable dans le PATH." }
Write-Host "pythonw : $pyw"
Write-Host "python  : $py"

foreach ($m in 'pystray','PIL') {
  if (-not (python -c "import $m" 2>$null)) {
    Write-Host "module Python manquant: $m -> py -m pip install pystray pillow" -ForegroundColor Yellow
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
    -Description "Icone de statut lock/unlock GPU (nv-uefi-unlocker)" -Force | Out-Null
Register-ScheduledTask -TaskName 'NvUnlock-Probe' -Action $probe -Trigger $trigger -Settings $setProbe `
    -Description "Sonde ssdiag au logon (installe/supprime son pilote a chaque sonde)" -Force | Out-Null

Set-ScheduledTask -TaskName 'NvUnlock-Probe' `
    -Principal (New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType S4U -RunLevel Highest) | Out-Null

Write-Host ''
Write-Host 'Taches installees:' -ForegroundColor Green
Get-ScheduledTask -TaskName 'NvUnlock-Tray','NvUnlock-Probe' |
    Select-Object TaskName, State, @{n='Niveau';e={ $_.Principal.RunLevel }} | Format-Table -AutoSize

Write-Host "ssdiag detecte: $(python -c "import sys;sys.path.insert(0,'$here');import etat;print(etat.find_ssdiag())")"
Write-Host 'Demarrage immediat de l''icone...' -ForegroundColor Cyan
Start-ScheduledTask -TaskName 'NvUnlock-Tray'
Write-Host 'OK. Icone dans la barre des taches (grise tant que la sonde n''a pas tourne).'
Write-Host 'Sonde manuelle: powershell -Command "Start-ScheduledTask -TaskName NvUnlock-Probe"'
