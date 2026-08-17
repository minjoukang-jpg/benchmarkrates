# Registers (or updates) the daily rates update in Windows Task Scheduler.
#
#   powershell -ExecutionPolicy Bypass -File install_task.ps1
#   powershell -ExecutionPolicy Bypass -File install_task.ps1 -Time 08:30
#   powershell -ExecutionPolicy Bypass -File install_task.ps1 -Remove
#
# Runs under the current user, no admin rights needed. If the machine is asleep
# or off at the scheduled time, the task runs at the next opportunity.

param(
    # Noon, matching the GitHub Actions schedule. Only relevant if you run the
    # update locally instead of on GitHub; do not run both.
    [string]$Time = "12:00",
    [string]$TaskName = "Benchmark Rates Daily Update",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "run_daily.cmd"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    return
}

if (-not (Test-Path $script)) { throw "Cannot find $script" }

# Weekdays only - none of these benchmarks publish at the weekend.
$trigger = New-ScheduledTaskTrigger -Weekly -At $Time `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday

$action = New-ScheduledTaskAction -Execute $script -WorkingDirectory $here

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Trigger $trigger -Action $action `
    -Settings $settings -Description "Fetches PHP BVAL, MYR KLIBOR and USD SOFR into rates.db" `
    -Force | Out-Null

Write-Host "Scheduled '$TaskName' for $Time on weekdays."
Write-Host "Log: $(Join-Path $here 'update.log')"
Write-Host ""
Write-Host "Run it now to confirm it works:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
