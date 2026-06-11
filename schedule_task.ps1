# PowerShell script to register Gmail Newsletter Digest as a Windows Scheduled Task
$ScriptDir = Split-Path -Parent -Path $MyInvocation.MyCommand.Definition
$PythonScript = Join-Path $ScriptDir "digest.py"
$TaskName = "GmailNewsletterDigest"

# Check if Python is available in PATH
$PythonPath = Get-Command "python.exe" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
if (-not $PythonPath) {
    Write-Error "python.exe could not be found in your PATH. Please make sure Python is installed and added to your PATH."
    Exit 1
}

# Task Trigger: Daily at 10:00 PM (EDT) = 7:30 AM (IST)
# Adjust the start date to today
$Today = Get-Date -Format "yyyy-MM-dd"
$TriggerTime = "22:00:00" # 10:00 PM local
$Trigger = New-ScheduledTaskTrigger -Daily -At "$Today $TriggerTime"

# Task Action: Run python.exe targeting digest.py
$Action = New-ScheduledTaskAction -Execute "$PythonPath" -Argument "`"$PythonScript`""

# Task Settings: Wake to run, start when available, run on battery, stop if runs longer than 1 hour
$Settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$Settings.ExecutionTimeLimit = "PT1H" # 1 hour execution limit

# Register Task
Write-Host "Registering Scheduled Task '$TaskName' to run daily at 10:00 PM ($TriggerTime)..."
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Daily Gmail Newsletter Digest (runs at 7:30 AM IST / 10:00 PM EDT)" -Force

Write-Host "Task successfully registered!"
Write-Host "You can verify or run it using Task Scheduler (taskschd.msc) or run:"
Write-Host "  Get-ScheduledTask -TaskName '$TaskName'"
