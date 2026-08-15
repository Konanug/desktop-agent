<#
.SYNOPSIS
  Install the Hermes gesture client so it survives closing PowerShell, and
  start it. Needs no administrator.

.DESCRIPTION
  WHY THIS IS A SCRIPT AND NOT A BLOCK TO PASTE.
  It was a block to paste, twice, and both times a line went missing. The first
  time it was Start-ScheduledTask, so the task existed and had never run --
  which looks exactly like "the task died", and was diagnosed as that. This
  script exists so that step cannot be dropped.

  TWO WAYS TO AUTOSTART, AND IT PICKS ONE.
  A Scheduled Task is preferred because it can restart the client if the
  process dies. But registering one in the root task folder wants elevation on
  many machines -- observed here as `Register-ScheduledTask : Access is denied`
  (HRESULT 0x80070005) from an ordinary PowerShell. So on failure it falls back
  to a Startup shortcut, which needs no admin and still gives the client what
  it actually requires: logon start, the owner's own session, and a desktop for
  SendInput. What is given up is restart-on-failure, which matters less than it
  sounds -- losing the CONNECTION is the common failure and the client already
  retries forever with backoff.

  EXACTLY ONE OF THEM IS EVER INSTALLED. Having both at once has already
  happened here, and the Pi sees it as viewers=2 while every key gets pressed
  twice.

  THE OTHER THING THAT BIT.
  A Scheduled Task with no working directory runs from C:\Windows\System32, so
  a relative --config resolved to a file that was not there and the client
  exited within milliseconds, before it had a console or a log to say why.
  -WorkingDirectory is that fix; hermes_gesture.py also now looks next to
  itself, so either half alone is enough.

.PARAMETER Dir
  Where hermes_gesture.py and gestures.json live. Default: this script's folder.

.PARAMETER Method
  auto (default) | task | startup. See above.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install-gesture-client.ps1

.EXAMPLE
  # force the no-admin route
  powershell -ExecutionPolicy Bypass -File .\install-gesture-client.ps1 -Method startup
#>

[CmdletBinding()]
param(
    [string]$Dir  = "",
    [string]$Task = "HermesGesture",
    # auto    try a Scheduled Task, fall back to a Startup shortcut
    # task    Scheduled Task only (may need an elevated PowerShell)
    # startup Startup shortcut only -- never needs admin
    [ValidateSet("auto", "task", "startup")]
    [string]$Method = "auto"
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

# OUR clients, not every pythonw on the machine.
#
# The python in use can be a shared one -- on this laptop it is the Hermes
# agent's own venv -- so `Get-Process pythonw | Stop-Process` would kill
# whatever else happens to be running under it. Match on the command line.
function Get-HermesClients {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" `
        -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*hermes_gesture.py*" }
}

# EXACTLY ONE AUTOSTART, whichever it ends up being.
#
# The owner has already had a Startup shortcut and a Scheduled Task installed at
# the same time, which the Pi saw as viewers=2 and which presses every key
# twice. So both are cleared before either is installed, and any client still
# running is stopped -- an install that leaves the previous one alive is the
# same bug wearing a different hat.
$startupLnk = Join-Path ([Environment]::GetFolderPath('Startup')) "HermesGesture.lnk"

if (Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
    try { Unregister-ScheduledTask -TaskName $Task -Confirm:$false } catch {
        Write-Host "  (could not remove the old task: $($_.Exception.Message))"
    }
}
if (Test-Path -LiteralPath $startupLnk) { Remove-Item -LiteralPath $startupLnk -Force }
Get-HermesClients | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

$arguments = "`"$clientPy`" --config `"$config`""
$installed = $null

# --- first choice: a Scheduled Task -------------------------------------
# Preferred because it can restart the client if the process dies, which a
# shortcut cannot. But registering in the root task folder wants elevation on
# many machines, and this must not require an admin prompt to work at all.
if ($Method -in @("auto", "task")) {
    try {
        $action  = New-ScheduledTaskAction -Execute $pythonw `
            -Argument $arguments -WorkingDirectory $Dir
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        # A delay at logon, because the network stack is usually not up yet.
        # Wrapped: a nicety must not be why the install fails.
        try { $trigger.Delay = "PT20S" } catch { }

        # Interactive: SendInput needs a desktop. A task set to "run whether the
        # user is logged on or not" runs in session 0, which has none, and every
        # keypress fails -- silently, with the process alive and connected.
        # NOT `$x = if (...) { } <newline> else { }` -- in an assignment the
        # newline ends the statement and the else is a parse error.
        $user = $env:USERNAME
        if ($env:USERDOMAIN) { $user = "$env:USERDOMAIN\$env:USERNAME" }
        $principal = New-ScheduledTaskPrincipal -UserId $user `
            -LogonType Interactive -RunLevel Limited

        # ExecutionTimeLimit 0 = no limit. The default stops the task after
        # three days, so a listener would silently die on day three.
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
        try {
            $settings.RestartInterval = "PT1M"
            $settings.RestartCount = 99
        } catch { }

        Register-ScheduledTask -TaskName $Task -Action $action `
            -Trigger $trigger -Principal $principal -Settings $settings `
            -ErrorAction Stop | Out-Null
        Start-ScheduledTask -TaskName $Task
        $installed = "task"
        Write-Host "`nregistered scheduled task '$Task' and started it."
    } catch {
        Write-Host "`nscheduled task refused: $($_.Exception.Message)" `
            -ForegroundColor Yellow
        if ($Method -eq "task") { exit 1 }
        Write-Host "falling back to a Startup shortcut, which needs no admin."
    }
}

# --- fallback: a Startup shortcut ---------------------------------------
# Needs no elevation, and gives the client everything it actually requires: it
# runs at logon, in the owner's own session, with a desktop for SendInput.
#
# What it gives up is restart-on-failure. That matters less than it sounds:
# losing the CONNECTION is the common failure and the client already retries
# forever with backoff. Only the process dying is unhandled, and it does not.
if (-not $installed) {
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($startupLnk)
    $sc.TargetPath       = $pythonw
    $sc.Arguments        = $arguments
    $sc.WorkingDirectory = $Dir
    $sc.Description      = "Hermes gesture client"
    $sc.Save()
    Start-Process -FilePath $pythonw -ArgumentList $arguments `
        -WorkingDirectory $Dir | Out-Null
    $installed = "startup"
    Write-Host "`ninstalled Startup shortcut and started it:"
    Write-Host "  $startupLnk"
}
# Long enough for a client that is going to fail to have failed. The failures
# this catches -- bad config path, bad token -- happen at startup, not later.
Start-Sleep -Seconds 5

# REPORT WHAT IS TRUE, NOT WHAT WAS ASKED FOR.
$procs = @(Get-HermesClients)
$log   = Join-Path $env:LOCALAPPDATA "hermes-gesture.log"

Write-Host ""
if ($procs.Count -eq 1) {
    Write-Host "RUNNING  one client (pid $($procs[0].ProcessId))" -ForegroundColor Green
    if ($installed -eq "startup") {
        Write-Host "  autostart: Startup shortcut (no restart-on-failure --" `
                   "run in an elevated PowerShell for a Scheduled Task)"
    } else {
        Write-Host "  autostart: Scheduled Task '$Task'"
    }
} elseif ($procs.Count -eq 0) {
    Write-Host "NOT RUNNING  no hermes_gesture.py process." -ForegroundColor Red
    if ($installed -eq "task") {
        $info = Get-ScheduledTaskInfo -TaskName $Task -ErrorAction SilentlyContinue
        if ($info) {
            Write-Host "  LastTaskResult = $($info.LastTaskResult)  (0 = exited cleanly, 2 = bad config or token)"
        }
    }
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
    Write-Host "$($procs.Count) clients running -- more than one." -ForegroundColor Yellow
    Write-Host "  Each will act on every gesture, so keys get pressed twice."
    Write-Host "  Not all of them are necessarily this client; check:"
    Write-Host "    Get-CimInstance Win32_Process -Filter `"Name='pythonw.exe'`" | Select ProcessId,CommandLine"
}
Write-Host "`nwatch it:  Get-Content `"$log`" -Tail 30 -Wait"
Write-Host "test one:  & `"$pyexe`" `"$clientPy`" --config `"$config`" --fire `"HERMES SPOTIFY`""
