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
    [string]$Dir  = $PSScriptRoot,
    [string]$Task = "HermesGesture"
)

$ErrorActionPreference = "Stop"

$Dir    = (Resolve-Path $Dir).Path
$script = Join-Path $Dir "hermes_gesture.py"
$config = Join-Path $Dir "gestures.json"

foreach ($f in @($script, $config)) {
    if (-not (Test-Path $f)) {
        Write-Error "missing: $f`nCopy hermes_gesture.py and gestures.json into $Dir first."
    }
}

# pythonw.exe, not python.exe: no console window for a thing that runs all day.
# The client detects the missing console and redirects its output to a log --
# without that, every message it produces would go nowhere (CPython's print()
# silently does nothing when sys.stdout is None).
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if (-not $py) { Write-Error "no python.exe on PATH -- install Python first" }
    $pythonw = Join-Path (Split-Path $py) "pythonw.exe"
    if (-not (Test-Path $pythonw)) { Write-Error "no pythonw.exe next to $py" }
}
Write-Host "python : $pythonw"
Write-Host "script : $script"
Write-Host "config : $config"

# Replace any previous definition rather than editing it: an install that
# half-updates an existing task is how you get two clients running at once,
# which shows up on the Pi as viewers=2 and doubles every keypress.
Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue |
    Unregister-ScheduledTask -Confirm:$false

$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument ("`"$script`" --config `"$config`"") `
    -WorkingDirectory $Dir

# At logon, and again shortly after: at logon the network stack is often not up
# yet, and while the client does retry with backoff, starting it once the Pi is
# actually reachable makes the first connection immediate.
$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn)
)
$triggers[0].Delay = "PT20S"

# Interactive: SendInput needs a desktop. A task set to "run whether the user is
# logged on or not" runs in session 0, which has no desktop, and every keypress
# fails -- silently, because the process is alive and connected the whole time.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
    -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 999 -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
# The client is a long-running listener, so "stop it after three days" is wrong.
$settings.DisallowStartIfOnBatteries = $false

Register-ScheduledTask -TaskName $Task -Action $action -Trigger $triggers `
    -Principal $principal -Settings $settings | Out-Null

Write-Host "`nregistered '$Task'. starting it..."
Start-ScheduledTask -TaskName $Task
Start-Sleep -Seconds 3

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
    Write-Host "  why: $log"
} else {
    Write-Host "$($procs.Count) pythonw processes -- more than one client." -ForegroundColor Yellow
    Write-Host "  Each will act on every gesture, so keys get pressed twice."
    Write-Host "  Close the extras, or check for a leftover Startup shortcut:"
    Write-Host "    shell:startup"
}
# --fire is run with python.exe on purpose: it is a diagnostic, and the whole
# point is to SEE what it says. pythonw would hide the answer.
$pyexe = Join-Path (Split-Path $pythonw) "python.exe"
Write-Host "`nwatch it:  Get-Content `"$log`" -Tail 30 -Wait"
Write-Host "test one:  & `"$pyexe`" `"$script`" --config `"$config`" --fire `"HERMES SPOTIFY`""
