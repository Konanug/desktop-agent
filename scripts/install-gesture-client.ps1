<#
.SYNOPSIS
  Install the Hermes gesture client as a Windows Scheduled Task that survives
  closing PowerShell, and start it.

.DESCRIPTION
  WHY THIS IS A SCRIPT AND NOT A BLOCK TO PASTE.
  It was a block to paste, twice, and both times a line went missing. The first
  time it was Start-ScheduledTask, so the task existed and had never run --
  which looks exactly like "the task died", and was diagnosed as that. This
  script exists so that step cannot be dropped.

  THE OTHER THING THAT BIT.
  A Scheduled Task with no working directory runs from C:\Windows\System32, so
  a relative --config resolved to a file that was not there and the client
  exited within milliseconds, before it had a console or a log to say why.
  -WorkingDirectory below is that fix; hermes_gesture.py also now looks next to
  itself, so either half alone is enough.

.PARAMETER Dir
  Where hermes_gesture.py and gestures.json live. Default: this script's folder.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install-gesture-client.ps1
#>

[CmdletBinding()]
param(
    [string]$Dir  = "",
    [string]$Task = "HermesGesture"
)

$ErrorActionPreference = "Stop"

# NOT `param($Dir = $PSScriptRoot)`. That default came back EMPTY under
# `powershell -File .\install-gesture-client.ps1`, and the first thing it
# reached was Resolve-Path, which fails with "Cannot bind argument to parameter
# 'Path' because it is an empty string" -- an error that says nothing about
# which variable was empty or why.
#
# Three sources, most specific first, because each is empty in a case the next
# one covers: -Dir when given, the script's own location when it is known, and
# the working directory as the last resort.
if (-not $Dir) { $Dir = $PSScriptRoot }
if (-not $Dir -and $MyInvocation.MyCommand.Path) {
    $Dir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if (-not $Dir) { $Dir = (Get-Location).Path }

$resolved = Resolve-Path -LiteralPath $Dir -ErrorAction SilentlyContinue
if (-not $resolved) {
    Write-Error "cannot resolve -Dir '$Dir'. Pass it explicitly: .\install-gesture-client.ps1 -Dir C:\Users\$env:USERNAME\hermes-gesture"
    exit 1
}
$Dir    = $resolved.Path
$clientPy = Join-Path $Dir "hermes_gesture.py"
$config = Join-Path $Dir "gestures.json"

Write-Host "folder : $Dir"

$missing = @($clientPy, $config) | Where-Object { -not (Test-Path -LiteralPath $_) }
if ($missing) {
    Write-Host "`nmissing:" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "  $_" }
    Write-Host @"

Copy both into $Dir first:
  scp alanmyin@<pi>:projects/hermes-pi/clients/windows/hermes_gesture.py .
  scp alanmyin@<pi>:gestures-ready.json .\gestures.json
"@
    exit 1
}

# pythonw.exe, not python.exe: no console window for a thing that runs all day.
# The client detects the missing console and redirects its output to a log --
# without that, every message it produces would go nowhere (CPython's print()
# silently does nothing when sys.stdout is None).
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue |
            Select-Object -First 1).Source
if (-not $pythonw) {
    $py = (Get-Command python.exe -ErrorAction SilentlyContinue |
           Select-Object -First 1).Source
    if (-not $py) {
        Write-Error "no python.exe on PATH -- install Python first"
        exit 1
    }
    $pythonw = Join-Path (Split-Path -Parent $py) "pythonw.exe"
    if (-not (Test-Path -LiteralPath $pythonw)) {
        Write-Error "no pythonw.exe next to $py"
        exit 1
    }
}
# python.exe alongside it, for diagnostics: --fire and a manual run both
# exist to be READ, and pythonw has nowhere to print.
$pyexe = Join-Path (Split-Path -Parent $pythonw) "python.exe"

Write-Host "python : $pythonw"
Write-Host "script : $clientPy"
Write-Host "config : $config"

$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument ("`"$clientPy`" --config `"$config`"") `
    -WorkingDirectory $Dir

$trigger = New-ScheduledTaskTrigger -AtLogOn

# A delay at logon, because the network stack is usually not up yet. Set on the
# object rather than by parameter (-RandomDelay is a different thing), and
# WRAPPED: it is a nicety, and an unsupported property must not be the reason
# the whole install fails.
try { $trigger.Delay = "PT20S" } catch {
    Write-Host "  (no logon delay set: $($_.Exception.Message))"
}

# Interactive: SendInput needs a desktop. A task set to "run whether the user is
# logged on or not" runs in session 0, which has no desktop, and every keypress
# fails -- silently, because the process is alive and connected the whole time.
$user = if ($env:USERDOMAIN) { "$env:USERDOMAIN\$env:USERNAME" } else { $env:USERNAME }
$principal = New-ScheduledTaskPrincipal -UserId $user `
    -LogonType Interactive -RunLevel Limited

# ExecutionTimeLimit 0 = no limit. The default stops the task after three days,
# which for a listener means it silently dies on day three.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

# Restart-on-failure, again wrapped: worth having, not worth failing over.
try {
    $settings.RestartInterval = "PT1M"
    $settings.RestartCount = 99
} catch {
    Write-Host "  (no restart-on-failure set: $($_.Exception.Message))"
}

# Replace any previous definition rather than editing it: an install that
# half-updates an existing task is how you get two clients running at once,
# which shows up on the Pi as viewers=2 and doubles every keypress.
if (Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask  -TaskName $Task -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $Task -Confirm:$false
}
# ... and any client still running from before, for the same reason.
Get-Process pythonw -ErrorAction SilentlyContinue | Stop-Process -Force `
    -ErrorAction SilentlyContinue

Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings | Out-Null

Write-Host "`nregistered '$Task'. starting it..."
Start-ScheduledTask -TaskName $Task
# Long enough for a client that is going to fail to have failed. The failures
# this catches -- bad config path, bad token -- happen at startup, not later.
Start-Sleep -Seconds 5

# REPORT WHAT IS TRUE, NOT WHAT WAS ASKED FOR.
$procs = @(Get-Process pythonw -ErrorAction SilentlyContinue)
$info  = Get-ScheduledTaskInfo -TaskName $Task
$log   = Join-Path $env:LOCALAPPDATA "hermes-gesture.log"

Write-Host ""
if ($procs.Count -eq 1) {
    Write-Host "RUNNING  one pythonw (pid $($procs[0].Id))" -ForegroundColor Green
} elseif ($procs.Count -eq 0) {
    Write-Host "NOT RUNNING  no pythonw process." -ForegroundColor Red
    Write-Host "  LastTaskResult = $($info.LastTaskResult)  (0 = exited cleanly, 2 = bad config or token)"
    # SHOW THE REASON, do not just name the file. The whole point of the log is
    # that this program has no other way to speak under pythonw, and telling
    # someone where to look is one step short of telling them what happened.
    if (Test-Path -LiteralPath $log) {
        Write-Host "`n  last lines of $log :" -ForegroundColor Red
        Get-Content -LiteralPath $log -Tail 15 |
            ForEach-Object { Write-Host "    $_" }
    } else {
        Write-Host "  no log at $log either -- it exited before it could open one."
        Write-Host "  run it in this window to see why:"
        Write-Host "    & `"$pyexe`" `"$clientPy`" --config `"$config`""
    }
} else {
    Write-Host "$($procs.Count) pythonw processes -- more than one client." -ForegroundColor Yellow
    Write-Host "  Each will act on every gesture, so keys get pressed twice."
    Write-Host "  Close the extras, or check for a leftover Startup shortcut:"
    Write-Host "    shell:startup"
}
Write-Host "`nwatch it:  Get-Content `"$log`" -Tail 30 -Wait"
Write-Host "test one:  & `"$pyexe`" `"$clientPy`" --config `"$config`" --fire `"HERMES SPOTIFY`""
